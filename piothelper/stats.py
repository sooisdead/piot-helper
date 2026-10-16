"""Статистика проверок марок: успех, время ответа ГИС МТ против таймаута ЕСМ. Помогает увидеть проблему до первой ошибки 514."""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import logs

# ЕСМ: «codes/check online ok host=https://cdn05.crpt.ru:19101 elapsed=57.1872ms failureCount=0»
ONLINE_OK = re.compile(r"codes/check online ok\b.*?elapsed=([\d.]+)\s*ms", re.I)
ONLINE_FAIL = re.compile(r"codes/check online\s+(?!ok\b)\S+", re.I)   # тот же префикс, что у успешной строки, но не «ok»
ELAPSED = re.compile(r"elapsed=([\d.]+)\s*ms", re.I)
# Событие km_chk в журнале событий ЕСМ: {"event_code":"km_chk","event_results":{"check_type_online":{"status":0,"elapsed_time":56,...}}}
KM_CHK = re.compile(r'"event_code"\s*:\s*"km_chk".*')
DEFAULT_TIMEOUT_MS = 1500  # значение cdnCodesCheckTimeout по умолчанию


@dataclass
class Check:
    time: datetime
    ok: bool
    ms: float | None


@dataclass
class Stats:
    total: int
    ok: int
    failed: int
    p50: float | None
    p95: float | None
    worst: float | None
    slow: int            # ответов дольше 2/3 таймаута
    timeout_ms: int

    @property
    def ok_rate(self) -> float:
        return 100.0 * self.ok / self.total if self.total else 0.0


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def _from_esm_log(lines: list[str]) -> list[Check]:
    out = []
    for ln in lines:
        t = logs.line_time(ln)
        if not t:
            continue
        m = ONLINE_OK.search(ln)
        if m:
            out.append(Check(t, True, float(m.group(1))))
        elif ONLINE_FAIL.search(ln):
            e = ELAPSED.search(ln)
            out.append(Check(t, False, float(e.group(1)) if e else None))
    return out


def _from_events_log(lines: list[str]) -> list[Check]:
    out = []
    for ln in lines:
        t = logs.line_time(ln)
        m = KM_CHK.search(ln) if t else None
        if not m:
            continue
        body = ln[ln.find("Сообщение:") + len("Сообщение:"):] if "Сообщение:" in ln else m.group(0)
        end = body.rfind("}. Дата")
        try:
            doc = json.loads(body[:end + 1].strip() if end != -1 else body.strip())
        except ValueError:
            continue
        online = (doc.get("event_results") or {}).get("check_type_online")
        if isinstance(online, dict):
            out.append(Check(t, online.get("status") == 0, float(online["elapsed_time"]) if "elapsed_time" in online else None))
    return out


def collect(cfg: dict, hours: float = 24) -> list[Check]:
    """Проверки за последние hours часов из всех заданных логов. Журнал событий используется, только если в основных логах пусто."""
    border = datetime.now() - timedelta(hours=hours)
    main: list[Check] = []
    events: list[Check] = []
    for entry in cfg["paths"]:
        if entry["kind"] != "log":
            continue
        for f in logs.list_log_files(entry["path"].strip())[:3]:
            try:
                if datetime.fromtimestamp(f.stat().st_mtime) < border:
                    continue
                lines = logs.read_tail(f, cfg["tail_kb"])
            except OSError:
                continue
            main += _from_esm_log(lines)
            if "event" in f.name.lower():
                events += _from_events_log(lines)
    chosen = main or events
    return sorted((c for c in chosen if c.time >= border), key=lambda c: c.time)


def summarize(checks: list[Check], timeout_ms: int = DEFAULT_TIMEOUT_MS) -> Stats:
    ms = [c.ms for c in checks if c.ok and c.ms is not None]
    return Stats(
        total=len(checks), ok=sum(c.ok for c in checks), failed=sum(not c.ok for c in checks),
        p50=_percentile(ms, 50), p95=_percentile(ms, 95), worst=max(ms) if ms else None,
        slow=sum(m > timeout_ms * 2 / 3 for m in ms), timeout_ms=timeout_ms)


def describe(s: Stats) -> str:
    if not s.total:
        return "Проверок марок за период не найдено."
    parts = [f"проверок: {s.total}, успешно {s.ok_rate:.0f}%"]
    if s.failed:
        parts.append(f"неудачных: {s.failed}")
    if s.p50 is not None:
        parts.append(f"ответ ГИС МТ: медиана {s.p50:.0f} мс, 95-й процентиль {s.p95:.0f} мс, максимум {s.worst:.0f} мс "
                     f"(таймаут ЕСМ {s.timeout_ms} мс)")
    return "; ".join(parts)
