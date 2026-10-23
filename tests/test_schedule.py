"""Ночной перезапуск служб: задача Планировщика Windows и режим --scheduled-restart."""
import json
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest

from piothelper import diagnose, fixes, restart, schedule, scheduled, stats

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


# ---------- время и XML ----------

@pytest.mark.parametrize("text,ok", [("05:00", True), ("00:00", True), ("23:59", True), (" 05:00 ", True), ("24:00", False),
                                     ("5:00", False), ("05:60", False), ("05-00", False), ("", False), ("утром", False)])
def test_valid_time(text, ok):
    assert schedule.valid_time(text) is ok


def _xml(**kw):
    return ET.fromstring(schedule.build_xml("05:00", **kw).split("?>", 1)[1])


def test_xml_for_system_task():
    root = _xml(system=True, program=r"C:\Program Files\PIoT\PIoTHelper.exe", arguments="--scheduled-restart")
    assert root.find(".//t:UserId", NS).text == "S-1-5-18" and root.find(".//t:RunLevel", NS).text == "HighestAvailable"
    assert root.find(".//t:StartBoundary", NS).text.endswith("T05:00:00")
    assert root.find(".//t:DaysInterval", NS).text == "1"
    assert root.find(".//t:Command", NS).text == r"C:\Program Files\PIoT\PIoTHelper.exe"
    assert root.find(".//t:Arguments", NS).text == "--scheduled-restart"


def test_xml_safety_settings():
    root = _xml(system=True, program="x.exe", arguments="")
    get = lambda tag: root.find(f".//t:{tag}", NS).text  # noqa: E731
    assert get("MultipleInstancesPolicy") == "IgnoreNew"           # вторую копию не запускать
    assert get("DisallowStartIfOnBatteries") == "false" and get("StopIfGoingOnBatteries") == "false"
    assert get("StartWhenAvailable") == "false"                    # не перезапускать «задним числом» сразу после загрузки
    assert get("ExecutionTimeLimit") == "PT2H"                     # больше ожидания тишины (час) + перезапуск, но не бесконечно


def test_xml_for_user_task_and_special_characters_are_escaped():
    root = _xml(system=False, program=r"C:\A & B\<x>.exe", arguments='"C:\\m & n.py" --scheduled-restart')
    assert root.find(".//t:LogonType", NS).text == "InteractiveToken" and root.find(".//t:UserId", NS) is None
    assert root.find(".//t:Command", NS).text == r"C:\A & B\<x>.exe"
    assert root.find(".//t:Arguments", NS).text == '"C:\\m & n.py" --scheduled-restart'


def test_command_for_exe_and_for_sources(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Kassa\PIoTHelper.exe")
    assert schedule.command() == (r"C:\Kassa\PIoTHelper.exe", "--scheduled-restart")
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    prog, args = schedule.command()
    assert prog == sys.executable and args.endswith('main.py" --scheduled-restart')


# ---------- управление задачей (schtasks подменён) ----------

class FakeRun:
    def __init__(self, code=0, out=b"", err=b""):
        self.returncode, self.stdout, self.stderr = code, out, err


def test_install_passes_xml_file_and_cleans_up(monkeypatch, isolated_app):
    seen = {}

    def fake(args, timeout=40):
        seen["args"] = args
        xml_path = args[args.index("/XML") + 1]
        seen["xml"] = open(xml_path, "rb").read()
        seen["path"] = xml_path
        return FakeRun()
    monkeypatch.setattr(schedule, "_schtasks", fake)
    msg = schedule.install("05:00", program="x.exe", arguments="--scheduled-restart")
    assert "каждый день в 05:00" in msg
    assert seen["args"][:4] == ["/Create", "/TN", schedule.TASK_NAME, "/XML"] and seen["args"][-1] == "/F"
    assert seen["xml"][:2] == b"\xff\xfe"                         # UTF-16 с BOM: так schtasks читает XML надёжно
    import os
    assert not os.path.exists(seen["path"])                       # временный файл удалён


def test_install_rejects_bad_time_without_calling_schtasks(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: pytest.fail("не должен вызываться"))
    with pytest.raises(ValueError, match="ЧЧ:ММ"):
        schedule.install("25:00")


def test_install_reports_schtasks_error_text(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(1, err="ОШИБКА: Отказано в доступе.".encode("cp866")))
    with pytest.raises(RuntimeError, match="Отказано в доступе"):
        schedule.install("05:00", program="x.exe", arguments="")


def test_remove_is_safe_when_task_is_absent(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(1, err=b"not found"))
    monkeypatch.setattr(schedule, "status", lambda name=schedule.TASK_NAME: None)
    assert "удалена" in schedule.remove()


def test_remove_raises_if_task_still_exists(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(1, err=b"access denied"))
    monkeypatch.setattr(schedule, "status", lambda name=schedule.TASK_NAME: {})
    with pytest.raises(RuntimeError, match="access denied"):
        schedule.remove()


def test_status_none_when_task_missing(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(1))
    assert schedule.status() is None


def test_status_parses_powershell_answer(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(0))
    monkeypatch.setattr(fixes, "ps", lambda *a, **k: json.dumps({
        "State": "Ready", "Program": r"C:\K\PIoTHelper.exe", "Arguments": "--scheduled-restart", "Start": "2026-01-01T05:00:00",
        "LastRun": "2026-10-22T05:00:03", "LastResult": 0, "NextRun": "2026-10-24T05:00:00"}))
    st = schedule.status()
    assert (st["time"], st["state"], st["last_result"]) == ("05:00", "Ready", 0)
    assert st["last_run"] == "2026-10-22T05:00:03" and st["next_run"] == "2026-10-24T05:00:00"


def test_status_never_ran_has_no_last_run(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(0))
    monkeypatch.setattr(fixes, "ps", lambda *a, **k: json.dumps({
        "State": "Ready", "Program": "x", "Arguments": "", "Start": "2026-01-01T05:00:00", "LastRun": "1999-11-30T00:00:00",
        "LastResult": schedule.NEVER_RAN, "NextRun": "2026-10-24T05:00:00"}))
    assert schedule.status()["last_run"] is None


def test_status_without_scheduler_cmdlets_still_reports_existence(monkeypatch):
    monkeypatch.setattr(schedule, "_schtasks", lambda *a, **k: FakeRun(0))
    monkeypatch.setattr(fixes, "ps", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("командлет не найден")))
    st = schedule.status()
    assert st is not None and st["time"] is None


