import shutil
from pathlib import Path

from app.config import Settings


def job_workspace(settings: Settings, job_id: str) -> Path:
    """Locate generated files independently of the source video's location."""
    identifier = Path(job_id)
    if identifier.name != job_id or job_id in {"", ".", ".."} or ":" in job_id:
        raise ValueError("Geçersiz video kimliği.")
    root = settings.data_dir.resolve() / "jobs"
    workspace = root / job_id
    if workspace.resolve() != workspace:
        raise ValueError("Video çalışma klasörü uygulamanın veri dizini dışında.")
    return workspace


def speech_transcript_path(settings: Settings, job_id: str) -> Path:
    return job_workspace(settings, job_id) / "speech-cleanup" / "transcript.json"


def remove_job_workspace(settings: Settings, job_id: str) -> None:
    workspace = job_workspace(settings, job_id)
    shutil.rmtree(workspace, ignore_errors=True)
