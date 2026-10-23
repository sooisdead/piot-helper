"""Хранение настроек: пути, шаблоны служб, порты, шаблоны ошибок."""
import copy
import json
import os
import re
import sys
from pathlib import Path

KINDS = {
    "log": "Папка/файл логов",
    "dir": "Папка программы",
    "file": "Файл",
}


def _powershell() -> str:
    """Полный путь к powershell.exe: на кассах PATH бывает урезан, и «powershell» по имени не находится."""
    p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(p) if p.exists() else "powershell"


POWERSHELL = _powershell()


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = app_dir() / "config.json"

DEFAULT_CONFIG = {
    "paths": [
        {"name": "Логи ТС ПИоТ", "kind": "log", "path": ""},
        {"name": "Папка ТС ПИоТ", "kind": "dir", "path": ""},
        {"name": "Логи Frontol", "kind": "log", "path": ""},
        {"name": "Папка Frontol", "kind": "dir", "path": ""},
        {"name": "Логи Атол", "kind": "log", "path": ""},
        {"name": "Папка Атол", "kind": "dir", "path": ""},
    ],
    # Ключевые слова автопоиска: продукт -> подстроки в названии папки, службы или программы.
    # Слово с «=» в начале ищется только как отдельное слово (ESP в «ESP\ESM», но не в «Respond»).
    "search_keywords": {
        "ТС ПИоТ": ["piot", "пиот", "=esm", "=esp", "=есм", "=есп", "единый сервисный", "сервисный модуль",
                    "=inventa", "=инвента"],
        "Frontol": ["frontol", "фронтол"],
        "Атол": ["atol", "атол", "fptr"],
        "Честный знак": ["crpt", "честн", "lmchz", "chestn"],
    },
    # Слова (без учёта регистра) для поиска служб по имени или описанию; «=» — только отдельное слово.
    "service_patterns": ["piot", "пиот", "честн", "атол", "atol", "fptr", "frontol", "фронтол",
                         "=esm", "=esp", "=есм", "=есп", "единый сервисный", "сервисный модуль"],
    # Связка служб для «Перезапустить связку» — в порядке ЗАПУСКА (останавливаются в обратном порядке).
    # Каждое слово ищется в имени или описании службы (подстрокой).
    "restart_chain": ["Fptr grpc", "ESM: Control Module", "ESM: Local Module Controller", "ESM: Orchestrator"],
    # Ночной перезапуск служб ЕСМ (задача Планировщика Windows). Драйвер АТОЛ намеренно не входит: с ним связан Frontol.
    "scheduled_restart_time": "05:00",
    "scheduled_restart_chain": ["ESM: Control Module", "ESM: Local Module Controller", "ESM: Orchestrator"],
    "scheduled_restart_quiet_minutes": 10,   # перезапускать, только если столько минут не было проверок марок
    "scheduled_restart_wait_minutes": 60,    # сколько ждать тишины; не дождались — пропустить эту ночь
    # Типовые места установки ТС ПИоТ (ЕСМ): проверяются автопоиском напрямую.
    "known_paths": [r"C:\ProgramData\ESP", r"C:\ProgramData\ESP\ESM", r"C:\Program Files\ESP",
                    r"C:\Program Files (x86)\ESP", r"C:\ProgramData\ESP\ESM\um"],
    "defaults_version": 2,
    # Папка с сертификатами ЕСМ (ca.crt, server.crt) — появляется после регистрации модуля.
    "esm_cert_dir": r"C:\ProgramData\ESP\ESM\um",
    # Где искать config_*ККТ*.yml модуля ЕСМ (параметр cdnCodesCheckTimeout).
    "esm_config_roots": [r"C:\ProgramData\ESP", r"C:\Program Files\ESP", r"C:\Program Files (x86)\ESP"],
    "ntp_server": "pool.ntp.org",
    # Отчёт для поддержки: маскировать ключи, серийные номера, ИНН, коды маркировки и т.п.
    "mask_report": True,
    # Свои слова для маскирования (название организации, адрес, фамилия): заменяются на [NAME…].
    "mask_extra": [],
    # Дата (ГГГГ-ММ-ДД), когда вы вручную проверили права кассиров во Frontol, и через сколько дней напомнить снова.
    "frontol_rights_confirmed": "",
    "rights_review_days": 90,
    # Фоновая проверка с уведомлениями в трее.
    "monitor_enabled": False,
    "monitor_minutes": 15,          # как часто проверять (не реже 5)
    "monitor_notify_warn": False,   # уведомлять и о предупреждениях, а не только об ошибках
    "monitor_repeat_hours": 6,      # одну и ту же проблему повторно показывать не чаще
    # Адрес файла правил для кнопки «Обновить правила» (только https и только GitHub).
    "rules_url": "https://raw.githubusercontent.com/sooisdead/piot-helper/main/piothelper/rules.json",
    # За сколько часов считать статистику проверок марок и хронологию в отчёте.
    "stats_hours": 24,
    # Ошибки в логах считаются только за последние N часов (если в логе есть метки времени).
    "recent_hours": 48,
    # Разовый сбой, не повторявшийся дольше N минут, считается устранённым (показывается как информация).
    "resolved_minutes": 30,
    # Проверки TCP-портов: {"name": ..., "host": ..., "port": ...}
    "ports": [],
    # Регулярные выражения для подсветки строк лога (без учёта регистра).
    "error_patterns": [r"\berror\b", r"\berr\b", r"\bfatal\b", r"\bfail", r"exception", r"ошибк", r"сбой", r"отказ"],
    "warn_patterns": [r"\bwarn", r"предупрежд", r"timeout", r"таймаут"],
    # Сколько последних килобайт файла читать при просмотре логов.
    "tail_kb": 2048,
    # Лог считается «застывшим», если в него не писали дольше N минут.
    "log_stale_minutes": 60,
}


def keyword_match(text: str, words: list[str]) -> bool:
    """Есть ли в тексте одно из слов. Слово с «=» в начале — только как отдельное слово, без букв по краям."""
    low = text.lower()
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        if w.startswith("="):
            if re.search(rf"(?<![a-zа-яё0-9]){re.escape(w[1:])}(?![a-zа-яё0-9])", low):
                return True
        elif w in low:
            return True
    return False


def _merge_new_defaults(cfg: dict) -> None:
    """Ключевые слова из новых версий программы добавляются к сохранённым (удалять их можно вручную позже)."""
    d = DEFAULT_CONFIG
    cfg["service_patterns"] = cfg["service_patterns"] + [w for w in d["service_patterns"] if w not in cfg["service_patterns"]]
    for product, words in d["search_keywords"].items():
        have = cfg["search_keywords"].setdefault(product, [])
        have += [w for w in words if w not in have]
    cfg["known_paths"] = d["known_paths"]
    cfg["defaults_version"] = d["defaults_version"]


def load() -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))  # sig: файл из Блокнота/PowerShell бывает с BOM
            cfg.update(saved)
            if saved.get("defaults_version", 1) < DEFAULT_CONFIG["defaults_version"]:
                _merge_new_defaults(cfg)
        except (OSError, ValueError):
            pass
    return cfg


def save(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
