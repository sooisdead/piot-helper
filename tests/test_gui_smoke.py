"""Дымовой тест окна: оно строится, вкладки на месте, ключевые сценарии не падают. Окно создаётся реально (нужен рабочий стол).

Корневое окно Tk создаётся один раз на модуль: повторное создание Tk в одном процессе после destroy() нестабильно.
"""
import time
import tkinter as tk
from datetime import datetime

import pytest

from piothelper import diagnose, knowledge, monitor
from piothelper.diagnose import Finding


@pytest.fixture(scope="module")
def _root():
    from piothelper import gui
    try:
        a = gui.App()
    except tk.TclError as e:
        pytest.skip(f"нет графической сессии для tkinter: {e}")
    a.withdraw()
    yield a
    try:
        a.shutdown()
    except tk.TclError:
        pass


@pytest.fixture
def app(_root, monkeypatch):
    # длинные проверки подменяем, чтобы тест был быстрым и не зависел от сети и служб
    monkeypatch.setattr(diagnose, "run", lambda cfg: [Finding("fail", "Тестовая проблема"), Finding("ok", "Норма")])
    _reset(_root)
    yield _root
    _reset(_root)


def _reset(a):
    a.cfg["monitor_enabled"] = False
    a.settings_tab.enabled.set(False)
    a.apply_monitor_settings()
    a.fix_tab.findings = []
    a.withdraw()


