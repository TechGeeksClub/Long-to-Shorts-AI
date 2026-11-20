from __future__ import annotations

import json
import logging
import subprocess
import sys
from threading import Lock


logger = logging.getLogger(__name__)
_picker_lock = Lock()
_PICKER_TIMEOUT_SECONDS = 300

# Run the modal dialog in its own process so that a timeout can close it and
# Tk does not share threads or window state with the web server.
_DIALOG_SCRIPT = """
import json
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
try:
    paths = filedialog.askopenfilenames(
        parent=root,
        title='Proje için video seç',
        filetypes=[('Video dosyaları', ('*.mp4', '*.mov', '*.mkv', '*.m4v', '*.avi', '*.webm'))],
    )
    print(json.dumps(list(paths)))
finally:
    root.destroy()
"""

_DIRECTORY_DIALOG_SCRIPT = """
import json
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
try:
    directory = filedialog.askdirectory(
        parent=root,
        title='Dışa aktarılan videonun kaydedileceği klasörü seç',
        mustexist=True,
    )
    print(json.dumps(directory or None))
finally:
    root.destroy()
"""


class PickerError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def choose_video_paths() -> list[str]:
    """Open the local Windows file chooser and return paths without opening videos."""
    if sys.platform != "win32":
        raise PickerError("Dosya seçici Windows'ta kullanılabilir. Video yollarını yapıştırarak devam et.")
    if not _picker_lock.acquire(blocking=False):
        raise PickerError("Video seçici zaten açık. Açık pencereden seçim yap veya pencereyi kapat.", 409)

    try:
        # A Windows venv executable redirects to another process. Start the
        # interpreter directly so timing out also terminates the dialog.
        interpreter = getattr(sys, "_base_executable", None) or sys.executable
        result = subprocess.run(
            [interpreter, "-I", "-X", "utf8", "-c", _DIALOG_SCRIPT],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PICKER_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
        paths = json.loads(result.stdout)
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise ValueError("Invalid file picker result")
        return paths
    except subprocess.TimeoutExpired as exc:
        raise PickerError("Video seçici zaman aşımına uğradı. Yeniden aç veya video yollarını yapıştır.", 504) from exc
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        # Avoid logging stdout or stderr, which can include local paths.
        logger.warning("Windows video picker unavailable (%s)", type(exc).__name__)
        raise PickerError("Video seçici açılamadı. Video yollarını yapıştırarak devam et.") from exc
    finally:
        _picker_lock.release()


def choose_export_directory() -> str | None:
    """Choose an existing local output folder; cancellation leaves it unchanged."""
    if sys.platform != "win32":
        raise PickerError("Klasör seçici Windows'ta kullanılabilir. Klasörün tam yolunu yazarak devam et.")
    if not _picker_lock.acquire(blocking=False):
        raise PickerError("Bir dosya veya klasör seçici zaten açık. Açık pencereden seçim yap veya pencereyi kapat.", 409)
    try:
        interpreter = getattr(sys, "_base_executable", None) or sys.executable
        result = subprocess.run(
            [interpreter, "-I", "-X", "utf8", "-c", _DIRECTORY_DIALOG_SCRIPT],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PICKER_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
        directory = json.loads(result.stdout)
        if directory is not None and (not isinstance(directory, str) or not directory.strip()):
            raise ValueError("Invalid directory picker result")
        return directory
    except subprocess.TimeoutExpired as exc:
        raise PickerError("Klasör seçici zaman aşımına uğradı. Yeniden aç veya klasörün tam yolunu yaz.", 504) from exc
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        logger.warning("Windows export folder picker unavailable (%s)", type(exc).__name__)
        raise PickerError("Klasör seçici açılamadı. Klasörün tam yolunu yazarak devam et.") from exc
    finally:
        _picker_lock.release()
