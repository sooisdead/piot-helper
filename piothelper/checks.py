"""Проверки: пути, службы, порты, общая сводка."""
import socket
import subprocess
from datetime import datetime
from pathlib import Path

from . import logs
from .config import POWERSHELL, keyword_match

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


def path_status(entry: dict) -> tuple[str, str]:
    """(уровень, описание) для одной записи из списка путей."""
    raw = entry.get("path", "").strip()
    if not raw:
        return WARN, "путь не указан"
    p = Path(raw)
    try:
        if not p.exists():
            return FAIL, "не найден"
        mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m.%Y %H:%M")
        what = "папка" if p.is_dir() else "файл"
        return OK, f"{what}, изменён {mtime}"
    except OSError as e:
        return FAIL, f"нет доступа ({e.strerror})"


def list_services(patterns: list[str]) -> list[dict]:
    """Службы Windows, у которых имя или описание содержит один из шаблонов."""
    return [s for s in all_services() if keyword_match(f"{s['name']} {s['display']}", patterns)]


def all_services() -> list[dict]:
    """Все службы Windows одним вызовом: [{name, display, status, start}]."""
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-Service | Select-Object Name,DisplayName,Status,StartType | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    import json
    data = json.loads(out.stdout.decode("utf-8", errors="replace") or "[]")
    if isinstance(data, dict):
        data = [data]
    status_names = {1: "Stopped", 2: "StartPending", 3: "StopPending", 4: "Running", 5: "ContinuePending", 6: "PausePending", 7: "Paused"}
    start_names = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}
    result = []
    for s in data:
        st, sm = s["Status"], s["StartType"]
        result.append({
            "name": s["Name"],
            "display": s["DisplayName"],
            "status": status_names.get(st, str(st)) if isinstance(st, int) else str(st),
            "start": start_names.get(sm, str(sm)) if isinstance(sm, int) else str(sm),
        })
    return result


def check_port(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, "открыт"
    except socket.timeout:
        return False, "таймаут"
    except OSError as e:
        return False, e.strerror or "недоступен"


def log_summary(entry: dict, cfg: dict) -> tuple[str, str]:
    files = logs.list_log_files(entry.get("path", "").strip())
    if not files:
        return WARN, "файлы логов не найдены"
    newest = files[0]
    try:
        age = logs.file_age_minutes(newest)
        cls = logs.Classifier(cfg)
        lines = logs.read_tail(newest, cfg["tail_kb"])
    except OSError as e:
        return FAIL, f"{newest.name}: не прочитать ({e.strerror})"
    recent, has_ts = logs.recent_lines(lines, cfg["recent_hours"])
    errs = sum(1 for ln in recent if cls.level(ln) == "error")
    warns = sum(1 for ln in recent if cls.level(ln) == "warn")
    scope = f"за {cfg['recent_hours']} ч" if has_ts else "в хвосте файла"
    text = f"{newest.name}: ошибок {errs}, предупреждений {warns} {scope}, последняя запись {age:.0f} мин назад"
    if age > cfg["log_stale_minutes"]:
        return WARN, text + " (лог давно не обновлялся)"
    return (FAIL if errs else OK), text


def run_summary(cfg: dict) -> list[tuple[str, str]]:
    """Все проверки разом: [(уровень, строка)]."""
    rows: list[tuple[str, str]] = []
    for e in cfg["paths"]:
        lvl, text = path_status(e)
        rows.append((lvl, f"{e['name']}: {text}"))
        if e["kind"] == "log" and lvl == OK:
            lvl, text = log_summary(e, cfg)
            rows.append((lvl, f"  └ {text}"))
    try:
        services = list_services(cfg["service_patterns"])
        if not services:
            rows.append((WARN, "Службы: ничего не найдено по шаблонам"))
        for s in services:
            lvl = OK if s["status"] == "Running" else (FAIL if s["start"] == "Automatic" else WARN)
            rows.append((lvl, f"Служба {s['display']} ({s['name']}): {s['status']}, запуск {s['start']}"))
    except Exception as e:  # noqa: BLE001 — диагностика не должна падать
        rows.append((FAIL, f"Службы: не удалось получить список ({e})"))
    for p in cfg["ports"]:
        ok, text = check_port(p["host"], p["port"])
        rows.append((OK if ok else FAIL, f"Порт {p['name']} {p['host']}:{p['port']}: {text}"))
    return rows
