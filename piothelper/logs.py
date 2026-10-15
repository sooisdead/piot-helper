"""Чтение и классификация логов."""
import re
from datetime import datetime
from pathlib import Path

LOG_SUFFIXES = {".log", ".txt", ".json", ".jsonl", ".out", ".err", ".trace"}


def is_log_file(p: Path) -> bool:
    return p.suffix.lower() in LOG_SUFFIXES or ".log" in p.name.lower()


def list_log_files(path: str) -> list[Path]:
    """Файлы логов по пути: сам файл или содержимое папки, новые первыми."""
    if not path:
        return []
    p = Path(path)
    try:
        if p.is_file():
            return [p]
        if p.is_dir():
            files = [f for f in p.rglob("*") if f.is_file() and is_log_file(f)]
            return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:200]
    except OSError:
        pass
    return []


def read_tail(path: Path, max_kb: int) -> list[str]:
    """Последние max_kb килобайт файла строками. Кодировка: utf-8, иначе cp1251."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        start = max(0, size - max_kb * 1024)
        f.seek(start)
        data = f.read()
    if start:
        data = data.split(b"\n", 1)[-1]  # отбрасываем оборванную первую строку
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace").splitlines()
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError:
        text = data.decode("cp1251", errors="replace")
    return text.splitlines()


# «Ошибки нет» в разных видах: "error" : 0, errorCode=0, «Ошибок нет» — это не ошибки.
NO_ERROR = re.compile(r'"?(?:error|errorcode)"?\s*[:=]\s*0\b|ошибок нет', re.I)
TIMESTAMP = re.compile(r"(\d{4})[-./](\d{2})[-./](\d{2})[ T](\d{2}):(\d{2}):(\d{2})")


def line_time(line: str) -> datetime | None:
    m = TIMESTAMP.search(line)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def recent_lines(lines: list[str], hours: float) -> tuple[list[str], bool]:
    """Строки за последние hours часов. Строки без метки времени (продолжение JSON и т.п.) наследуют
    время предыдущей. Если в файле нет ни одной метки, возвращаются все строки (False в ответе)."""
    border = datetime.now().timestamp() - hours * 3600
    out, current, seen = [], None, False
    for ln in lines:
        t = line_time(ln)
        if t:
            current, seen = t.timestamp(), True
        if current is None or current >= border:
            out.append(ln)
    return (out, True) if seen else (lines, False)


# Явный уровень записи: JSON {"level":"info"}, «INF/DBG/ERR/WRN», CouchDB «[error]», Erlang «... error <0.1.0>:».
EXPLICIT_LEVEL = re.compile(
    r'"level"\s*:\s*"(\w+)"|^\W*\[(error|warning|warn|notice|info|debug)\]|'
    r'\d[ T]\d{2}:\d{2}:\d{2}[.,\d]*\s+(?:T:\s*\w+\s+)*\[?(TRACE|DBG|DEBUG|INF|INFO|NOTICE|WRN|WARN|WARNING|ERR|ERROR|FATAL|PANIC)\b|'
    r'Z\s+(error|warning|notice|info|debug)\s+<', re.I)
_LEVEL_MAP = {"err": "error", "error": "error", "fatal": "error", "panic": "error", "crit": "error",
              "wrn": "warn", "warn": "warn", "warning": "warn"}


class Classifier:
    def __init__(self, cfg: dict):
        self.err = [re.compile(p, re.I) for p in cfg["error_patterns"]]
        self.warn = [re.compile(p, re.I) for p in cfg["warn_patterns"]]

    def level(self, line: str) -> str:
        m = EXPLICIT_LEVEL.search(line)
        if m:  # уровень указан в самой записи — ему и верим, слова «error» в названиях полей не считаются
            word = next(g for g in m.groups() if g)
            return _LEVEL_MAP.get(word.lower(), "info")
        line = NO_ERROR.sub("", line)
        if any(r.search(line) for r in self.err):
            return "error"
        if any(r.search(line) for r in self.warn):
            return "warn"
        return "info"


def file_age_minutes(path: Path) -> float:
    return (datetime.now().timestamp() - path.stat().st_mtime) / 60
