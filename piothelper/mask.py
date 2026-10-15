"""Маскирование личных данных в отчёте для поддержки.

Замены постоянные внутри одного отчёта (одинаковое значение → одинаковая метка [SN1]), поэтому по маскированным
логам по-прежнему видно, что речь об одной и той же кассе/ключе. Автоматическая маскировка не даёт гарантий:
названия организаций, адреса и т.п. она не знает — для них есть список mask_extra в config.json.
"""
import collections
import getpass
import ipaddress
import os
import platform
import re
from typing import Iterable

# (категория, шаблон). Порядок важен: сначала секреты и коды маркировки, потом общие числа.
SECRET_KV = re.compile(
    r'(?i)("?\b(?:token|password|passwd|pwd|secret|api[_-]?key|authorization|x-clientid|x-api-key|access[_-]?key|'
    r'client[_-]?secret)\b"?\s*[:=]\s*"?)([^"\s,;}&]{3,})')
KM_PLAIN = re.compile(r"01\d{14}21[\x21-\x7e\x1d]{1,40}")      # код маркировки: 01+GTIN+21+серийный номер…
KM_B64 = re.compile(r"MDEw[A-Za-z0-9+/]{20,}={0,2}")           # он же в base64 (так его присылает Frontol)
LICENSE_KEY = re.compile(r"\b[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}\b")
LONG_NUMBER = re.compile(r"(?<!\d)(?<!\d\.)\d{14,20}(?!\d)(?!\.\d)")  # заводские номера ККТ/ФН, РНМ (точка в конце — не дробь)
INN_CTX = re.compile(r"(?i)((?:inn|инн|vatin)\W{0,14})(\d{10}|\d{12})(?!\d)")  # ecr_inn, producerInn, Vatin, «ИНН 123…»
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
DATETIME14 = re.compile(r"^20\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{6}$")  # 20261015144416 — это дата, не номер


class Masker:
    def __init__(self, extra: Iterable[str] = ()):
        self.maps: dict[str, dict[str, str]] = collections.defaultdict(dict)
        literals = [os.environ.get("COMPUTERNAME", ""), platform.node(), _user()] + list(extra)
        # длинные первыми, чтобы короткое имя не «съело» часть длинного
        self.literals = sorted({s for s in literals if s and len(s) >= 3}, key=len, reverse=True)

    def _tag(self, cat: str, value: str) -> str:
        m = self.maps[cat]
        if value not in m:
            m[value] = f"[{cat}{len(m) + 1}]"  # без < > : — они недопустимы в именах файлов Windows
        return m[value]

    def learn(self, s: str) -> None:
        """Предварительный проход: ИНН, найденные по подписи, потом маскируются и там, где подписи рядом нет."""
        for m in INN_CTX.finditer(s):
            self._tag("INN", m.group(2))

    def text(self, s: str) -> str:
        for value in self.maps.get("INN", {}):
            if value in s:
                s = re.sub(rf"(?<!\d){value}(?!\d)", self.maps["INN"][value], s)
        for lit in self.literals:
            s = re.sub(re.escape(lit), lambda _m: self._tag("NAME", lit.lower()), s, flags=re.I)
        s = SECRET_KV.sub(lambda m: m.group(1) + self._tag("SECRET", m.group(2)), s)
        s = KM_PLAIN.sub(lambda m: self._tag("КМ", m.group(0)), s)
        s = KM_B64.sub(lambda m: self._tag("КМ64", m.group(0)), s)
        s = LICENSE_KEY.sub(lambda m: self._tag("KEY", m.group(0)), s)
        s = INN_CTX.sub(lambda m: m.group(1) + self._tag("INN", m.group(2)), s)
        s = LONG_NUMBER.sub(lambda m: m.group(0) if DATETIME14.match(m.group(0)) else self._tag("SN", m.group(0)), s)
        s = EMAIL.sub(lambda m: self._tag("EMAIL", m.group(0)), s)
        s = UUID.sub(lambda m: self._tag("UUID", m.group(0).lower()), s)
        s = IPV4.sub(self._ip, s)
        return s

    def _ip(self, m: re.Match) -> str:
        try:
            ip = ipaddress.ip_address(m.group(0))
        except ValueError:
            return m.group(0)
        return self._tag("IP", m.group(0)) if ip.is_global else m.group(0)  # внутренние и 127.0.0.1 оставляем

    def stats(self) -> str:
        names = {"NAME": "имён", "SECRET": "токенов/паролей", "КМ": "кодов маркировки", "КМ64": "кодов маркировки (base64)",
                 "KEY": "лицензионных ключей", "INN": "ИНН", "SN": "длинных номеров (заводские, ФН, РНМ)",
                 "EMAIL": "e-mail", "UUID": "идентификаторов", "IP": "внешних IP"}
        parts = [f"{names[c]}: {len(v)}" for c, v in self.maps.items() if v]
        return ", ".join(parts) if parts else "ничего не найдено"


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USERNAME", "")
