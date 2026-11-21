$ErrorActionPreference = "Stop"
$originalPath = $env:Path
$originalPythonPath = $env:PYTHONPATH
$toolsDir = Join-Path $PSScriptRoot ".tools"
$uvVersion = "0.11.19"
$uvExe = Join-Path $toolsDir "uv\uv.exe"

function Install-PortableZip {
    param($Name, $Url, $Sha256, $ArchiveRoot, $Destination, $Executable)

    $requiredFile = Join-Path $Destination $Executable
    if (Test-Path -LiteralPath $requiredFile -PathType Leaf) { return }

    $toolsRoot = (New-Item -ItemType Directory -Force -Path $toolsDir).FullName
    $stage = New-Item -ItemType Directory -Path (Join-Path $toolsRoot ("install-" + [Guid]::NewGuid()))
    try {
        Write-Host "Downloading $Name..." -ForegroundColor Cyan
        $archive = Join-Path $stage.FullName "package.zip"
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $archive
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $Sha256) {
            throw "The $Name download failed its SHA-256 check. Run the script again to retry."
        }
        $extracted = Join-Path $stage.FullName "extracted"
        Expand-Archive -LiteralPath $archive -DestinationPath $extracted
        $source = Join-Path $extracted $ArchiveRoot
        if (-not (Test-Path -LiteralPath (Join-Path $source $Executable) -PathType Leaf)) {
            throw "The $Name archive has an unexpected layout."
        }
        try {
            [IO.Directory]::Move([IO.Path]::GetFullPath($source), $Destination)
        }
        catch {
            if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) { throw }
        }
    }
    finally {
        if ($stage.Parent.FullName -eq $toolsRoot) {
            Remove-Item -LiteralPath $stage.FullName -Recurse -Force
        }
    }
}

function Test-Node {
    if (-not (Get-Command node.exe -CommandType Application -ErrorAction SilentlyContinue) -or
        -not (Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue)) { return $false }
    $version = & node.exe --version
    return $LASTEXITCODE -eq 0 -and $version -match '^v(\d+)\.(\d+)\.' -and
        ([int]$Matches[1] -gt 22 -or ([int]$Matches[1] -eq 22 -and [int]$Matches[2] -ge 12))
}

function Test-MediaTools {
    $ffmpeg = if ($env:FFMPEG_PATH) { $env:FFMPEG_PATH } else { "ffmpeg.exe" }
    $ffprobe = if ($env:FFPROBE_PATH) { $env:FFPROBE_PATH } else { "ffprobe.exe" }
    if (-not (Get-Command $ffmpeg -CommandType Application -ErrorAction SilentlyContinue) -or
        -not (Get-Command $ffprobe -CommandType Application -ErrorAction SilentlyContinue)) { return $false }
    $filters = & $ffmpeg -hide_banner -filters
    $encoders = & $ffmpeg -hide_banner -encoders
    return [bool]($filters -match '^\s*\S+\s+ass\s') -and [bool]($encoders -match '^\s*\S+\s+libx264\s')
}

Push-Location $PSScriptRoot
try {
    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "Shorts Studio requires 64-bit Windows."
    }
    $env:PYTHONPATH = $null

    if (-not (Test-Node)) {
        $nodeVersion = "24.22.3"
        $nodeArchive = "node-v$nodeVersion-win-x64"
        $nodeDir = Join-Path $toolsDir $nodeArchive
        $nodeUrl = "https://nodejs.org/dist/v$nodeVersion/$nodeArchive.zip"
        $nodeHash = "e612af99f626ab415ea225af6bce5c971feb448cb5cae250bb1c3fa50c5f7d9f"
        Install-PortableZip "Node.js $nodeVersion" $nodeUrl $nodeHash $nodeArchive $nodeDir "npm.cmd"
        $env:Path = "$nodeDir;$env:Path"
    }

    if (-not (Test-MediaTools)) {
        $ffmpegVersion = "9.1"
        $ffmpegArchive = "ffmpeg-$ffmpegVersion-essentials_build"
        $ffmpegDir = Join-Path $toolsDir $ffmpegArchive
        $ffmpegUrl = "https://github.com/GyanD/codexffmpeg/releases/download/$ffmpegVersion/$ffmpegArchive.zip"
        $ffmpegHash = "b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd"
        Install-PortableZip "FFmpeg $ffmpegVersion" $ffmpegUrl $ffmpegHash $ffmpegArchive $ffmpegDir "bin\ffprobe.exe"
        $ffmpegBin = Join-Path $ffmpegDir "bin"
        $env:Path = "$ffmpegBin;$env:Path"
        if (-not (Test-MediaTools)) {
            throw "FFmpeg needs the ass filter and libx264 encoder. Check FFMPEG_PATH and FFPROBE_PATH if set."
        }
    }

    $uvUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-x86_64-pc-windows-msvc.zip"
    $uvHash = "1665fc8e37b5d70a134820d6d7891747471a2ac8bc940ee7af0b69fd03b28d61"
    Install-PortableZip "uv $uvVersion" $uvUrl $uvHash "." (Split-Path -Parent $uvExe) "uv.exe"

    Write-Host "Preparing Python 3.12..." -ForegroundColor Cyan
    & $uvExe sync --locked --managed-python --python cpython-3.12-windows-x86_64-none --dev
    if ($LASTEXITCODE -ne 0) { throw "Python dependencies could not be installed." }

    Write-Host "Building the web interface..." -ForegroundColor Cyan
    Push-Location (Join-Path $PSScriptRoot "frontend")
    try {
        & npm.cmd ci --include=dev
        if ($LASTEXITCODE -ne 0) { throw "Web dependencies could not be installed." }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "The web interface could not be built." }
    }
    finally {
        Pop-Location
    }

    & $uvExe run --no-sync --python cpython-3.12-windows-x86_64-none python -m app.launcher @args
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
finally {
    $env:Path = $originalPath
    $env:PYTHONPATH = $originalPythonPath
    Pop-Location
}
