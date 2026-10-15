"""Безопасный перезапуск связки служб ТС ПИоТ.

Останавливаем в обратном порядке запуска (оркестратор → … → драйвер АТОЛ), запускаем в прямом, после каждого шага
ждём нужного состояния службы. Затем проверяем по логам, что ЕСМ снова видит ККТ.
Если остановка не удалась — уже остановленные службы запускаются обратно.
"""
import datetime
import re
import time
from typing import Callable

from . import checks, fixes, knowledge, logs
from .config import keyword_match

# Сообщения, по которым видно, что ЕСМ ещё не нашёл ККТ / не достучался до модуля.
VERIFY_KEYS = ("dkkt_client", "serial", "12029", "http")
VERIFY_SECONDS = 90
QUIET_SECONDS = 30  # столько без свежих ошибок считаем «связь восстановлена»


def resolve_chain(cfg: dict) -> tuple[list[dict], list[str]]:
    """Службы по шаблонам restart_chain в порядке запуска. Возвращает (найденные, шаблоны без совпадений)."""
    services = checks.all_services()
    chain, missing, seen = [], [], set()
    for pattern in cfg["restart_chain"]:
        hits = [s for s in services if keyword_match(f"{s['name']} {s['display']}", [pattern]) and s["name"] not in seen]
        if not hits:
            missing.append(pattern)
        for s in hits:
            seen.add(s["name"])
            chain.append(s)
    return chain, missing


def _fresh_problems(cfg: dict, since: datetime.datetime) -> list[str]:
    """Строки логов не раньше since, говорящие о потере связи ЕСМ с драйвером/модулем."""
    rules = [re.compile(r.pattern, re.I) for r in knowledge.RULES if r.key in VERIFY_KEYS]
    found = []
    for entry in cfg["paths"]:
        if entry["kind"] != "log":
            continue
        files = logs.list_log_files(entry["path"].strip())
        if not files:
            continue
        try:
            lines = logs.read_tail(files[0], 512)
        except OSError:
            continue
        for ln in lines:
            t = logs.line_time(ln)
            if t and t >= since and any(r.search(ln) for r in rules):
                found.append(f"{files[0].name}: {ln.strip()[:160]}")
    return found


def run_chain(chain: list[dict], cfg: dict, say: Callable[[str], None]) -> str:
    """Выполняет перезапуск. say(текст) — вывод хода работы. Возвращает итоговый отчёт; при сбое остановки — исключение."""
    t0 = time.time()
    report: list[str] = []
    stopped: list[dict] = []
    for s in reversed(chain):
        say(f"Останавливаю «{s['display']}»…")
        try:
            if s["status"] == "Running":
                fixes.stop_service(s["name"])
                stopped.append(s)
        except Exception as e:  # noqa: BLE001
            say("Остановка не удалась, возвращаю остановленные службы…")
            for back in reversed(stopped):
                try:
                    fixes.start_service(back["name"])
                except Exception:  # noqa: BLE001
                    pass
            raise RuntimeError(f"Не удалось остановить «{s['display']}»: {e}. Остановленные службы запущены обратно.") from e
        report.append(f"остановлена: {s['display']}")

    restart_started = datetime.datetime.now() - datetime.timedelta(seconds=2)
    failed = []
    for s in chain:
        if s["status"] != "Running" and s not in stopped:
            report.append(f"пропущена (не работала до перезапуска): {s['display']}")
            continue
        say(f"Запускаю «{s['display']}»…")
        try:
            fixes.start_service(s["name"])
            report.append(f"запущена: {s['display']}")
        except Exception as e:  # noqa: BLE001
            failed.append(s["display"])
            report.append(f"НЕ ЗАПУЩЕНА: {s['display']} ({e})")

    if failed:
        return "Перезапуск завершён с ошибками.\n" + "\n".join(report) + \
            f"\n\nНе запустились: {', '.join(failed)}. Запустите их вручную (services.msc) и проверьте логи."

    say(f"Жду восстановления связи ЕСМ с драйвером (до {VERIFY_SECONDS} с)…")
    deadline = time.time() + VERIFY_SECONDS
    last_problem_at = time.time()
    problems: list[str] = []
    while time.time() < deadline:
        time.sleep(10)
        problems = _fresh_problems(cfg, restart_started)
        if problems:
            last_problem_at = time.time()
        elif time.time() - last_problem_at >= QUIET_SECONDS:
            break
    spent = time.time() - t0
    if problems:  # на последнем опросе свежие ошибки связи ещё были
        verdict = ("Службы запущены, но ЕСМ пока сообщает о проблемах связи (последние записи ниже). "
                   "Подождите 1–2 минуты и запустите диагностику ещё раз.\n  " + "\n  ".join(problems[-3:]))
    else:
        verdict = "Связь восстановлена: свежих ошибок связи ЕСМ с драйвером/модулем в логах нет."
    return "\n".join(report) + f"\n\n{verdict}\nВремя: {spent:.0f} с."
