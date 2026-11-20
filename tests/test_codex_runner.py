import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import codex_runner
from app.codex_runner import CodexError, codex_status, resolve_codex_executable, run_codex_json


SCHEMA = {
    "type": "object", "properties": {"topics": {"type": "array", "items": {"type": "string"}}},
    "required": ["topics"], "additionalProperties": False,
}


def settings(**overrides):
    values = {"codex_path": "", "codex_model": "", "codex_timeout_seconds": 30}
    values.update(overrides)
    return SimpleNamespace(**values)


def isolate_executable(monkeypatch):
    monkeypatch.setattr(codex_runner, "resolve_codex_executable", lambda configured="": "C:/Codex/codex.exe")


def fake_process(monkeypatch, *, answer='{"topics":["Motor konforu"]}', code=0,
                 diagnostic=b"", events=b'{"type":"thread.started","model":"reported-model"}\n',
                 error=None, communicate_callback=None):
    calls = []

    class Process:
        pid = 98765

        def __init__(self, arguments, **kwargs):
            self.returncode = code
            self.arguments = arguments
            self.kwargs = kwargs
            calls.append(self)
            kwargs["stdout"].write(events)
            kwargs["stderr"].write(diagnostic)

        def communicate(self, input=None, timeout=None):
            self.input = input
            self.timeout = timeout
            if error:
                raise error
            if communicate_callback:
                return communicate_callback(self, input, timeout)
            if answer is not None:
                Path(self.arguments[self.arguments.index("--output-last-message") + 1]).write_text(answer, encoding="utf-8")
            return None, None

        def poll(self):
            return self.returncode

    isolate_executable(monkeypatch)
    monkeypatch.setattr(codex_runner.subprocess, "Popen", Process)
    return calls


def test_explicit_native_executable_with_spaces_is_resolved(tmp_path, monkeypatch):
    executable = tmp_path / "Codex app" / "codex.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"executable")
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _: None)
    assert resolve_codex_executable(str(executable)) == str(executable.resolve())


@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1", ".py", ".sh"])
def test_shell_wrappers_are_never_executed(tmp_path, monkeypatch, suffix):
    wrapper = tmp_path / ("codex" + suffix)
    wrapper.write_text("wrapper", encoding="utf-8")
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _: str(wrapper))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "missing-local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "missing-roaming"))
    assert resolve_codex_executable(str(wrapper)) is None
    assert resolve_codex_executable() is None


def test_app_installation_fallback_uses_newest_existing_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_runner.shutil, "which", lambda _: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    old = tmp_path / "OpenAI" / "Codex" / "bin" / "old" / "codex.exe"
    new = tmp_path / "OpenAI" / "Codex" / "bin" / "new" / "codex.exe"
    for path, stamp in ((old, 100), (new, 200)):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"binary")
        os.utime(path, (stamp, stamp))
    assert resolve_codex_executable() == str(new.resolve())


def test_status_only_checks_existing_login_without_reading_credentials(monkeypatch):
    isolate_executable(monkeypatch)
    calls = []

    def run(arguments, **kwargs):
        calls.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")

    monkeypatch.setattr(codex_runner.subprocess, "run", run)
    result = codex_status(settings())
    assert result == {"provider": "codex", "available": True, "authenticated": True,
                      "model": None, "message": "Codex hazır; kayıtlı Codex hesabı kullanılacak."}
    assert calls[0][0] == ["C:/Codex/codex.exe", "login", "status"]
    assert calls[0][1]["timeout"] == 10


def test_status_is_clear_when_missing_logged_out_or_unresponsive(monkeypatch):
    monkeypatch.setattr(codex_runner, "resolve_codex_executable", lambda _: None)
    assert codex_status(settings())["available"] is False
    isolate_executable(monkeypatch)
    monkeypatch.setattr(codex_runner.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=1, stdout="Not logged in: SECRET", stderr=""))
    result = codex_status(settings())
    assert result["available"] is True and result["authenticated"] is False
    assert "SECRET" not in result["message"]
    monkeypatch.setattr(codex_runner.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("codex", 10)))
    assert "zamanında" in codex_status(settings())["message"]


