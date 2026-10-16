import threading
import time
import winreg
from datetime import datetime, timedelta

import pytest

from piothelper import autostart, diagnose, history, monitor, tray
from piothelper.diagnose import Finding


def F(level, title):
    return Finding(level, title)


# ---------- что показывать пользователю ----------

def test_only_new_fail_problems_are_notified_by_default(cfg):
    prev = [("fail", "Служба «A» не запущена")]
    now = [F("fail", "Служба «A» не запущена"), F("fail", "Новая беда"), F("warn", "Новое предупреждение")]
    notes = monitor.pick_notifications(prev, now, cfg, {})
    assert notes == [("Новая беда", "fail")]


def test_warnings_are_included_when_enabled(cfg):
    cfg["monitor_notify_warn"] = True
    notes = monitor.pick_notifications([], [F("warn", "Предупреждение")], cfg, {})
    assert notes == [("Предупреждение", "warn")]


def test_worsened_problem_is_notified(cfg):
    notes = monitor.pick_notifications([("warn", "Порт медленный")], [F("fail", "Порт медленный")], cfg, {})
    assert notes == [("Порт медленный", "fail")]


def test_same_problem_is_not_repeated_within_pause_but_is_after(cfg):
    state: dict = {}
    now = datetime(2026, 10, 16, 12, 0)
    assert monitor.pick_notifications([], [F("fail", "Беда")], cfg, state, now)
    assert not monitor.pick_notifications([], [F("fail", "Беда")], cfg, state, now + timedelta(hours=1))
    assert monitor.pick_notifications([], [F("fail", "Беда")], cfg, state, now + timedelta(hours=7))


def test_numbers_in_title_do_not_defeat_repeat_suppression(cfg):
    state: dict = {}
    now = datetime(2026, 10, 16, 12, 0)
    monitor.pick_notifications([], [F("fail", "Лицензия: осталось дн.: 3")], cfg, state, now)
    assert not monitor.pick_notifications([], [F("fail", "Лицензия: осталось дн.: 2")], cfg, state, now + timedelta(hours=1))


def test_resolved_or_ok_findings_do_not_notify(cfg):
    assert monitor.pick_notifications([("fail", "Беда")], [F("ok", "Всё хорошо")], cfg, {}) == []


def test_message_single_and_multiple():
    title, text, level = monitor.message([("Одна", "warn")])
    assert "проблема" in title and text == "Одна" and level == "warn"
    title, text, level = monitor.message([("Первая", "warn"), ("Вторая", "fail")])
    assert "2" in title and "и ещё 1" in text and level == "fail"


def test_worst_level():
    assert monitor.worst_level([F("ok", "a"), F("fail", "b")]) == "fail"
    assert monitor.worst_level([F("info", "a"), F("warn", "b")]) == "warn"
    assert monitor.worst_level([F("ok", "a")]) == "ok"


# ---------- поток монитора ----------

def test_run_once_saves_snapshot_notifies_once_and_reports(monkeypatch, cfg, isolated_app):
    results, notes = [], []
    monkeypatch.setattr(diagnose, "run", lambda c: [F("fail", "Беда"), F("ok", "Норма")])
    m = monitor.Monitor(lambda: cfg, lambda *a: notes.append(a), lambda f, lvl: results.append(lvl))
    m.run_once()
    m.run_once()
    assert len(notes) == 1 and notes[0][2] == "fail"         # второй раз — без повторного уведомления
    assert results == ["fail", "fail"]
    assert len(list(history.history_dir().glob("2*.json"))) == 2
    assert (isolated_app / "monitor.log").exists()


def test_background_loop_runs_and_stops(monkeypatch, cfg):
    cfg["monitor_minutes"] = 0
    monkeypatch.setattr(monitor, "FIRST_RUN_DELAY", 0)
    monkeypatch.setattr(monitor, "MIN_INTERVAL_MIN", 0)
    runs = []
    monkeypatch.setattr(diagnose, "run", lambda c: runs.append(1) or [F("ok", "ok")])
    m = monitor.Monitor(lambda: cfg, lambda *a: None)
    m.start()
    deadline = time.time() + 5
    while len(runs) < 2 and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    m._thread.join(5)
    assert len(runs) >= 2 and not m.running


def test_loop_survives_an_exception(monkeypatch, cfg):
    cfg["monitor_minutes"] = 0
    monkeypatch.setattr(monitor, "FIRST_RUN_DELAY", 0)
    monkeypatch.setattr(monitor, "MIN_INTERVAL_MIN", 0)
    calls = []

    def flaky(c):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("временный сбой")
        return [F("ok", "ok")]
    monkeypatch.setattr(diagnose, "run", flaky)
    m = monitor.Monitor(lambda: cfg, lambda *a: None)
    m.start()
    deadline = time.time() + 5
    while len(calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    m.stop()
    m._thread.join(5)
    assert len(calls) >= 2


# ---------- автозапуск (временный раздел реестра, настоящий Run не трогаем) ----------

TEST_KEY = r"Software\PIoTHelperTest\Run"


@pytest.fixture
def temp_key():
    yield TEST_KEY
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\PIoTHelperTest")
    except OSError:
        pass


def test_autostart_enable_disable(temp_key):
    assert not autostart.is_enabled(temp_key)
    autostart.set_enabled(True, temp_key)
    assert autostart.is_enabled(temp_key)
    autostart.set_enabled(False, temp_key)
    assert not autostart.is_enabled(temp_key)
    autostart.set_enabled(False, temp_key)   # повторное отключение безопасно


def test_autostart_command_points_to_tray_mode():
    cmd = autostart.command()
    assert cmd.endswith("--tray") and cmd.count('"') >= 2


# ---------- значок в трее ----------

@pytest.fixture
def icon():
    calls = {"open": 0, "check": 0, "exit": 0}
    t = tray.Tray(lambda: calls.__setitem__("open", calls["open"] + 1), lambda: calls.__setitem__("check", calls["check"] + 1),
                  lambda: calls.__setitem__("exit", calls["exit"] + 1))
    assert t.start(), "не удалось создать окно значка"
    yield t, calls
    t.stop()


def test_tray_routes_clicks_and_menu_commands(icon):
    t, calls = icon
    tray.user32.SendMessageW(t.hwnd, tray.WM_TRAY, 1, tray.WM_LBUTTONUP)
    tray.user32.SendMessageW(t.hwnd, tray.WM_TRAY, 1, tray.NIN_BALLOONUSERCLICK)
    tray.user32.SendMessageW(t.hwnd, tray.WM_COMMAND, tray.ID_CHECK, 0)
    tray.user32.SendMessageW(t.hwnd, tray.WM_COMMAND, tray.ID_EXIT, 0)
    tray.user32.SendMessageW(t.hwnd, tray.WM_COMMAND, tray.ID_OPEN, 0)
    assert calls == {"open": 3, "check": 1, "exit": 1}


def test_tray_state_and_notifications_do_not_crash(icon):
    t, _ = icon
    t.set_state("fail", "PIoT Helper: проблемы")
    t.notify("Заголовок", "Текст уведомления", "fail")
    t.set_state("ok")
    done = threading.Event()
    t._post(done.set)          # очередь обработана потоком значка
    assert done.wait(3) and t.level == "ok"


def test_tray_stop_ends_thread():
    t = tray.Tray(lambda: None, lambda: None, lambda: None)
    assert t.start()
    t.stop()
    assert not t._thread.is_alive()