def _pump(app, seconds=3.0, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not until():
        app.update()
        time.sleep(0.02)


def test_all_tabs_exist(app):
    nb = app.nametowidget(app.winfo_children()[0])
    titles = [nb.tab(t, "text") for t in nb.tabs()]
    assert titles == ["Сводка", "Исправления", "Хронология", "Пути", "Службы", "Порты", "Порт ККМ", "Логи", "Настройки"]


def test_diagnostics_show_findings_and_diff_from_previous_run(app):
    tab = app.fix_tab
    tab.run()
    _pump(app, until=lambda: tab.findings)
    assert [f.title for f in tab.findings] == ["Тестовая проблема", "Норма"]
    assert "Проблем: 1" in tab.status.cget("text")
    tab.findings = []
    tab.run()
    _pump(app, until=lambda: "С прошлой диагностики" in tab.detail.get("1.0", "end"))
    assert "С прошлой диагностики" in tab.detail.get("1.0", "end")


def test_timeline_tab_builds_from_log(app, tmp_path):
    log = tmp_path / "esm.log"
    log.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERR Ошибка соединения", encoding="utf-8")
    app.cfg["paths"] = [{"name": "esm", "kind": "log", "path": str(log)}]
    tab = app.timeline_tab
    tab.hours.set("2")
    tab.events = []
    tab.build()
    _pump(app, until=lambda: tab.events)
    assert len(tab.events) == 1 and len(tab.tree.get_children()) == 1


def test_failing_result_handler_does_not_stop_the_queue(app, isolated_app):
    done = []

    def bad(res, err):
        raise RuntimeError("обработчик упал")
    app._q.put((bad, None, None))
    app._q.put((lambda res, err: done.append("второй"), None, None))
    _pump(app, until=lambda: done)
    assert done == ["второй"]                                             # очередь пережила сбой
    assert "обработчик упал" in (isolated_app / "error.log").read_text(encoding="utf-8")
    later = []
    app._q.put((lambda res, err: later.append(1), None, None))            # и опрос продолжает работать
    _pump(app, until=lambda: later)
    assert later == [1]


def test_unhandled_window_error_is_logged_and_shown_once(app, isolated_app, monkeypatch):
    from tkinter import messagebox
    shown = []
    monkeypatch.setattr(messagebox, "showerror", lambda title, msg, **k: shown.append(msg))
    app._error_shown = False
    app.report_callback_exception(RuntimeError, RuntimeError("упал обработчик кнопки"), None)
    app.report_callback_exception(RuntimeError, RuntimeError("упал ещё раз"), None)
    assert len(shown) == 1 and "упал обработчик кнопки" in shown[0] and "error.log" in shown[0]
    assert "упал ещё раз" in (isolated_app / "error.log").read_text(encoding="utf-8")   # в журнал попадают обе


def _nightly_info(**kw):
    info = {"state": "Ready", "program": r"C:\Old\PIoTHelper.exe", "arguments": "--scheduled-restart", "time": "05:00",
            "last_run": "2026-10-22T05:00:03", "last_result": 0, "next_run": "2026-10-24T05:00:00"}
    info.update(kw)
    return info


def test_nightly_status_is_shown_with_warning_about_moved_program(app, monkeypatch):
    from piothelper import schedule
    monkeypatch.setattr(schedule, "status", lambda *a: _nightly_info())
    tab = app.settings_tab
    tab.night_on.set(False)
    tab.refresh_nightly()
    _pump(app, until=lambda: "Включён" in tab.night_status.cget("text"))
    text = tab.night_status.cget("text")
    assert tab.night_on.get() is True and "каждый день в 05:00" in text and "2026-10-24 05:00" in text
    assert "ВНИМАНИЕ" in text and r"C:\Old\PIoTHelper.exe" in text            # задача указывает на другой файл


def test_nightly_status_when_off(app, monkeypatch):
    from piothelper import schedule
    monkeypatch.setattr(schedule, "status", lambda *a: None)
    tab = app.settings_tab
    tab.night_on.set(True)
    tab.refresh_nightly()
    _pump(app, until=lambda: tab.night_status.cget("text") == "Выключен.")
    assert tab.night_on.get() is False


def test_enabling_nightly_without_admin_offers_relaunch_and_reverts(app, monkeypatch):
    from tkinter import messagebox
    from piothelper import gui_extra, schedule
    asked = []
    monkeypatch.setattr(gui_extra.portowner, "is_admin", lambda: False)
    monkeypatch.setattr(messagebox, "askyesno", lambda title, msg, **k: asked.append(title) or False)
    monkeypatch.setattr(schedule, "install", lambda *a, **k: pytest.fail("без прав задачу создавать нельзя"))
    tab = app.settings_tab
    tab.night_time.set("05:00")
    tab.night_on.set(True)
    tab.on_nightly_toggle()
    assert asked == ["Нужны права администратора"] and tab.night_on.get() is False


def test_enabling_nightly_with_admin_creates_the_task_and_saves_time(app, monkeypatch):
    from tkinter import messagebox
    from piothelper import gui_extra, schedule
    created = []
    monkeypatch.setattr(gui_extra.portowner, "is_admin", lambda: True)
    monkeypatch.setattr(messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(schedule, "install", lambda t, *a, **k: created.append(t) or "создана")
    monkeypatch.setattr(schedule, "status", lambda *a: _nightly_info(program=schedule.command()[0], time="04:30"))
    tab = app.settings_tab
    tab.night_time.set("04:30")
    tab.night_on.set(True)
    tab.on_nightly_toggle()
    _pump(app, until=lambda: created and app.cfg.get("scheduled_restart_time") == "04:30")
    assert created == ["04:30"] and app.cfg["scheduled_restart_time"] == "04:30"


def test_nightly_rejects_bad_time_without_touching_scheduler(app, monkeypatch):
    from tkinter import messagebox
    from piothelper import schedule
    warned = []
    monkeypatch.setattr(messagebox, "showwarning", lambda t, m, **k: warned.append(m))
    monkeypatch.setattr(schedule, "install", lambda *a, **k: pytest.fail("не должен вызываться"))
    tab = app.settings_tab
    tab.night_time.set("25:61")
    tab.night_on.set(True)
    tab.on_nightly_toggle()
    assert warned and tab.night_on.get() is False


def test_nightly_dry_run_button_reports_without_restarting(app, monkeypatch):
    from tkinter import messagebox
    from piothelper import restart
    shown = []
    monkeypatch.setattr(messagebox, "showinfo", lambda t, m, **k: shown.append((t, m)))
    monkeypatch.setattr(restart, "resolve_chain", lambda c, key="restart_chain": (
        [{"name": "esm-orchestrator", "display": "ESM: Orchestrator", "status": "Running", "start": "Automatic"}], []))
    monkeypatch.setattr(restart, "run_chain", lambda *a, **k: pytest.fail("репетиция не должна перезапускать службы"))
    from piothelper import scheduled
    monkeypatch.setattr(scheduled, "recent_checks", lambda c: [])
    app.settings_tab.test_nightly()
    _pump(app, until=lambda: shown)
    assert shown and "службы не тронуты" in shown[0][0] and "ESM: Orchestrator" in shown[0][1]


def test_settings_show_rules_info_and_reload(app):
    tab = app.settings_tab
    assert f"Версия правил: {knowledge.RULES_INFO['version']}" in tab.rules_label.cget("text")
    tab.reload_rules()


def test_enabling_monitor_creates_tray_and_disabling_removes_it(app):
    tab = app.settings_tab
    tab.enabled.set(True)
    tab.on_monitor_changed()
    assert app.cfg["monitor_enabled"] is True and app.monitor.running
    assert app.tray is not None
    tab.enabled.set(False)
    tab.on_monitor_changed()
    _pump(app, 0.3)
    assert app.tray is None and app.cfg["monitor_enabled"] is False


def test_closing_window_hides_to_tray_when_monitor_enabled(app):
    app.settings_tab.enabled.set(True)
    app.settings_tab.on_monitor_changed()
    app.on_close()
    assert app.state() == "withdrawn" and app.monitor.running
    app.show_window()
    assert app.state() == "normal"


def test_monitor_notifications_reach_the_tray(app, monkeypatch):
    sent = []
    app.settings_tab.enabled.set(True)
    app.settings_tab.on_monitor_changed()
    monkeypatch.setattr(app.tray, "notify", lambda *a: sent.append(a))
    monkeypatch.setattr(monitor, "FIRST_RUN_DELAY", 0)
    app.monitor.run_once()
    assert sent and sent[0][2] == "fail" and "Тестовая проблема" in sent[0][1]
