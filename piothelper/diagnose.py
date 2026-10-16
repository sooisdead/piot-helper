"""Диагностика «зелёный, но коды не бьются»: только чтение. Исправления описаны в Finding.fix и выполняются позже."""
import datetime
import json
import os
import re
import socket
import struct
import time
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import checks, fixes, knowledge, logs
from .checks import FAIL, INFO, OK, WARN


@dataclass
class Fix:
    label: str
    description: str
    run: Callable[[], str]
    needs_admin: bool = True


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""
    fix: Optional[Fix] = None


def _entry_path(cfg: dict, name: str) -> str:
    return next((e["path"].strip() for e in cfg["paths"] if e["name"] == name), "")


def _ver(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text or ""))


def _fmt(v: tuple[int, ...]) -> str:
    return ".".join(map(str, v))


# ---------- службы и порты ----------

def check_services(cfg: dict) -> list[Finding]:
    svcs = checks.list_services(cfg["service_patterns"])
    if not svcs:
        return [Finding(WARN, "Службы не найдены", "Измените слова поиска на вкладке «Службы».")]
    out = []
    for s in svcs:
        title = f"Служба «{s['display']}»"
        if s["status"] == "Running":
            out.append(Finding(OK, f"{title} работает"))
            continue
        level = FAIL if s["start"] == "Automatic" else WARN
        out.append(Finding(level, f"{title} не запущена", f"Состояние: {s['status']}, запуск: {s['start']}", Fix(
            "Запустить службу", f"Запустить службу Windows «{s['display']}» ({s['name']})",
            lambda n=s["name"]: fixes.start_service(n))))
    if not any(re.search(r"fptr|grpc", f"{s['name']} {s['display']}", re.I) for s in svcs):
        out.append(Finding(WARN, "Служба gRPC драйвера АТОЛ не найдена",
                           "Для ТС ПИоТ нужна «ATOL: Fptr grpc service» (драйвер 10.10.8.24+ с компонентом интеграции ЕСМ)."))
    return out


def check_ports(cfg: dict) -> list[Finding]:
    if not cfg["ports"]:
        return [Finding(INFO, "Порты не заданы", "На вкладке «Порты» нажмите «Типовые порты ЕСМ/Frontol».")]
    out = []
    for p in cfg["ports"]:
        ok, text = checks.check_port(p["host"], p["port"])
        title = f"Порт {p['name']} ({p['host']}:{p['port']})"
        if ok:
            out.append(Finding(OK, f"{title} отвечает"))
        else:
            out.append(Finding(FAIL, f"{title} не отвечает: {text}",
                               "Служба не запущена, порт закрыт брандмауэром/антивирусом или указан неверный адрес."))
    return out


# ---------- время ----------

def ntp_offset(server: str, timeout: float = 3.0) -> Optional[float]:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            t0 = time.time()
            s.sendto(b"\x1b" + 47 * b"\0", (server, 123))
            data, _ = s.recvfrom(48)
            t1 = time.time()
    except OSError:
        return None
    secs, frac = struct.unpack("!II", data[40:48])
    return (secs - 2208988800 + frac / 2**32) - (t0 + t1) / 2


def check_time(cfg: dict) -> list[Finding]:
    off = ntp_offset(cfg["ntp_server"])
    if off is None:
        return [Finding(INFO, "Время не проверено", f"Нет ответа NTP-сервера {cfg['ntp_server']} (UDP 123).")]
    if abs(off) < 60:
        return [Finding(OK, f"Время компьютера точное (расхождение {off:+.1f} с)")]
    return [Finding(FAIL, f"Часы расходятся с эталоном на {off:+.0f} с",
                    "Из-за этого ломаются сертификаты и токены (ошибки 401/403/514).",
                    Fix("Синхронизировать время", "Запустить службу времени и выполнить w32tm /resync /force", fixes.sync_time))]


# ---------- сертификаты ----------

