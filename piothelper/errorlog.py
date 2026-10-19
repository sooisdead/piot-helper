"""Журнал ошибок самой программы: error.log рядом с программой.

Программа без консоли (exe), поэтому необработанная ошибка иначе была бы невидима. Сюда пишутся: необработанные
исключения (в окне, в фоновых потоках, при запуске) и ошибки фоновых задач. В файл добавляются версия программы, Windows и
Python — этого достаточно, чтобы понять, чем отличается «странная» касса (например, Windows 8.1 или повреждённый WMI).
"""
import ctypes
import os
import platform
import sys
import threading
import traceback
from datetime import datetime

from . import __version__
from .config import app_dir

MAX_BYTES = 200_000
_lock = threading.Lock()


def path():
    return app_dir() / "error.log"


def environment() -> str:
    frozen = "exe" if getattr(sys, "frozen", False) else "исходники"
    return (f"PIoT Helper {__version__} ({frozen}); {platform.platform()} (build {platform.version()}); "
            f"Python {platform.python_version()} {platform.architecture()[0]}; админ: {_is_admin()}")


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def log(exc: BaseException | None = None, where: str = "") -> str:
    """Записывает исключение (по умолчанию — текущее) и возвращает его краткое описание. Сама никогда не падает."""
    try:
        if exc is None:
            exc = sys.exc_info()[1]
        if exc is None:
            return ""
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with _lock:
            p = path()
            if p.exists() and p.stat().st_size > MAX_BYTES:  # оставляем хвост, чтобы файл не рос бесконечно
                p.write_text(p.read_text(encoding="utf-8", errors="replace")[-MAX_BYTES // 2:], encoding="utf-8")
            with open(p, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} {where} =====\n{environment()}\n{text}")
        return f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001
        return ""


def install_hooks() -> None:
    """Необработанные исключения основного и фоновых потоков попадают в журнал."""
    def main_hook(exc_type, exc, tb):
        log(exc, "необработанное исключение")
        sys.__excepthook__(exc_type, exc, tb)

    def thread_hook(args):
        if args.exc_type is not SystemExit:
            log(args.exc_value, f"поток {getattr(args.thread, 'name', '?')}")

    sys.excepthook = main_hook
    threading.excepthook = thread_hook


def fatal(exc: BaseException | None = None) -> None:
    """Ошибка при запуске (до появления окна): журнал + системное окно с текстом и путём к файлу."""
    short = log(exc, "ошибка при запуске") or "неизвестная ошибка"
    message = (f"PIoT Helper не смог запуститься.\n\n{short}\n\nПодробности записаны в файл:\n{path()}\n"
               f"Пришлите его разработчику или откройте Issue на GitHub.")
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "PIoT Helper", 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001
        pass
    os._exit(1)