def test_points_to_this_program(monkeypatch):
    monkeypatch.setattr(schedule, "command", lambda: (r"C:\K\PIoTHelper.exe", "--scheduled-restart"))
    assert schedule.points_to_this_program({"program": r"c:\k\piothelper.exe"})
    assert schedule.points_to_this_program({"program": r'"C:\K\PIoTHelper.exe"'})
    assert not schedule.points_to_this_program({"program": r"C:\Old\PIoTHelper.exe"})
    assert not schedule.points_to_this_program({"program": None})


def test_real_task_scheduler_roundtrip_for_current_user():
    """Настоящий Планировщик: создать, прочитать, сменить время, удалить (от текущего пользователя, права админа не нужны)."""
    name = f"PIoTHelperTest-{uuid.uuid4().hex[:8]}"
    cmd = r"C:\Windows\System32\cmd.exe"
    try:
        assert schedule.status(name) is None
        schedule.install("05:00", system=False, name=name, program=cmd, arguments="/c exit 0")
        st = schedule.status(name)
        assert st is not None and st["time"] == "05:00" and st["program"].lower() == cmd.lower()
        assert st["last_run"] is None and st["next_run"]
        schedule.install("05:30", system=False, name=name, program=cmd, arguments="/c exit 0")
        assert schedule.status(name)["time"] == "05:30"
    finally:
        schedule.remove(name)
    assert schedule.status(name) is None


# ---------- режим --scheduled-restart ----------

class Env:
    def __init__(self, monkeypatch, cfg):
        self.cfg, self.sleeps, self.lines, self.runs = cfg, [], [], []
        self.chain = [{"name": "esm-orchestrator", "display": "ESM: Orchestrator", "status": "Running", "start": "Automatic"}]
        self.recent = []            # что вернёт «проверки марок за последние минуты»
        self.result = restart.RESTORED_TEXT + ": всё хорошо.\nВремя: 30 с."
        monkeypatch.setattr(restart, "resolve_chain", lambda c, key="restart_chain": (self.chain, []))
        monkeypatch.setattr(scheduled, "recent_checks", lambda c: list(self.recent() if callable(self.recent) else self.recent))
        monkeypatch.setattr(restart, "run_chain", self._run_chain)

    def _run_chain(self, chain, cfg, say):
        self.runs.append(chain)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def go(self, **kw):
        return scheduled.run(cfg=self.cfg, sleep=self.sleeps.append, say=self.lines.append, lock=False, **kw)


@pytest.fixture
def env(monkeypatch, cfg):
    return Env(monkeypatch, cfg)


def test_idle_store_gets_restarted_and_result_saved(env):
    assert env.go() == scheduled.EXIT_OK
    assert len(env.runs) == 1 and env.sleeps == []
    state = scheduled.load_state()
    assert state["ok"] is True and state["skipped"] is False


def test_waits_for_a_pause_in_sales_then_restarts(env):
    answers = iter([[object()], [object()], []])
    env.recent = lambda: next(answers)
    assert env.go() == scheduled.EXIT_OK
    assert env.sleeps == [scheduled.WAIT_STEP_MINUTES * 60] * 2 and len(env.runs) == 1


def test_skips_the_night_when_sales_never_stop(env):
    env.recent = [object()]
    env.cfg["scheduled_restart_wait_minutes"] = 15
    assert env.go() == scheduled.EXIT_BUSY
    assert env.runs == [] and len(env.sleeps) == 3
    state = scheduled.load_state()
    assert state["skipped"] is True and "Пропущено" in state["message"]


def test_no_services_found(env):
    env.chain = []
    assert env.go() == scheduled.EXIT_NO_SERVICES
    assert env.runs == [] and scheduled.load_state()["ok"] is False