CERT_SCRIPT = (
    "$c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($env:PIOT_ARG);"
    "$t = $c.Thumbprint;"
    "$i = (Test-Path \"Cert:\\LocalMachine\\Root\\$t\") -or (Test-Path \"Cert:\\CurrentUser\\Root\\$t\");"
    "[pscustomobject]@{Thumbprint=$t;Subject=$c.Subject;NotAfter=$c.NotAfter.ToString('yyyy-MM-dd');Installed=$i}"
    " | ConvertTo-Json -Compress"
)


def check_certs(cfg: dict) -> list[Finding]:
    folder = Path(cfg["esm_cert_dir"])
    if not folder.is_dir():
        return [Finding(INFO, "Папка сертификатов ЕСМ не найдена",
                        f"{folder}. Если ЕСМ ещё не зарегистрирован — это нормально; иначе поправьте esm_cert_dir в config.json.")]
    out = []
    for name in knowledge.CERT_FILES:
        f = folder / name
        if not f.is_file():
            out.append(Finding(WARN, f"Нет файла {name}", f"Ожидался в {folder}. Регистрация модуля не завершена?"))
            continue
        try:
            info = json.loads(fixes.ps(CERT_SCRIPT, str(f)))
        except Exception as e:  # noqa: BLE001
            out.append(Finding(WARN, f"{name}: не удалось прочитать сертификат", str(e)))
            continue
        brief = f"{info['Subject']}; действует до {info['NotAfter']}; отпечаток {info['Thumbprint']}"
        if info["NotAfter"] < time.strftime("%Y-%m-%d"):
            out.append(Finding(FAIL, f"{name}: срок действия истёк", brief + ". Нужна перерегистрация модуля в ЛК ЕСП."))
        elif not info["Installed"]:
            out.append(Finding(FAIL, f"{name} не установлен в доверенные корневые", brief, Fix(
                "Установить сертификат", f"Добавить {f} в «Доверенные корневые центры сертификации» (LocalMachine).\n"
                                         f"Субъект: {info['Subject']}\nОтпечаток: {info['Thumbprint']}",
                lambda p=str(f): fixes.import_cert(p))))
        else:
            out.append(Finding(OK, f"{name} установлен в доверенные корневые", brief))
    return out


# ---------- конфиг ЕСМ ----------

def _find_esm_config(cfg: dict) -> Optional[Path]:
    for root in cfg["esm_config_roots"]:
        base = Path(root)
        if not base.is_dir():
            continue
        depth0 = len(base.parts)
        for dirpath, dirs, files in os.walk(base):
            if len(Path(dirpath).parts) - depth0 >= 5:
                dirs[:] = []
            for fn in files:
                if re.fullmatch(r"config_.*ккт.*\.ya?ml", fn, re.I):
                    return Path(dirpath) / fn
    return None


def check_esm_config(cfg: dict) -> list[Finding]:
    f = _find_esm_config(cfg)
    if not f:
        return [Finding(INFO, "Конфиг ЕСМ (config_…ККТ.yml) не найден", "Папки поиска: esm_config_roots в config.json.")]
    try:
        text = f.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as e:
        return [Finding(WARN, "Конфиг ЕСМ не прочитать", str(e))]
    m = re.search(r"cdnCodesCheckTimeout\s*:\s*(\d+)", text)
    rec = knowledge.CDN_TIMEOUT_RECOMMENDED
    if not m:
        return [Finding(INFO, "В конфиге ЕСМ нет cdnCodesCheckTimeout", str(f))]
    cur = int(m.group(1))
    if cur >= rec:
        return [Finding(OK, f"cdnCodesCheckTimeout = {cur} мс", str(f))]
    return [Finding(WARN, f"cdnCodesCheckTimeout = {cur} мс (рекомендуется {rec})",
                    f"{f}\nПри медленном ответе ГИС МТ проверка падает по таймауту (514). После правки перезапустите службу ЕСМ.",
                    Fix("Поставить 2500 мс", f"В файле {f} заменить cdnCodesCheckTimeout {cur} → {rec}.\n"
                                             "Рядом будет создана резервная копия (.bak-…).",
                        lambda p=str(f): fixes.set_cdn_timeout(p, rec), needs_admin=False))]


