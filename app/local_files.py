from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path


logger = logging.getLogger(__name__)


class FileBrowserError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def reveal_file(path: Path) -> None:
    """Select an existing local output in Windows File Explorer."""
    if sys.platform != "win32":
        raise FileBrowserError("Dosya Gezgini bu bilgisayarda kullanılamıyor.")
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileBrowserError("Çıktı dosyası bulunamadı.", 404)

    explorer = str(Path(os.environ.get("WINDIR", "C:/Windows")) / "explorer.exe")
    # Explorer expects /select,<path>. Quote the path independently so spaces
    # are preserved, and launch the executable directly without a command shell.
    command = f"{subprocess.list2cmdline([explorer])} /select,{subprocess.list2cmdline([str(resolved)])}"
    try:
        subprocess.Popen(command, executable=explorer, close_fds=True)
    except OSError as exc:
        logger.warning("File Explorer unavailable (%s)", type(exc).__name__)
        raise FileBrowserError("Dosya Gezgini açılamadı. Yeniden dene.") from exc
