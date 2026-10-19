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


_STATUS = {1: "Stopped", 2: "StartPending", 3: "StopPending", 4: "Running", 5: "ContinuePending", 6: "PausePending", 7: "Paused"}
_START_TYPE = {0: "Boot", 1: "System", 2: "Automatic", 3: "Manual", 4: "Disabled"}
_CIM_STATE = {"Running": "Running", "Stopped": "Stopped", "Start Pending": "StartPending", "Stop Pending": "StopPending",
              "Continue Pending": "ContinuePending", "Pause Pending": "PausePending", "Paused": "Paused"}
_CIM_START = {"Auto": "Automatic", "Manual": "Manual", "Disabled": "Disabled", "Boot": "Boot", "System": "System"}


def _run_json(script: str, timeout: int = 40) -> list[dict]:
    import json
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
                          "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script],
                         capture_output=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(out.stdout.decode("utf-8", errors="replace").strip() or "[]")
    return [data] if isinstance(data, dict) else data


def parse_cim_services(data: list[dict]) -> list[dict]:
    """Win32_Service → [{name, display, status, start}]. StartMode есть во всех версиях Windows, в отличие от StartType."""
    return [{"name": s["Name"], "display": s.get("DisplayName") or s["Name"],
             "status": _CIM_STATE.get(s.get("State"), str(s.get("State"))),
             "start": _CIM_START.get(s.get("StartMode"), str(s.get("StartMode")))} for s in data]


def parse_get_service(data: list[dict]) -> list[dict]:
    """Запасной вариант (Get-Service): тип запуска в PowerShell 4 (Windows 8.1) и старше не определяется."""
    out = []
    for s in data:
        st, sm = s["Status"], s.get("StartType")
        out.append({"name": s["Name"], "display": s["DisplayName"],
                    "status": _STATUS.get(st, str(st)) if isinstance(st, int) else str(st),
                    "start": (_START_TYPE.get(sm, str(sm)) if isinstance(sm, int) else str(sm)) if sm is not None else "Unknown"})
    return out


def all_services() -> list[dict]:
    """Все службы Windows одним вызовом: [{name, display, status, start}].

    Основной способ — WMI/CIM (Win32_Service): работает и в PowerShell 4 (Windows 8.1), где у Get-Service нет StartType.
    Если WMI на кассе повреждён или пуст, используется Get-Service (тип запуска тогда неизвестен)."""
    try:
        services = parse_cim_services(_run_json("Get-CimInstance Win32_Service | Select-Object Name,DisplayName,State,StartMode "
                                                "| ConvertTo-Json -Compress"))
        if services:
            return services
    except Exception:  # noqa: BLE001 — переходим к запасному способу
        pass
    return parse_get_service(_run_json("Get-Service | Select-Object Name,DisplayName,Status,StartType | ConvertTo-Json -Compress"))


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