# ---------- версии и разрядность ----------

def pe_machine(path: Path) -> str:
    with open(path, "rb") as f:
        f.seek(0x3C)
        off = struct.unpack("<I", f.read(4))[0]
        f.seek(off)
        if f.read(4) != b"PE\0\0":
            return "?"
        m = struct.unpack("<H", f.read(2))[0]
    return {0x14C: "x86", 0x8664: "x64"}.get(m, hex(m))


def _find_file(base: str, name: str, depth: int) -> Optional[Path]:
    if not base or not Path(base).is_dir():
        return None
    d0 = len(Path(base).parts)
    for dirpath, dirs, files in os.walk(base):
        if len(Path(dirpath).parts) - d0 >= depth:
            dirs[:] = []
        for fn in files:
            if fn.lower() == name:
                return Path(dirpath) / fn
    return None


def check_versions(cfg: dict) -> list[Finding]:
    out = []
    frontol = _find_file(_entry_path(cfg, "Папка Frontol"), "frontol.exe", 2)
    driver = _find_file(_entry_path(cfg, "Папка Атол"), "fptr10.dll", 5)
    if not driver:
        for guess in (r"C:\Program Files (x86)\ATOL\Drivers10\KKT\bin\fptr10.dll", r"C:\Program Files\ATOL\Drivers10\KKT\bin\fptr10.dll"):
            if Path(guess).is_file():
                driver = Path(guess)
                break
    if not frontol:
        out.append(Finding(INFO, "frontol.exe не найден", "Укажите «Папка Frontol» на вкладке «Пути» (или запустите автопоиск)."))
    if not driver:
        out.append(Finding(INFO, "fptr10.dll (драйвер АТОЛ) не найден", "Укажите «Папка Атол» на вкладке «Пути»."))
    for label, f, minimum in (("Frontol", frontol, knowledge.MIN_VERSIONS["frontol"]),
                              ("Драйвер АТОЛ", driver, knowledge.MIN_VERSIONS["driver"])):
        if not f:
            continue
        try:
            v = _ver(fixes.ps("(Get-Item -LiteralPath $env:PIOT_ARG).VersionInfo.FileVersion", str(f)))
        except Exception:  # noqa: BLE001
            continue
        if v and v < minimum:
            out.append(Finding(FAIL, f"{label} {_fmt(v)} старее минимальной {_fmt(minimum)}", f"{f}\nОбновите для работы с ТС ПИоТ."))
        elif v:
            out.append(Finding(OK, f"{label} {_fmt(v)} (минимум {_fmt(minimum)})", str(f)))
    if frontol and driver:
        try:
            a, b = pe_machine(frontol), pe_machine(driver)
        except OSError:
            return out
        if a != b:
            out.append(Finding(FAIL, f"Разрядность не совпадает: Frontol {a}, драйвер АТОЛ {b}",
                               "Драйвер должен быть той же разрядности, что и кассовое ПО (для Frontol 6 обычно x86)."))
        else:
            out.append(Finding(OK, f"Разрядность Frontol и драйвера совпадает ({a})"))
    return out


# ---------- внешние адреса ----------

def _probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except socket.gaierror:
        return False, "имя не разрешается (DNS)"
    except socket.timeout:
        return False, "таймаут (порт, вероятно, закрыт файрволом)"
    except OSError as e:
        return False, e.strerror or "ошибка соединения"


