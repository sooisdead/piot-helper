"""Хронология: события из всех логов одной лентой по времени, с повторами в одну строку и пометками по базе знаний."""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import knowledge, logs

LEVEL_RANK = {"info": 0, "warn": 1, "error": 2}
_RULE_LEVEL = {"fail": "error", "warn": "warn", "info": "info"}
MAX_SOURCE_FILES = 3  # по папке берём 3 самых свежих файла (учитывает ротацию)
COLLAPSE_GAP = timedelta(minutes=10)  # одинаковые записи с паузой меньше этой сворачиваются в одну строку


@dataclass
class Event:
    time: datetime
    last_time: datetime
    source: str
    level: str
    text: str
    rule: str = ""     # название сработавшего правила базы знаний
    count: int = 1     # сколько подряд одинаковых записей свёрнуто в эту строку


def _combined_rules():
    """Все правила одним регулярным выражением: на каждую строку один вызов вместо десятков."""
    parts, titles = [], {}
    for i, r in enumerate(knowledge.RULES):
        parts.append(f"(?P<r{i}>{r.pattern})")
        titles[f"r{i}"] = r
    return (re.compile("|".join(parts), re.I) if parts else None), titles


def _normalize(text: str) -> str:
    text = re.sub(r"\d+", "N", text)
    return re.sub(r"\s+", " ", text).strip()[:160]


def _source_name(entry_name: str, file) -> str:
    return f"{entry_name}: {file.name}" if entry_name not in file.name else file.name


def collect(cfg: dict, hours: float, min_level: str = "warn", needle: str = "", limit: int = 5000) -> list[Event]:
    """События за последние hours часов уровня не ниже min_level, от старых к новым."""
    border = datetime.now() - timedelta(hours=hours)
    classifier = logs.Classifier(cfg)
    rx, rules = _combined_rules()
    wanted = LEVEL_RANK[min_level]
    needle = needle.strip().lower()
    events: list[Event] = []
    for entry in cfg["paths"]:
        if entry["kind"] != "log":
            continue
        for f in logs.list_log_files(entry["path"].strip())[:MAX_SOURCE_FILES]:
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < border:
                    continue  # файл не менялся в окне — событий в нём нет
                lines = logs.read_tail(f, cfg["tail_kb"])
            except OSError:
                continue
            source = _source_name(entry["name"], f)
            current = None
            latest: dict[tuple, Event] = {}  # последняя запись каждого текста (чередующиеся повторы тоже сворачиваются)
            for ln in lines:
                t = logs.line_time(ln)
                if t:
                    current = t
                if current is None or current < border or not ln.strip():
                    continue
                level, rule = classifier.level(ln), ""
                if rx:
                    m = rx.search(ln)
                    if m and m.lastgroup:
                        r = rules[m.lastgroup]
                        rule = r.title
                        level = max(level, _RULE_LEVEL[r.level], key=LEVEL_RANK.get)
                if LEVEL_RANK[level] < wanted or (needle and needle not in ln.lower()):
                    continue
                text = ln.strip()
                key = (level, _normalize(text))
                ev = latest.get(key)
                if ev is not None and current - ev.last_time <= COLLAPSE_GAP:  # тот же текст — сворачиваем
                    ev.count += 1
                    ev.last_time = max(ev.last_time, current)
                    continue
                ev = Event(current, current, source, level, text[:600], rule)
                events.append(ev)
                latest[key] = ev
    events.sort(key=lambda e: e.time)
    return events[-limit:]


def format_events(events: list[Event]) -> str:
    """Текстовая версия ленты (для сохранения и для отчёта)."""
    out = []
    for e in events:
        times = f"{e.time:%d.%m %H:%M:%S}" + (f"–{e.last_time:%H:%M:%S}" if e.count > 1 else "")
        rule = f" [{e.rule}]" if e.rule else ""
        count = f" (×{e.count})" if e.count > 1 else ""
        out.append(f"{times}  {e.level.upper():5s}  {e.source}{rule}{count}\n    {e.text}")
    return "\n".join(out) + ("\n" if out else "")
