"""База знаний по сбоям ТС ПИоТ (ЕСМ, ЕСП) + Frontol 6 + драйвер АТОЛ.

Источники: документация АТОЛ (atoldriver.ru), справка Контура и ЕСП, FAQ ТС ПИоТ, материалы ЦРПТ.
Номера кодов в разных системах пересекаются (403, 407, 514), поэтому совпадение в логе — подсказка, а не приговор.

Правила хранятся в данных, а не в коде. Слои (каждый следующий перекрывает предыдущий по ключу `key`):
  1. piothelper/rules.json          — встроенные правила (лежат в репозитории и в exe);
  2. <папка программы>/rules_update.json — правила, скачанные кнопкой «Обновить правила» (если версия новее встроенных);
  3. <папка программы>/rules_user.json   — ваши правила: добавить своё, заменить встроенное с тем же key
                                           или отключить: {"key": "514", "disabled": true}.

Формат файла: {"rules_version": 1, "rules": [{"key", "pattern", "level": "fail|warn|info", "title", "advice",
"transient": false}]}. Вместо `pattern` (или вместе с ним) можно задать `codes`: [{"n": "514", "extra": "нет ответа"}] —
трёхзначный код засчитывается только рядом со словами «код/error/ошибка…» или со словами из `extra` после числа
(иначе совпадают миллисекунды во времени 14:14:28.514).
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import namedtuple
from pathlib import Path

from .config import app_dir

# transient=True: сбой разовый (связь, перезапуск); если он давно не повторяется, считаем его устранённым.
Rule = namedtuple("Rule", "key pattern level title advice transient", defaults=(False,))

RULES: list[Rule] = []
TRANSIENT_KEYS: set[str] = set()
RULES_INFO: dict = {"version": 0, "bundled": 0, "update": 0, "user": 0, "errors": []}

DEFAULT_RULES_URL = "https://raw.githubusercontent.com/sooisdead/piot-helper/main/piothelper/rules.json"
ALLOWED_HOSTS = {"raw.githubusercontent.com", "github.com"}
MAX_RULES_BYTES = 512 * 1024
MAX_RULES = 400

# Внешние адреса, нужные ЕСМ (документация АТОЛ «Установка ЕСМ»): регистрация, проверка кодов, лицензия.
EXTERNAL_HOSTS = {
    "Регистрация ТС ПИоТ (ЦРПТ)": [("ts-reg.crpt.ru", 19100)],
    "Серверы проверки кодов (ЦРПТ)": [(f"cdn{n:02d}.crpt.ru", 19101) for n in range(1, 12)],
    "Лицензирование ЕСП": [("stats.ao-esp.ru", 443), ("api.ao-esp.ru", 443)],
}
MIN_ESM_VERSION = (1, 6, 4, 0)
SHIFT_WARN_HOURS = 2  # предупреждать, если до конца 24-часовой смены осталось меньше

# Типовые порты связки Frontol 6 + Mark Unit + ЕСМ + драйвер АТОЛ (документация АТОЛ «ЕСМ и Frontol»).
TYPICAL_PORTS = [
    ("Mark Unit (Frontol → Mark Unit, HTTP)", "127.0.0.1", 8000),
    ("ЕСМ / ТС ПИоТ (Mark Unit → ЕСМ, HTTPS)", "127.0.0.1", 51401),
    ("gRPC драйвера АТОЛ (драйвер → ЕСМ)", "127.0.0.1", 4041),
    ("Контроллер ЛМ ЧЗ", "127.0.0.1", 50063),
]

# Минимальные версии для работы ТС ПИоТ с Frontol 6 и АТОЛ (документация АТОЛ).
MIN_VERSIONS = {"frontol": (6, 28, 0), "driver": (10, 10, 8, 24)}
# С этой версии Frontol, по сведениям поставщика (Робот Икс), не даёт пробить просроченный маркированный товар.
FRONTOL_EXPIRY_PROTECTION = (6, 29, 0)

# Права пользователей Frontol 6, позволяющие кассиру обойти запрет на продажу маркированного товара
# (руководство «Frontol Mark Unit. Быстрый старт», раздел «Настройка прав пользователя Frontol 6»).
FRONTOL_RIGHTS = [
    "Документ продажи \\ Регистрация \\ С не корректным результатом проверки штрихкода маркировки",
    "Документ \\ Игнорировать ошибки повторной продажи маркированной продукции",
]
FRONTOL_RIGHTS_TEXT = (
    "По руководству Frontol Mark Unit запрет на продажу маркированного товара не блокирует кассу: кассир сможет добавить "
    "товар в чек, если в его профиле включены права обхода. Чтобы кассиры не продавали товар в обход запрета (в том числе "
    "просроченный), во Frontol Администратор откройте «Системные справочники → Пользователи», выберите профиль кассира и "
    "оставьте эти права только ответственному сотруднику:\n"
    + "\n".join(f"  • «{r}»" for r in FRONTOL_RIGHTS)
    + "\nНазвания прав взяты из руководства FMU и в вашей версии Frontol могут отличаться. Относится ли первое право именно "
      "к просроченным товарам — уточните в поддержке АТОЛ. Программа не читает и не меняет базу Frontol, поэтому проверить "
      "это автоматически не может."
)
CDN_TIMEOUT_RECOMMENDED = 2500  # мс; по умолчанию 1500
CERT_FILES = ("ca.crt", "server.crt")


# ---------- загрузка и проверка правил ----------

def _code(n: str, extra: str = "") -> str:
    ctx = r"(?:код|code|error|errorcode|ошибк\w*|status|http|rpc)"
    p = rf"{ctx}\W{{0,8}}(?:{n})(?!\d|[.:]\d)"
    if extra:
        p += rf"|(?<![\d.:/-])(?:{n})(?!\d|[.:]\d)\W{{0,3}}(?:{extra})"
    return p


_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*[+*{]")  # (a+)+ — риск «взрыва» регулярки


def _build_pattern(item: dict) -> str:
    parts = []
    for c in item.get("codes") or []:
        parts.append(_code(str(c["n"]), str(c.get("extra", ""))))
    if item.get("pattern"):
        parts.append(str(item["pattern"]))
    return "|".join(parts)


def validate_rules_doc(doc: object, allow_disabled: bool = False) -> tuple[list[dict], list[str]]:
    """Проверяет документ правил. Возвращает (разобранные элементы, ошибки). Элементы — dict с готовым pattern."""
    errors: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("rules"), list):
        return [], ["ожидался объект с массивом rules"]
    if len(doc["rules"]) > MAX_RULES:
        return [], [f"слишком много правил (>{MAX_RULES})"]
    items, seen = [], set()
    for i, raw in enumerate(doc["rules"]):
        where = f"правило #{i + 1}"
        if not isinstance(raw, dict):
            errors.append(f"{where}: не объект")
            continue
        key = raw.get("key")
        if not isinstance(key, str) or not re.fullmatch(r"[\w.-]{1,40}", key):
            errors.append(f"{where}: некорректный key")
            continue
        where = f"правило «{key}»"
        if key in seen:
            errors.append(f"{where}: повторяющийся key")
            continue
        seen.add(key)
        if raw.get("disabled") is True:
            if allow_disabled:
                items.append({"key": key, "disabled": True})
            else:
                errors.append(f"{where}: disabled допустим только в rules_user.json")
            continue
        if raw.get("level") not in ("fail", "warn", "info"):
            errors.append(f"{where}: level должен быть fail, warn или info")
            continue
        title, advice = raw.get("title"), raw.get("advice", "")
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or not isinstance(advice, str) or len(advice) > 1500:
            errors.append(f"{where}: некорректные title/advice")
            continue
        try:
            pattern = _build_pattern(raw)
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"{where}: некорректные codes ({e})")
            continue
        if not pattern or len(pattern) > 1500:
            errors.append(f"{where}: пустой или слишком длинный pattern")
            continue
        if _NESTED_QUANTIFIER.search(pattern):
            errors.append(f"{where}: вложенные квантификаторы в pattern (риск зависания)")
            continue
        try:
            re.compile(pattern, re.I)
        except re.error as e:
            errors.append(f"{where}: ошибка в регулярном выражении ({e})")
            continue
        items.append({"key": key, "pattern": pattern, "level": raw["level"], "title": title.strip(),
                      "advice": advice.strip(), "transient": bool(raw.get("transient", False))})
    return items, errors


def _bundled_path() -> Path:
    here = Path(__file__).with_name("rules.json")
    if here.exists():
        return here
    frozen = getattr(sys, "_MEIPASS", None)  # собранный exe: данные лежат во временной папке распаковки
    return Path(frozen) / "piothelper" / "rules.json" if frozen else here


def _read(path: Path, allow_disabled: bool = False) -> tuple[list[dict], int, list[str]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))  # sig: ваш rules_user.json может быть с BOM
    except (OSError, ValueError) as e:
        return [], 0, [f"{path.name}: {e}"]
    items, errors = validate_rules_doc(doc, allow_disabled)
    version = doc.get("rules_version", 0) if isinstance(doc, dict) else 0
    return items, version if isinstance(version, int) else 0, [f"{path.name}: {e}" for e in errors]


def load_rules() -> tuple[list[Rule], dict]:
    """Склеивает слои: встроенные → скачанные (если новее) → пользовательские."""
    info = {"version": 0, "bundled": 0, "update": 0, "user": 0, "errors": []}
    merged: dict[str, dict] = {}
    base, base_v, errs = _read(_bundled_path())
    info["errors"] += errs
    info["bundled"] = info["version"] = base_v
    for it in base:
        merged[it["key"]] = it
    upd, upd_v, errs = _read(app_dir() / "rules_update.json") if (app_dir() / "rules_update.json").exists() else ([], 0, [])
    info["errors"] += errs
    if upd and upd_v > base_v:
        info["update"] = info["version"] = upd_v
        merged = {it["key"]: it for it in upd}  # скачанный набор полный и заменяет встроенный
    usr, _, errs = _read(app_dir() / "rules_user.json", allow_disabled=True) if (app_dir() / "rules_user.json").exists() else ([], 0, [])
    info["errors"] += errs
    for it in usr:
        if it.get("disabled"):
            merged.pop(it["key"], None)
        else:
            merged[it["key"]] = it
    info["user"] = len(usr)
    rules = [Rule(it["key"], it["pattern"], it["level"], it["title"], it["advice"], it["transient"]) for it in merged.values()]
    return rules, info


def reload_rules() -> None:
    """Перечитывает правила и обновляет RULES/TRANSIENT_KEYS на месте (на них ссылаются другие модули)."""
    rules, info = load_rules()
    RULES[:] = rules
    TRANSIENT_KEYS.clear()
    TRANSIENT_KEYS.update(r.key for r in rules if r.transient)
    RULES_INFO.clear()
    RULES_INFO.update(info)


def update_rules(url: str = DEFAULT_RULES_URL, timeout: float = 15.0) -> str:
    """Скачивает файл правил по кнопке пользователя. Принимает только https и только с GitHub, проверяет содержимое."""
    from . import __version__
    if urllib.parse.urlparse(url).scheme != "https":
        raise ValueError("адрес правил должен начинаться с https://")
    req = urllib.request.Request(url, headers={"User-Agent": f"PIoTHelper/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if urllib.parse.urlparse(resp.geturl()).hostname not in ALLOWED_HOSTS:
            raise ValueError("файл правил должен загружаться с GitHub")
        data = resp.read(MAX_RULES_BYTES + 1)
    if len(data) > MAX_RULES_BYTES:
        raise ValueError("файл правил слишком большой")
    doc = json.loads(data.decode("utf-8"))
    items, errors = validate_rules_doc(doc)
    if errors:
        raise ValueError("файл правил отклонён: " + "; ".join(errors[:3]))
    version = doc.get("rules_version")
    if not isinstance(version, int):
        raise ValueError("в файле нет числовой rules_version")
    current = max(RULES_INFO["bundled"], RULES_INFO["update"])
    if version <= current:
        return f"Правила актуальны (версия {current})."
    before = {r.key for r in RULES}
    target = app_dir() / "rules_update.json"
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    reload_rules()
    added = len({r.key for r in RULES} - before)
    return f"Правила обновлены: версия {current} → {version}, правил: {len(RULES)} (новых: {added})."


reload_rules()