def check_external(cfg: dict) -> list[Finding]:
    """Доступны ли внешние адреса, без которых ЕСМ не проверит код и не получит лицензию."""
    from concurrent.futures import ThreadPoolExecutor
    jobs = [(group, h, p) for group, hosts in knowledge.EXTERNAL_HOSTS.items() for h, p in hosts]
    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda j: _probe(j[1], j[2]), jobs))
    by_group: dict[str, list[tuple[str, int, bool, str]]] = {}
    for (group, h, p), (ok, why) in zip(jobs, results):
        by_group.setdefault(group, []).append((h, p, ok, why))
    if not any(ok for rows in by_group.values() for _, _, ok, _ in rows):
        return [Finding(FAIL, "Нет доступа к внешним адресам ЕСМ (ЦРПТ, ЕСП)",
                        "Недоступен ни один из проверяемых адресов: нет интернета, DNS или всё закрыто файрволом. "
                        "Проверьте подключение и попросите администратора сети разрешить исходящие TCP-соединения: "
                        + "; ".join(f"{h}:{p}" for rows in by_group.values() for h, p, _, _ in rows[:1]) + " и др.")]
    out = []
    for group, rows in by_group.items():
        good = [r for r in rows if r[2]]
        bad = [r for r in rows if not r[2]]
        ports = sorted({r[1] for r in rows})
        if group.startswith("Серверы") and good:  # серверов проверки много, нужен хотя бы один; остальные адреса обязательны
            lvl = OK if len(good) == len(rows) else INFO
            out.append(Finding(lvl, f"{group}: доступно {len(good)} из {len(rows)} (порт {ports[0]})",
                               "; ".join(f"{h}: {why}" for h, _, _, why in bad)))
        elif not bad:
            out.append(Finding(OK, f"{group}: доступно ({', '.join(f'{h}:{p}' for h, p, _, _ in rows)})"))
        else:
            hosts = ", ".join(f"{h}:{p}" for h, p, _, _ in bad)
            why = "; ".join(sorted({w for *_, w in bad}))
            out.append(Finding(FAIL, f"{group}: недоступно ({hosts})",
                               f"{why}. Это частая причина ошибок 514/403: попросите администратора сети или провайдера "
                               "открыть исходящие соединения к этим адресам."))
    return out


# ---------- смена ----------

SHIFT_RE = re.compile(r"shiftStatus\W{1,6}\{(.*?)\}", re.S)


def check_shift(cfg: dict) -> list[Finding]:
    """ЕСМ требует смену младше 24 часов. Берём последнюю запись getShiftStatus из свежего лога драйвера."""
    for entry in cfg["paths"]:
        if entry["kind"] != "log":
            continue
        for f in logs.list_log_files(entry["path"].strip())[:3]:
            if "fptr" not in f.name.lower():
                continue
            try:
                text = "\n".join(logs.read_tail(f, cfg["tail_kb"]))
                age = logs.file_age_minutes(f)
            except OSError:
                continue
            ms = list(SHIFT_RE.finditer(text))
            if not ms:
                continue
            body = ms[-1].group(1)
            state = (re.search(r"state\W{1,6}(\w+)", body) or [None, ""])[1]
            number = (re.search(r"number\W{1,6}(\d+)", body) or [None, "?"])[1]
            exp = re.search(r"expiredTime\W{1,6}(\d{4}-\d{2}-\d{2}T[\d:]+(?:[+-]\d{2}:\d{2}|Z)?)", body)
            if age > 60:
                return [Finding(INFO, "Смена не проверена: лог драйвера давно не обновлялся", str(f))]
            if state == "closed":
                return [Finding(INFO, f"Смена закрыта (по логу драйвера {f.name})",
                                "Откройте смену: проверка кодов и регистрация ЕСМ работают только при открытой смене.")]
            if state == "expired" or not exp:
                return [Finding(FAIL if state == "expired" else INFO, f"Смена №{number}: состояние «{state or '?'}»",
                                "Смена истекла (больше 24 ч): закройте её и откройте новую.")]
            end = datetime.datetime.fromisoformat(exp.group(1).replace("Z", "+00:00"))
            now = datetime.datetime.now(end.tzinfo) if end.tzinfo else datetime.datetime.now()
            left = (end - now).total_seconds() / 3600
            if left <= 0:
                return [Finding(FAIL, f"Смена №{number} истекла {-left:.0f} ч назад",
                                "Смена дольше 24 часов: закройте и откройте новую, иначе проверка маркировки и регистрация "
                                "ЕСМ работать не будут.")]
            if left < knowledge.SHIFT_WARN_HOURS:
                return [Finding(WARN, f"Смена №{number} закончится через {left * 60:.0f} мин",
                                "Закройте и откройте смену заранее, чтобы не потерять проверку марок.")]
            return [Finding(OK, f"Смена №{number} открыта, до конца 24 ч осталось {left:.0f} ч")]
    return [Finding(INFO, "Смена не проверена", "В логах драйвера (fptr10) нет данных о смене или лог не указан.")]