def test_runner_uses_stdin_schema_isolation_and_saved_auth_without_model_guess(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-key")
    monkeypatch.setenv("CODEX_API_KEY", "unrelated-codex-key")
    monkeypatch.setenv("CODEX_HOME", "C:/existing-login")
    calls = fake_process(monkeypatch)
    result, model = run_codex_json(prompt="Türkçe konuşma\nİkinci satır", schema=SCHEMA,
                                   settings=settings(), work_dir=tmp_path)
    process = calls[0]
    assert result == {"topics": ["Motor konforu"]} and model == "reported-model"
    assert process.input == "Türkçe konuşma\nİkinci satır".encode("utf-8")
    assert "Türkçe konuşma" not in process.arguments
    assert "--model" not in process.arguments
    assert process.arguments[1:3] == ["--no-daemon", "exec"]
    assert "--ignore-user-config" in process.arguments and "--ephemeral" in process.arguments
    assert process.arguments[process.arguments.index("--sandbox") + 1] == "read-only"
    assert 'approval_policy="never"' in process.arguments
    assert 'web_search="disabled"' in process.arguments
    assert "project_doc_max_bytes=0" in process.arguments
    assert all("bypass" not in argument for argument in process.arguments)
    disabled = [process.arguments[index + 1] for index, arg in enumerate(process.arguments) if arg == "--disable"]
    assert {"shell_tool", "apps", "plugins", "hooks", "multi_agent", "browser_use", "computer_use"} <= set(disabled)
    assert process.kwargs["env"]["CODEX_HOME"] == "C:/existing-login"
    assert "OPENAI_API_KEY" not in process.kwargs["env"] and "CODEX_API_KEY" not in process.kwargs["env"]
    assert process.kwargs["cwd"] != tmp_path
    assert not Path(process.kwargs["cwd"]).exists()


def test_configured_model_is_preserved_and_unknown_default_is_not_invented(tmp_path, monkeypatch):
    calls = fake_process(monkeypatch, events=b"")
    configured = settings()
    configured.codex_model = "user-selected-model"
    _, model = run_codex_json(prompt="Analyze", schema=SCHEMA, settings=configured, work_dir=tmp_path)
    assert model == "user-selected-model"
    assert calls[0].arguments[calls[0].arguments.index("--model") + 1] == model
    _, default = run_codex_json(prompt="Analyze", schema=SCHEMA, settings=settings(), work_dir=tmp_path)
    assert default is None
    assert calls[0].kwargs["cwd"] != calls[1].kwargs["cwd"]


@pytest.mark.parametrize("diagnostic,expected", [
    (b"unexpected argument --ephemeral SECRET_TRANSCRIPT", "güncelleyip"),
    (b"401 unauthorized SECRET_TRANSCRIPT", "oturumu"),
    (b"429 quota exceeded SECRET_TRANSCRIPT", "kullanım sınırına"),
    (b"model is not supported SECRET_TRANSCRIPT", "modeli"),
    (b"network connection failed SECRET_TRANSCRIPT", "bağlanılamadı"),
    (b"Unexpected failure SECRET_TRANSCRIPT", "tamamlayamadı"),
])
def test_failures_have_bounded_turkish_messages_without_prompt_or_credentials(tmp_path, monkeypatch, diagnostic, expected):
    fake_process(monkeypatch, code=1, diagnostic=diagnostic)
    with pytest.raises(CodexError) as raised:
        run_codex_json(prompt="PRIVATE_PROMPT", schema=SCHEMA, settings=settings(), work_dir=tmp_path)
    assert expected in str(raised.value)
    assert "SECRET_TRANSCRIPT" not in str(raised.value) and "PRIVATE_PROMPT" not in str(raised.value)
    assert len(str(raised.value)) < 250


@pytest.mark.parametrize("answer", [None, "", "not json", "[]", "null"])
def test_missing_or_invalid_structured_results_are_not_accepted(tmp_path, monkeypatch, answer):
    fake_process(monkeypatch, answer=answer)
    with pytest.raises(CodexError, match="geçerli bir konu planı"):
        run_codex_json(prompt="Analyze", schema=SCHEMA, settings=settings(), work_dir=tmp_path)


def test_timeout_and_interruption_clean_up_the_owned_process(tmp_path, monkeypatch):
    cleanup = Mock()
    monkeypatch.setattr(codex_runner, "_terminate_process_tree", cleanup)
    calls = fake_process(monkeypatch, error=subprocess.TimeoutExpired("codex", 30))
    with pytest.raises(CodexError, match="zaman aşımına"):
        run_codex_json(prompt="Analyze", schema=SCHEMA, settings=settings(), work_dir=tmp_path)
    cleanup.assert_called_once_with(calls[0])
    assert not Path(calls[0].kwargs["cwd"]).exists()
    cleanup.reset_mock()
    calls = fake_process(monkeypatch, error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        run_codex_json(prompt="Analyze", schema=SCHEMA, settings=settings(), work_dir=tmp_path)
    cleanup.assert_called_once_with(calls[0])


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup")
def test_windows_timeout_kills_only_owned_process_tree(monkeypatch):
    process = Mock(pid=98765)
    process.poll.return_value = None
    calls = []

    def taskkill(arguments, **kwargs):
        calls.append(arguments)
        process.poll.return_value = 1
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(codex_runner.subprocess, "run", taskkill)
    codex_runner._terminate_process_tree(process)
    assert calls[0][1:] == ["/PID", "98765", "/T", "/F"]
    assert Path(calls[0][0]).name == "taskkill.exe"
    process.kill.assert_not_called()


def test_shutdown_cancels_only_registered_runs_and_reports_interruption(tmp_path, monkeypatch):
    started, released = threading.Event(), threading.Event()
    results = []

    def communicate(process, input, timeout):
        started.set()
        assert released.wait(3)
        process.returncode = -1
        return None, None

    calls = fake_process(monkeypatch, code=None, communicate_callback=communicate)
    stopped = []

    def terminate(process, *, wait=True):
        stopped.append((process, wait))
        released.set()

    monkeypatch.setattr(codex_runner, "_terminate_process_tree", terminate)

    def analyze():
        try:
            run_codex_json(prompt="Analyze", schema=SCHEMA, settings=settings(), work_dir=tmp_path)
        except CodexError as error:
            results.append(str(error))

    worker = threading.Thread(target=analyze)
    worker.start()
    assert started.wait(3)
    codex_runner.cancel_active_codex_runs()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert stopped == [(calls[0], False)]
    assert len(results) == 1 and "durduruldu" in results[0]
    assert not Path(calls[0].kwargs["cwd"]).exists()
    codex_runner.cancel_active_codex_runs()
    assert stopped == [(calls[0], False)]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_does_not_start_an_agent(tmp_path, monkeypatch, timeout):
    isolate_executable(monkeypatch)
    configured = settings()
    configured.codex_timeout_seconds = timeout
    start = Mock()
    monkeypatch.setattr(codex_runner.subprocess, "Popen", start)
    with pytest.raises(CodexError, match="ayar"):
        run_codex_json(prompt="Analyze", schema=SCHEMA, settings=configured, work_dir=tmp_path)
    start.assert_not_called()
