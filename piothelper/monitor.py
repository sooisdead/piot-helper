"""Фоновая проверка: раз в N минут запускает диагностику и сообщает только о НОВЫХ или ухудшившихся проблемах."""
import ctypes
import json
import threading
from datetime import datetime, timedelta
from typing import Callable

from . import diagnose, errorlog, history

FIRST_RUN_DELAY = 20       # секунд после запуска, чтобы окно успело открыться
MIN_INTERVAL_MIN = 5
LOG_LIMIT_BYTES = 200_000
STATE_KEEP = timedelta(days=7)


def _state_path():
    return history.history_dir() / "monitor_state.json"


def _load_state() -> dict[str, datetime]:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
        return {k: datetime.fromisoformat(v) for k, v in raw.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_state(state: dict[str, datetime]) -> None:
    now = datetime.now()
    state = {k: v for k, v in state.items() if now - v < STATE_KEEP}
    try:
        history.history_dir().mkdir(parents=True, exist_ok=True)
        _state_path().write_text(json.dumps({k: v.isoformat(timespec="seconds") for k, v in state.items()}), encoding="utf-8")
    except OSError:
        pass


def pick_notifications(prev: list[tuple[str, str]], findings, cfg: dict, state: dict[str, datetime],
                       now: datetime | None = None) -> list[tuple[str, str]]:
    """Какие проблемы показать пользователем: новые и ухудшившиеся, не чаще monitor_repeat_hours для одной и той же.
    Возвращает [(заголовок, уровень)]; state (ключ → время последнего уведомления) обновляется."""
    now = now or datetime.now()
    diff = history.compare(prev, [(f.level, f.title) for f in findings])
    candidates = list(diff.new) + [(t, new) for t, _old, new in diff.worse]
    allowed = {"fail", "warn"} if cfg.get("monitor_notify_warn") else {"fail"}
    pause = timedelta(hours=cfg.get("monitor_repeat_hours", 6))
    out = []
    for title, level in candidates:
        if level not in allowed:
            continue
        key = history.normalize(title)
        if key in state and now - state[key] < pause:
            continue
        state[key] = now
        out.append((title, level))
    return out


def message(notes: list[tuple[str, str]]) -> tuple[str, str, str]:
    """(заголовок, текст, уровень) для всплывающего уведомления."""
    level = "fail" if any(lvl == "fail" for _, lvl in notes) else "warn"
    title = "PIoT Helper: проблема на кассе" if len(notes) == 1 else f"PIoT Helper: новых проблем — {len(notes)}"
    text = notes[0][0] if len(notes) == 1 else f"{notes[0][0]} (и ещё {len(notes) - 1}). Откройте вкладку «Исправления»."
    return title, text[:240], level


def worst_level(findings) -> str:
    levels = {f.level for f in findings}
    return "fail" if "fail" in levels else "warn" if "warn" in levels else "ok"


class Monitor:
    """Поток фоновой проверки. get_cfg() отдаёт актуальные настройки, on_notify(title, text, level) показывает уведомление,
    on_result(findings, level) обновляет статус в окне и значке."""

    def __init__(self, get_cfg: Callable[[], dict], on_notify: Callable[[str, str, str], None],
                 on_result: Callable[[list, str], None] | None = None):
        self.get_cfg, self.on_notify, self.on_result = get_cfg, on_notify, on_result
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self.last_run: datetime | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(target=self._loop, name="piot-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run_now(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        try:  # фоновая работа не должна мешать кассе: пониженный приоритет потока
            ctypes.windll.kernel32.SetThreadPriority(ctypes.windll.kernel32.GetCurrentThread(), -1)
        except Exception:  # noqa: BLE001
            pass
        self._wake.wait(FIRST_RUN_DELAY)
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.run_once()
            except Exception as e:  # noqa: BLE001 — поток не должен умирать из-за одной ошибки
                self._log(f"ошибка проверки: {e}")
                errorlog.log(e, "фоновая проверка")
            minutes = max(MIN_INTERVAL_MIN, int(self.get_cfg().get("monitor_minutes", 15)))
            self._wake.wait(minutes * 60)

    def run_once(self) -> list:
        cfg = self.get_cfg()
        findings = diagnose.run(cfg)
        _, prev = history.latest_snapshot()
        state = _load_state()
        notes = pick_notifications(prev, findings, cfg, state)
        history.save_snapshot(findings)
        _save_state(state)
        self.last_run = datetime.now()
        level = worst_level(findings)
        self._log(f"проверок: {len(findings)}, худший уровень: {level}, уведомлений: {len(notes)}")
        if notes:
            self.on_notify(*message(notes))
        if self.on_result:
            self.on_result(findings, level)
        return findings

    def _log(self, text: str) -> None:
        try:
            path = history.history_dir().parent / "monitor.log"
            if path.exists() and path.stat().st_size > LOG_LIMIT_BYTES:
                path.write_text("", encoding="utf-8")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError:
            pass
