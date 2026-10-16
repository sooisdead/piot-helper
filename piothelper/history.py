"""История диагностик и сравнение: что появилось, что исправилось, что ухудшилось.

Снимки лежат в <папка программы>/history/ (последние KEEP штук) — только локально, в git не попадают.
Сравнивать можно и два отчёта-архива (diagnostics.txt внутри, либо summary.txt у старых версий).
"""
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import app_dir

KEEP = 30
RANK = {"ok": 0, "info": 1, "warn": 2, "fail": 3}
PROBLEM_RANK = 2  # «проблема» — это warn и выше; ok/info при сравнении не считаем
_LEVEL_WORDS = {"[FAIL]": "fail", "[WARN]": "warn", "[INFO]": "info", "[ OK ]": "ok"}


def history_dir() -> Path:
    return app_dir() / "history"


def normalize(title: str) -> str:
    """Ключ для сравнения: числа и метки маскирования ([SN1] → [X]) не считаются различиями."""
    title = re.sub(r"\[[A-ZА-Я0-9]+\d\]", "[X]", title)
    title = re.sub(r"\d+", "N", title)
    return re.sub(r"\s+", " ", title).strip()


@dataclass
class Diff:
    new: list[tuple[str, str]] = field(default_factory=list)            # (заголовок, уровень сейчас)
    resolved: list[tuple[str, str]] = field(default_factory=list)       # (заголовок, уровень был)
    worse: list[tuple[str, str, str]] = field(default_factory=list)     # (заголовок, было, стало)
    better: list[tuple[str, str, str]] = field(default_factory=list)
    unchanged_problems: int = 0

    @property
    def empty(self) -> bool:
        return not (self.new or self.resolved or self.worse or self.better)


def compare(old: list[tuple[str, str]], new: list[tuple[str, str]]) -> Diff:
    """old/new: списки (уровень, заголовок). Сравниваются только проблемы (warn и выше)."""
    def index(items):
        out: dict[str, tuple[str, str]] = {}
        for level, title in items:
            key = normalize(title)
            if key not in out or RANK[level] > RANK[out[key][0]]:
                out[key] = (level, title)
        return out

    a, b = index(old), index(new)
    d = Diff()
    for key, (level, title) in b.items():
        old_level = a[key][0] if key in a else "ok"
        if RANK[level] >= PROBLEM_RANK and RANK[old_level] < PROBLEM_RANK:
            d.new.append((title, level))
        elif RANK[level] > RANK[old_level] and RANK[level] >= PROBLEM_RANK:
            d.worse.append((title, old_level, level))
        elif RANK[level] < RANK[old_level] and RANK[old_level] >= PROBLEM_RANK:
            (d.better if RANK[level] >= PROBLEM_RANK else d.resolved).append(
                (title, old_level, level) if RANK[level] >= PROBLEM_RANK else (title, old_level))
        elif RANK[level] >= PROBLEM_RANK:
            d.unchanged_problems += 1
    for key, (level, title) in a.items():
        if key not in b and RANK[level] >= PROBLEM_RANK:
            d.resolved.append((title, level))
    return d


def format_diff(d: Diff, old_label: str = "прошлой проверкой", new_label: str = "сейчас") -> str:
    if d.empty and not d.unchanged_problems:
        return f"Проблем нет ни {old_label}, ни {new_label}."
    lines = [f"Изменения между {old_label} и {new_label}:"]
    sections = [
        ("Новые проблемы", [f"  + [{lvl.upper()}] {t}" for t, lvl in d.new]),
        ("Стало хуже", [f"  ↑ {t}: {o.upper()} → {n.upper()}" for t, o, n in d.worse]),
        ("Исправлено или пропало", [f"  ✔ {t} (было {o.upper()})" for t, o in d.resolved]),
        ("Стало лучше", [f"  ↓ {t}: {o.upper()} → {n.upper()}" for t, o, n in d.better]),
    ]
    for title, items in sections:
        if items:
            lines += ["", f"{title}:"] + items
    if d.unchanged_problems:
        lines += ["", f"Без изменений: {d.unchanged_problems} проблем(ы)."]
    if d.empty:
        lines.insert(1, "Новых проблем и исправлений нет.")
    return "\n".join(lines)


# ---------- снимки диагностики ----------

def save_snapshot(findings) -> Path:
    """Сохраняет результат диагностики (список Finding или пар (level, title)) и подчищает старые снимки."""
    items = [{"level": getattr(f, "level", None) or f[0], "title": getattr(f, "title", None) or f[1]} for f in findings]
    folder = history_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}.json"  # доли секунды: два снимка подряд не затирают друг друга
    path.write_text(json.dumps({"time": datetime.now().isoformat(timespec="seconds"), "findings": items},
                               ensure_ascii=False, indent=1), encoding="utf-8")
    for old in sorted(folder.glob("*.json"))[:-KEEP]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def load_snapshot(path: Path) -> tuple[datetime | None, list[tuple[str, str]]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return datetime.fromisoformat(doc["time"]), [(f["level"], f["title"]) for f in doc["findings"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


def latest_snapshot() -> tuple[datetime | None, list[tuple[str, str]]]:
    snaps = sorted(history_dir().glob("*.json")) if history_dir().exists() else []
    return load_snapshot(snaps[-1]) if snaps else (None, [])


# ---------- отчёты-архивы ----------

def parse_report_text(text: str) -> list[tuple[str, str]]:
    """Строки вида «[FAIL] заголовок» из diagnostics.txt или summary.txt (подробности с отступом пропускаются)."""
    out = []
    for line in text.splitlines():
        for mark, level in _LEVEL_WORDS.items():
            if line.startswith(mark):
                out.append((level, line[len(mark):].strip()))
                break
    return out


def read_report(path: str | Path) -> list[tuple[str, str]]:
    """Находки из архива отчёта: diagnostics.txt (с версии 0.7), иначе summary.txt."""
    with zipfile.ZipFile(path) as z:
        for name in ("diagnostics.txt", "summary.txt"):
            if name in z.namelist():
                return parse_report_text(z.read(name).decode("utf-8", errors="replace"))
    raise ValueError("в архиве нет diagnostics.txt или summary.txt — это не отчёт PIoT Helper")