def test_restart_exception_gives_error_code_and_state(env):
    env.result = RuntimeError("служба не остановилась")
    assert env.go() == scheduled.EXIT_ERROR
    assert scheduled.load_state()["ok"] is False and "не остановилась" in scheduled.load_state()["message"]


def test_unconfirmed_connection_is_an_error_exit(env):
    env.result = "Службы запущены, но ЕСМ пока сообщает о проблемах связи."
    assert env.go() == scheduled.EXIT_ERROR
    assert scheduled.load_state()["ok"] is False


def test_dry_run_never_touches_services_or_state(env):
    assert env.go(dry_run=True) == scheduled.EXIT_OK
    env.recent = [object()]
    assert env.go(dry_run=True) == scheduled.EXIT_BUSY
    assert env.runs == [] and env.sleeps == [] and scheduled.load_state() is None


def test_single_instance_mutex(monkeypatch):
    monkeypatch.setattr(scheduled, "MUTEX_NAME", f"Global\\PIoTHelperTest{uuid.uuid4().hex}")
    monkeypatch.setattr(scheduled, "_mutex", None)
    assert scheduled.acquire_single_instance() is True
    assert scheduled.acquire_single_instance() is False


def test_second_instance_exits_with_code_4(monkeypatch, cfg):
    monkeypatch.setattr(scheduled, "acquire_single_instance", lambda: False)
    lines = []
    assert scheduled.run(cfg=cfg, say=lines.append) == scheduled.EXIT_RUNNING and "уже выполняется" in lines[0]


def test_main_logs_unexpected_errors_and_returns_error_code(monkeypatch, isolated_app):
    monkeypatch.setattr(scheduled, "run", lambda dry_run=False: (_ for _ in ()).throw(OSError("диск недоступен")))
    assert scheduled.main([]) == scheduled.EXIT_ERROR
    assert "диск недоступен" in (isolated_app / "error.log").read_text(encoding="utf-8")


def test_log_is_written_and_capped(isolated_app, monkeypatch):
    monkeypatch.setattr(scheduled, "LOG_LIMIT", 1000)
    for i in range(60):
        scheduled.log(f"строка номер {i} " + "x" * 50)
    text = scheduled.log_path().read_text(encoding="utf-8")
    assert len(text) < 3000 and "номер 59" in text


def test_recent_checks_use_the_quiet_window(monkeypatch, cfg):
    seen = {}
    monkeypatch.setattr(stats, "collect", lambda c, hours: seen.setdefault("hours", hours) and [])
    cfg["scheduled_restart_quiet_minutes"] = 12
    scheduled.recent_checks(cfg)
    assert seen["hours"] == pytest.approx(0.2)


# ---------- находки диагностики ----------

def _status(**kw):
    base = {"state": "Ready", "program": r"C:\K\PIoTHelper.exe", "arguments": "--scheduled-restart", "time": "05:00",
            "last_run": "2026-10-22T05:00:03", "last_result": 0, "next_run": "2026-10-24T05:00:00"}
    base.update(kw)
    return base


@pytest.fixture
def nightly(monkeypatch):
    monkeypatch.setattr(schedule, "command", lambda: (r"C:\K\PIoTHelper.exe", "--scheduled-restart"))


def test_no_finding_when_nightly_restart_is_off(monkeypatch, cfg):
    monkeypatch.setattr(schedule, "status", lambda *a: None)
    assert diagnose.check_nightly_restart(cfg) == []


def test_finding_ok_when_healthy(monkeypatch, cfg, nightly):
    monkeypatch.setattr(schedule, "status", lambda *a: _status())
    f = diagnose.check_nightly_restart(cfg)[0]
    assert f.level == "ok" and "05:00" in f.title


def test_finding_warns_when_program_was_moved(monkeypatch, cfg, nightly):
    monkeypatch.setattr(schedule, "status", lambda *a: _status(program=r"C:\Old\PIoTHelper.exe"))
    f = diagnose.check_nightly_restart(cfg)[0]
    assert f.level == "warn" and "другой файл" in f.title


def test_finding_warns_when_last_run_failed(monkeypatch, cfg, nightly):
    monkeypatch.setattr(schedule, "status", lambda *a: _status(last_result=2147942402))
    f = diagnose.check_nightly_restart(cfg)[0]
    assert f.level == "warn" and "0x80070002" in f.title


def test_finding_ignores_never_ran_code_and_skipped_nights(monkeypatch, cfg, nightly):
    monkeypatch.setattr(schedule, "status", lambda *a: _status(last_run=None, last_result=schedule.NEVER_RAN))
    assert diagnose.check_nightly_restart(cfg)[0].level == "ok"
    scheduled.save_state(True, True, "Пропущено: идут продажи")
    assert diagnose.check_nightly_restart(cfg)[0].level == "ok"        # пропуск из-за продаж — не поломка


def test_finding_warns_when_last_restart_failed(monkeypatch, cfg, nightly):
    monkeypatch.setattr(schedule, "status", lambda *a: _status())
    scheduled.save_state(False, False, "службы не найдены")
    f = diagnose.check_nightly_restart(cfg)[0]
    assert f.level == "warn" and "службы не найдены" in f.detail
