#!/bin/bash
set -eo pipefail

fail() {
    printf 'Hata: %s\n' "$1" >&2
    exit 1
}

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
project_dir="$PWD"
unset PYTHONPATH

[[ "$(uname -s)" == Darwin ]] || fail 'Bu betik macOS içindir. Windows için start.cmd kullanın.'
macos_version="$(sw_vers -productVersion)"
(( ${macos_version%%.*} >= 14 )) || fail 'Bu sürüm macOS 14 veya üstünü gerektirir.'

# An Intel shell under Rosetta should still install native Apple Silicon tools.
if [[ "$(sysctl -n sysctl.proc_translated 2>/dev/null || true)" == 1 ]]; then
    exec /usr/bin/arch -arm64 /bin/bash "$project_dir/start.sh" "$@"
fi

uv_version=0.11.19
case "$(uname -m)" in
    arm64)
        brew_default=/opt/homebrew/bin/brew
        uv_target=aarch64-apple-darwin
        uv_checksum=d8f59c38e8c4168ee468d423cd63184be12fa6995a4283d41ee1a14d003c9453
        ;;
    x86_64)
        brew_default=/usr/local/bin/brew
        uv_target=x86_64-apple-darwin
        uv_checksum=1585f415cade9f061e7f00fe5b00030a79ccfac60c650242ce639ba946138d40
        ;;
    *) fail 'Bu işlemci mimarisi desteklenmiyor.' ;;
esac

node_ready() {
    command -v npm >/dev/null && command -v node >/dev/null &&
        node -e '
            const [major, minor] = process.versions.node.split(".").map(Number);
            process.exit(major > 22 || (major === 22 && minor >= 12) ? 0 : 1);
        ' 2>/dev/null
}

ffmpeg_ready() {
    "$1" -hide_banner -filters 2>/dev/null | grep -E '^[[:space:].A-Z]+ass[[:space:]]' >/dev/null &&
        "$1" -hide_banner -encoders 2>/dev/null | grep -E '^[[:space:].A-Z]+libx264[[:space:]]' >/dev/null
}

media_ready() {
    command -v "${FFPROBE_PATH-ffprobe}" >/dev/null && ffmpeg_ready "${FFMPEG_PATH-ffmpeg}"
}

if [[ ${FFMPEG_PATH-ffmpeg} != ffmpeg ]] && ! ffmpeg_ready "$FFMPEG_PATH"; then
    fail 'FFMPEG_PATH, ASS altyazı filtresini ve libx264 kodlayıcısını içeren bir FFmpeg dosyasına işaret etmeli.'
fi
if [[ ${FFPROBE_PATH-ffprobe} != ffprobe ]] && ! command -v "$FFPROBE_PATH" >/dev/null; then
    fail 'FFPROBE_PATH çalıştırılabilir bir FFprobe dosyasına işaret etmeli.'
fi

find_missing_packages() {
    packages=()
    if ! node_ready; then packages+=(node@24); fi
    # The full formula includes the libass filter used by clip exports.
    if ! media_ready; then packages+=(ffmpeg-full); fi
}

find_missing_packages
if (( ${#packages[@]} )); then
    if [[ -x "$brew_default" ]]; then
        brew_exe="$brew_default"
    elif ! brew_exe="$(command -v brew)"; then
        printf 'Homebrew hazırlanıyor. Yükleyici macOS yönetici onayı isteyebilir.\n'
        brew_installer="$(curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" \
            || fail 'Homebrew yükleyicisi indirilemedi.'
        /bin/bash -c "$brew_installer" || fail 'Homebrew kurulumu tamamlanamadı.'
        brew_exe="$brew_default"
    fi
    [[ -x "$brew_exe" ]] || fail 'Homebrew çalıştırılabilir dosyası bulunamadı.'

    for formula in "${packages[@]}"; do
        formula_prefix="$("$brew_exe" --prefix "$formula")"
        export PATH="$formula_prefix/bin:$PATH"
    done

    find_missing_packages
    if (( ${#packages[@]} )); then
        printf 'Sistem bağımlılıkları kuruluyor …\n'
        "$brew_exe" install "${packages[@]}" || fail 'Homebrew bağımlılıkları kurulamadı.'
    fi
fi

node_ready || fail 'Node.js 22.12 veya üstü ve npm bulunamadı.'
media_ready || fail 'ASS altyazı filtresi ve libx264 kodlayıcısı olan FFmpeg ile FFprobe bulunamadı.'

uv_dir="$project_dir/.tools/uv/$uv_target"
uv_exe="$uv_dir/uv"
install_uv() (
    printf 'uv %s indiriliyor …\n' "$uv_version"
    mkdir -p "$uv_dir"
    staging="$(mktemp -d "$uv_dir/install.XXXXXXXX")"
    asset="uv-$uv_target.tar.gz"
    archive="$staging/$asset"
    trap 'rm -f -- "$archive" "$staging/uv"; rmdir -- "$staging"' EXIT
    curl --proto '=https' --tlsv1.2 -fsSL \
        "https://github.com/astral-sh/uv/releases/download/$uv_version/$asset" -o "$archive" \
        || fail 'uv indirilemedi.'
    printf '%s  %s\n' "$uv_checksum" "$archive" | shasum -a 256 -c - >/dev/null \
        || fail 'uv arşivinin SHA-256 doğrulaması başarısız oldu.'
    tar -xOf "$archive" "uv-$uv_target/uv" > "$staging/uv"
    chmod 755 "$staging/uv"
    mv -f -- "$staging/uv" "$uv_exe"
)

uv_identification="$("$uv_exe" --version 2>/dev/null || true)"
if [[ "$uv_identification" != "uv $uv_version" && "$uv_identification" != "uv $uv_version "* ]]; then
    install_uv
fi

printf 'Python 3.12 ortamı hazırlanıyor …\n'
"$uv_exe" sync --locked --python 3.12 --managed-python --dev || fail 'Python bağımlılıkları kurulamadı.'

printf 'Web arayüzü hazırlanıyor …\n'
(cd frontend && npm ci --include=dev && npm run build) || fail 'Web arayüzü hazırlanamadı.'

exec "$uv_exe" run --no-sync python -m app.launcher "$@"
