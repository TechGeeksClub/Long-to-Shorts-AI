import json
import subprocess
from types import SimpleNamespace

import pytest

from app import local_video_picker as picker


@pytest.mark.parametrize("directory", ["D:/Videolar/İşlenmiş kayıtlar", None])
def test_export_directory_picker_returns_selected_folder_or_cancel(monkeypatch, directory):
    monkeypatch.setattr(picker.sys, "platform", "win32")
    calls = []

    def run(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(stdout=json.dumps(directory))

    monkeypatch.setattr(picker.subprocess, "run", run)
    assert picker.choose_export_directory() == directory
    assert calls[0][0][-1] == picker._DIRECTORY_DIALOG_SCRIPT
    assert calls[0][0][1:4] == ["-I", "-X", "utf8"]
    assert calls[0][1]["timeout"] == picker._PICKER_TIMEOUT_SECONDS
    assert calls[0][1]["encoding"] == "utf-8"
    assert not picker._picker_lock.locked()


def test_export_directory_picker_shares_lock_with_video_picker(monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("Dialog already open"))
    assert picker._picker_lock.acquire(blocking=False)
    try:
        with pytest.raises(picker.PickerError) as error:
            picker.choose_export_directory()
        assert error.value.status_code == 409
    finally:
        picker._picker_lock.release()


@pytest.mark.parametrize("result", ["[]", '""', "42", "{}", "invalid json"])
def test_export_directory_picker_rejects_invalid_result_and_unlocks(monkeypatch, result):
    monkeypatch.setattr(picker.sys, "platform", "win32")
    monkeypatch.setattr(picker.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=result))
    with pytest.raises(picker.PickerError) as error:
        picker.choose_export_directory()
    assert error.value.status_code == 503
    assert not picker._picker_lock.locked()


def test_export_directory_picker_timeout_unlocks_for_retry(monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "win32")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("folder-picker", 300)

    monkeypatch.setattr(picker.subprocess, "run", timeout)
    with pytest.raises(picker.PickerError) as error:
        picker.choose_export_directory()
    assert error.value.status_code == 504
    assert not picker._picker_lock.locked()


def test_export_directory_picker_unavailable_platform_has_manual_fallback(monkeypatch):
    monkeypatch.setattr(picker.sys, "platform", "linux")
    with pytest.raises(picker.PickerError, match="tam yolunu"):
        picker.choose_export_directory()
    assert not picker._picker_lock.locked()
