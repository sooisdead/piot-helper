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
