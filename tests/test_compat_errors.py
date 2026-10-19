"""Совместимость со старыми Windows (8.1: PowerShell 4, нет StartType) и журнал собственных ошибок программы."""
import threading
import types
import zipfile

import pytest

from piothelper import checks, diagnose, errorlog, fixes, report


# ---------- службы: CIM вместо Get-Service.StartType ----------

CIM = [
    {"Name": "atol-grpc-service", "DisplayName": "ATOL: Fptr grpc service", "State": "Running", "StartMode": "Auto"},
    {"Name": "esm-orchestrator", "DisplayName": "ESM: Orchestrator", "State": "Stopped", "StartMode": "Auto"},
    {"Name": "FrontolSynchro", "DisplayName": "FrontolSynchro", "State": "Stopped", "StartMode": "Manual"},
    {"Name": "Start Pending", "DisplayName": None, "State": "Start Pending", "StartMode": "Disabled"},
]


def test_cim_services_are_normalized():
    s = {x["name"]: x for x in checks.parse_cim_services(CIM)}
    assert s["atol-grpc-service"]["status"] == "Running" and s["atol-grpc-service"]["start"] == "Automatic"
    assert s["esm-orchestrator"]["status"] == "Stopped" and s["esm-orchestrator"]["start"] == "Automatic"
    assert s["FrontolSynchro"]["start"] == "Manual"
    assert s["Start Pending"]["status"] == "StartPending" and s["Start Pending"]["display"] == "Start Pending"


def test_get_service_without_starttype_gives_unknown_not_crash():
    """PowerShell 4 (Windows 8.1): у Get-Service нет StartType, в JSON поле либо отсутствует, либо null."""
    data = [{"Name": "a", "DisplayName": "A", "Status": 4}, {"Name": "b", "DisplayName": "B", "Status": 1, "StartType": None},
            {"Name": "c", "DisplayName": "C", "Status": 4, "StartType": 2}]
    s = {x["name"]: x for x in checks.parse_get_service(data)}
    assert s["a"]["start"] == "Unknown" and s["b"]["start"] == "Unknown" and s["c"]["start"] == "Automatic"
    assert s["a"]["status"] == "Running" and s["b"]["status"] == "Stopped"


def test_falls_back_to_get_service_when_wmi_is_broken(monkeypatch):
    calls = []

    def run_json(script, timeout=40):
        calls.append(script)
        if "Win32_Service" in script:
            raise RuntimeError("WMI повреждён")
        return [{"Name": "x", "DisplayName": "X", "Status": 4}]
    monkeypatch.setattr(checks, "_run_json", run_json)
    res = checks.all_services()
    assert res == [{"name": "x", "display": "X", "status": "Running", "start": "Unknown"}] and len(calls) == 2


def test_falls_back_when_wmi_returns_nothing(monkeypatch):
    monkeypatch.setattr(checks, "_run_json", lambda script, timeout=40: [] if "Win32_Service" in script else
                        [{"Name": "x", "DisplayName": "X", "Status": 4, "StartType": 2}])
    assert checks.all_services()[0]["start"] == "Automatic"


def test_unknown_start_type_is_a_warning_not_a_failure(monkeypatch, cfg):
    monkeypatch.setattr(checks, "all_services", lambda: [{"name": "esm-orchestrator", "display": "ESM: Orchestrator",
                                                          "status": "Stopped", "start": "Unknown"}])
    findings = diagnose.check_services(cfg)
    assert findings[0].level == "warn" and findings[0].fix is not None


def test_real_machine_services_have_known_start_modes():
    services = checks.all_services()
    assert len(services) > 20
    assert {s["start"] for s in services} <= {"Automatic", "Manual", "Disabled", "Boot", "System", "Unknown"}
    assert any(s["status"] == "Running" for s in services)


# ---------- журнал ошибок ----------

def test_log_writes_traceback_environment_and_returns_short_text(isolated_app):
    try:
        raise ValueError("сломалось тут")
    except ValueError as e:
        short = errorlog.log(e, "проверка теста")
    text = (isolated_app / "error.log").read_text(encoding="utf-8")
    assert short == "ValueError: сломалось тут"
    assert "проверка теста" in text and "Traceback" in text and "сломалось тут" in text
    assert "PIoT Helper" in text and "Python" in text and "Windows" in text


def test_log_is_capped(isolated_app, monkeypatch):
    monkeypatch.setattr(errorlog, "MAX_BYTES", 2000)
    for i in range(30):
        errorlog.log(RuntimeError(f"ошибка номер {i} " + "x" * 200), "цикл")
    assert (isolated_app / "error.log").stat().st_size < 6000
    assert "номер 29" in (isolated_app / "error.log").read_text(encoding="utf-8")


def test_log_never_raises_even_if_folder_is_unwritable(monkeypatch, tmp_path):
    monkeypatch.setattr(errorlog, "app_dir", lambda: tmp_path / "нет" / "такой" / "папки")
    assert errorlog.log(RuntimeError("x"), "тест") in ("RuntimeError: x", "")


def test_hooks_capture_exceptions_from_background_threads(isolated_app, monkeypatch):
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)   # вернётся после теста
    monkeypatch.setattr(errorlog.sys, "excepthook", errorlog.sys.excepthook)
    errorlog.install_hooks()

    def boom():
        raise KeyError("в потоке")
    t = threading.Thread(target=boom, name="тестовый-поток")
    t.start()
    t.join()
    text = (isolated_app / "error.log").read_text(encoding="utf-8")
    assert "в потоке" in text and "тестовый-поток" in text


def test_fatal_logs_shows_dialog_with_path_and_exits(isolated_app, monkeypatch):
    shown = []
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(
        user32=types.SimpleNamespace(MessageBoxW=lambda h, text, title, flags: shown.append((text, title, flags))),
        shell32=types.SimpleNamespace(IsUserAnAdmin=lambda: 0)))
    monkeypatch.setattr(errorlog, "ctypes", fake_ctypes)
    monkeypatch.setattr(errorlog.os, "_exit", lambda code: (_ for _ in ()).throw(SystemExit(code)))
    with pytest.raises(SystemExit):
        errorlog.fatal(ImportError("нет модуля tkinter"))
    assert shown and "нет модуля tkinter" in shown[0][0] and "error.log" in shown[0][0]
    assert "нет модуля tkinter" in (isolated_app / "error.log").read_text(encoding="utf-8")


# ---------- отчёт ----------

def test_report_contains_environment_and_error_log(monkeypatch, cfg, isolated_app):
    monkeypatch.setattr(checks, "run_summary", lambda c: [("ok", "всё хорошо")])
    monkeypatch.setattr(diagnose, "run", lambda c: [])
    monkeypatch.setattr(fixes, "ps", lambda *a, **k: "4.0")
    errorlog.log(RuntimeError("видимая ошибка программы"), "тест")
    cfg["paths"] = []
    out = isolated_app / "r.zip"
    report.build_report(cfg, out, mask=False)
    z = zipfile.ZipFile(out)
    assert "PowerShell 4.0" in z.read("summary.txt").decode("utf-8")
    assert "видимая ошибка программы" in z.read("error.log").decode("utf-8")
