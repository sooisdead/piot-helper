"""Режим --scheduled-restart: ночной перезапуск служб ЕСМ (запускается Планировщиком Windows, без окна).

Порядок: найти службы → дождаться паузы в продажах → перезапустить в безопасном порядке → записать итог.
Паузой считается отсутствие проверок марок в логах ЕСМ за scheduled_restart_quiet_minutes. Не дождались за
scheduled_restart_wait_minutes — эта ночь пропускается (лучше пропустить, чем оставить кассу без проверки марок).
Если логи ЕСМ не заданы на вкладке «Пути», активность определить нельзя и перезапуск выполняется без ожидания.

Коды выхода: 0 — выполнено, 1 — ошибка перезапуска, 2 — службы не найдены, 3 — пропущено (идут продажи), 4 — уже выполняется.
"""
import ctypes
import json
import time
from datetime import datetime
from typing import Callable

from . import config, errorlog, restart, stats
from .config import app_dir

EXIT_OK, EXIT_ERROR, EXIT_NO_SERVICES, EXIT_BUSY, EXIT_RUNNING = 0, 1, 2, 3, 4
WAIT_STEP_MINUTES = 5
LOG_LIMIT = 200_000
MUTEX_NAME = "Global\\PIoTHelperScheduledRestart"
_ERROR_ALREADY_EXISTS = 183
_mutex = None  # держим дескриптор до конца процесса


def log_path():
    return app_dir() / "scheduled_restart.log"


def state_path():
    return app_dir() / "scheduled_restart.json"


def log(text: str) -> None:
    try:
        p = log_path()
        if p.exists() and p.stat().st_size > LOG_LIMIT:
            p.write_text(p.read_text(encoding="utf-8", errors="replace")[-LOG_LIMIT // 2:], encoding="utf-8")
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def save_state(ok: bool, skipped: bool, message: str) -> None:
    try:
        state_path().write_text(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "ok": ok,
                                            "skipped": skipped, "message": message}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_state() -> dict | None:
    try:
        return json.loads(state_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def acquire_single_instance() -> bool:
    """Именованный мьютекс: ночной перезапуск не должен идти одновременно с другим таким же."""
    global _mutex
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = k32.CreateMutexW(None, False, MUTEX_NAME)
    already = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
    if already or not handle:
        return False
    _mutex = handle
    return True


def recent_checks(cfg: dict) -> list:
    """Проверки марок за последние quiet-минуты по логам ЕСМ (пусто — тихо или логи не заданы)."""
    return stats.collect(cfg, cfg.get("scheduled_restart_quiet_minutes", 10) / 60)


def run(dry_run: bool = False, cfg: dict | None = None, sleep: Callable[[float], None] = time.sleep,
        say: Callable[[str], None] = log, lock: bool = True) -> int:
    cfg = cfg or config.load()
    if lock and not acquire_single_instance():
        say("Пропущено: другой ночной перезапуск уже выполняется.")
        return EXIT_RUNNING
    say("=== Ночной перезапуск служб ЕСМ" + (" (проверка без перезапуска)" if dry_run else "") + " ===")
    chain, missing = restart.resolve_chain(cfg, "scheduled_restart_chain")
    if not chain:
        msg = f"Службы не найдены (шаблоны: {', '.join(cfg['scheduled_restart_chain'])}). Проверьте scheduled_restart_chain."
        say(msg)
        if not dry_run:
            save_state(False, False, msg)
        return EXIT_NO_SERVICES
    say("Будут перезапущены: " + ", ".join(s["display"] for s in chain)
        + (f". Не найдены: {', '.join(missing)}" if missing else ""))

    waited, limit = 0, cfg.get("scheduled_restart_wait_minutes", 60)
    quiet = cfg.get("scheduled_restart_quiet_minutes", 10)
    while True:
        recent = recent_checks(cfg)
        if not recent:
            break
        if dry_run:
            say(f"Сейчас идут продажи (проверок марок за {quiet} мин: {len(recent)}): в реальном запуске программа подождала бы "
                f"паузы до {limit} мин. Службы не тронуты.")
            return EXIT_BUSY
        if waited >= limit:
            msg = f"Пропущено: проверки марок шли и через {limit} мин ожидания (за последние {quiet} мин: {len(recent)})."
            say(msg)
            save_state(True, True, msg)
            return EXIT_BUSY
        say(f"За последние {quiet} мин было проверок марок: {len(recent)}. Жду {WAIT_STEP_MINUTES} мин (ожидание {waited}/{limit} мин).")
        sleep(WAIT_STEP_MINUTES * 60)
        waited += WAIT_STEP_MINUTES

    if dry_run:
        say(f"Продаж не видно (за {quiet} мин проверок марок нет): перезапуск можно выполнять. Службы не тронуты.")
        return EXIT_OK
    try:
        result = restart.run_chain(chain, cfg, say)
    except Exception as e:  # noqa: BLE001
        errorlog.log(e, "ночной перезапуск")
        say(f"ОШИБКА: {e}")
        save_state(False, False, str(e))
        return EXIT_ERROR
    say(result.replace("\n", " | "))
    ok = restart.RESTORED_TEXT in result
    save_state(ok, False, (restart.RESTORED_TEXT if ok else "перезапуск выполнен, но связь не подтверждена; см. журнал"))
    return EXIT_OK if ok else EXIT_ERROR


def main(args: list[str]) -> int:
    """Точка входа режима --scheduled-restart [--dry-run]. Окон не показывает: задача идёт в сеансе без рабочего стола."""
    try:
        return run(dry_run="--dry-run" in args)
    except Exception as e:  # noqa: BLE001
        errorlog.log(e, "ночной перезапуск (запуск)")
        log(f"ОШИБКА запуска: {e}")
        return EXIT_ERROR
