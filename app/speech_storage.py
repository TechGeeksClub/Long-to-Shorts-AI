from pathlib import Path

from app.config import Settings
from app.job_workspace import job_workspace


def speech_export_workspace(
    settings: Settings, job_id: str, export_id: str, *, default_location: bool = False,
) -> Path:
    if (
        Path(export_id).name != export_id
        or export_id in {"", ".", ".."}
        or any(character in export_id for character in ":/\\")
    ):
        raise ValueError("Geçersiz dışa aktarma kimliği.")

    job_dir = job_workspace(settings, job_id)
    if settings.speech_export_dir is not None and not default_location:
        root = settings.speech_export_dir.resolve() / job_dir.name
    else:
        root = job_dir / "speech-cleanup" / "exports"

    output_dir = root / export_id
    if output_dir.resolve() != output_dir:
        raise ValueError("Konuşma temizleme çıktı klasörü belirlenen dizinin dışında.")
    return output_dir


def remove_speech_export_partials(output_dir: Path) -> None:
    (output_dir / "render.partial.mp4").unlink(missing_ok=True)
    for chunk in output_dir.glob("cut-*.mp4"):
        if chunk.stem.removeprefix("cut-").isdigit():
            chunk.unlink(missing_ok=True)