# ---------- версия ЕСМ ----------

def check_esm_version(cfg: dict) -> list[Finding]:
    """Версия ЕСМ по файлам служб esm-*. Рекомендуется 1.6.4+."""
    script = ("Get-CimInstance Win32_Service | Where-Object { $_.Name -like 'esm-*' } | "
              "Select-Object Name,PathName | ConvertTo-Json -Compress")
    raw = fixes.ps(script, timeout=40)
    if not raw:
        return []
    rows = json.loads(raw)
    rows = [rows] if isinstance(rows, dict) else rows
    versions = {}
    for r in rows:
        m = re.match(r'\s*"([^"]+)"|\s*(\S+?\.exe)', r.get("PathName") or "", re.I)
        exe = (m.group(1) or m.group(2)) if m else ""
        if not exe or not Path(exe).is_file():
            continue
        try:
            v = _ver(fixes.ps("(Get-Item -LiteralPath $env:PIOT_ARG).VersionInfo.FileVersion", exe))
        except Exception:  # noqa: BLE001
            continue
        if v:
            versions[r["Name"]] = v
    if not versions:
        return [Finding(INFO, "Версия ЕСМ не определена", "У файлов служб esm-* нет информации о версии.")]
    low = min(versions.values())
    shown = ", ".join(f"{n}: {_fmt(v)}" for n, v in versions.items())
    if low < knowledge.MIN_ESM_VERSION:
        return [Finding(WARN, f"ЕСМ {_fmt(low)} старее рекомендуемой {_fmt(knowledge.MIN_ESM_VERSION)}", shown +
                        "\nОбновите ЕСМ и контроллер (сначала их, и только потом ЛМ ЧЗ).")]
    return [Finding(OK, f"ЕСМ {_fmt(low)} (рекомендуется {_fmt(knowledge.MIN_ESM_VERSION)}+)", shown)]


# ---------- прокси ----------

def check_proxy(cfg: dict) -> list[Finding]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as k:
            enabled = winreg.QueryValueEx(k, "ProxyEnable")[0]
            server = winreg.QueryValueEx(k, "ProxyServer")[0] if enabled else ""
            try:
                override = winreg.QueryValueEx(k, "ProxyOverride")[0]
            except OSError:
                override = ""
    except OSError:
        return []
    if not enabled:
        return [Finding(OK, "Прокси в системе не включён")]
    local_ok = any(x in override.lower() for x in ("<local>", "127.0.0.1", "localhost"))
    if local_ok:
        return [Finding(OK, f"Прокси включён ({server}), локальные адреса исключены")]
    return [Finding(WARN, f"Прокси включён ({server}) и не исключает локальные адреса",
                    f"ProxyOverride: «{override}». Запросы к 127.0.0.1 могут уходить в прокси (ошибка 12029). "
                    "Добавьте <local>;127.0.0.1;localhost в исключения прокси.")]


# ---------- ошибки в логах ----------

def check_logs(cfg: dict) -> list[Finding]:
    rules = [(r, re.compile(r.pattern, re.I)) for r in knowledge.RULES]
    out, any_log = [], False
    for entry in cfg["paths"]:
        if entry["kind"] != "log":
            continue
        files = logs.list_log_files(entry["path"].strip())
        if not files:
            continue
        any_log = True
        try:
            lines = logs.read_tail(files[0], cfg["tail_kb"])
        except OSError:
            continue
        recent, has_ts = logs.recent_lines(lines, cfg["recent_hours"])
        for rule, rx in rules:
            hits = [ln for ln in recent if rx.search(ln)]
            where = f"{entry['name']} / {files[0].name}"
            if not hits:
                old = [ln for ln in lines if rx.search(ln)]
                if old and has_ts:  # встречалось раньше, сейчас нет
                    t = next((logs.line_time(ln) for ln in reversed(old) if logs.line_time(ln)), None)
                    when = f"последний раз {t:%d.%m.%Y %H:%M}" if t else "давно"
                    out.append(Finding(INFO, f"{where}: {rule.title} — было раньше ({when}), за {cfg['recent_hours']} ч нет",
                                       f"Встречалось записей в хвосте файла: {len(old)}."))
                continue
            level = {"fail": FAIL, "warn": WARN, "info": INFO}[rule.level]
            title = f"{where}: {rule.title} (за {cfg['recent_hours']} ч: {len(hits)})" if has_ts else \
                f"{where}: {rule.title} (совпадений: {len(hits)})"
            detail = f"{rule.advice}\nПоследняя строка: {hits[-1].strip()[:200]}"
            last = next((logs.line_time(ln) for ln in reversed(hits) if logs.line_time(ln)), None)
            if rule.key in knowledge.TRANSIENT_KEYS and last and has_ts:
                quiet = (datetime.datetime.now() - last).total_seconds() / 60
                if quiet > cfg["resolved_minutes"]:  # давно не повторяется — устранено
                    out.append(Finding(INFO, f"{where}: {rule.title} — было, не повторяется с {last:%H:%M} "
                                             f"(записей: {len(hits)})", detail))
                    continue
            if rule.key == "lic":  # ищем льготную дату и считаем остаток дней
                m = re.search(r"сроком до (\d{2})\.(\d{2})\.(\d{4})", " ".join(hits))
                if m:
                    left = (datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) - datetime.date.today()).days
                    title += f" — льготный срок до {m.group(1)}.{m.group(2)}.{m.group(3)} (осталось дн.: {left})"
                    level = FAIL if left <= 3 else WARN
            out.append(Finding(level, title, detail))
    if not any_log:
        out.append(Finding(INFO, "Логи не проанализированы", "Укажите пути к логам на вкладке «Пути»."))
    elif not out:
        out.append(Finding(OK, "Известных ошибок в хвостах логов не найдено"))
    return out


CHECKS = [
    ("Службы", check_services), ("Порты", check_ports), ("Время", check_time), ("Сертификаты", check_certs),
    ("Конфиг ЕСМ", check_esm_config), ("Версии", check_versions), ("Версия ЕСМ", check_esm_version),
    ("Внешние адреса", check_external), ("Смена", check_shift), ("Прокси", check_proxy), ("Логи", check_logs),
]
ORDER = {FAIL: 0, WARN: 1, INFO: 2, OK: 3}


def run(cfg: dict) -> list[Finding]:
    findings: list[Finding] = []
    for name, fn in CHECKS:
        try:
            findings += fn(cfg)
        except Exception as e:  # noqa: BLE001 — одна упавшая проверка не должна ломать остальные
            findings.append(Finding(WARN, f"Проверка «{name}» не выполнена", str(e)))
    return sorted(findings, key=lambda f: ORDER[f.level])
