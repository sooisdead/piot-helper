"""Сбор архива для техподдержки (по умолчанию с маскированием личных данных)."""
import json
import platform
import zipfile
from datetime import datetime
from pathlib import Path

from . import __version__, checks, diagnose, logs
from .mask import Masker

MARK = {checks.OK: "[ OK ]", checks.WARN: "[WARN]", checks.FAIL: "[FAIL]", checks.INFO: "[INFO]"}

MASKED_NOTE = (
    "Отчёт ЗАМАСКИРОВАН: лицензионные ключи, заводские номера ККТ/ФН/РНМ, ИНН, коды маркировки, токены и пароли,\n"
    "e-mail, идентификаторы, внешние IP, имя пользователя и компьютера заменены метками вида [SN1], [KEY1].\n"
    "Одинаковые значения получают одинаковые метки. Названия организаций и адреса автоматически не определяются:\n"
    "добавьте их в mask_extra в config.json и просмотрите отчёт перед отправкой.\n"
)
PLAIN_NOTE = "ВНИМАНИЕ: отчёт НЕ замаскирован — в нём лицензионные ключи, заводские номера и другие данные.\n"


def build_report(cfg: dict, out_path: Path, mask: bool | None = None) -> str:
    """Собирает zip. Возвращает строку со статистикой маскирования (пустую, если маскирование выключено)."""
    mask = cfg.get("mask_report", True) if mask is None else mask
    masker = Masker(cfg.get("mask_extra", [])) if mask else None
    clean = masker.text if masker else (lambda s: s)

    summary = checks.run_summary(cfg)
    findings = diagnose.run(cfg)
    header = (
        f"Отчёт PIoT Helper {__version__}, {datetime.now():%d.%m.%Y %H:%M:%S}\n"
        f"Компьютер: {platform.node()}, {platform.platform()}\n"
        f"{MASKED_NOTE if mask else PLAIN_NOTE}\n"
    )
    summary_text = header + "\n".join(f"{MARK[lvl]} {line}" for lvl, line in summary) + "\n"
    diag_text = "\n\n".join(
        f"{MARK.get(f.level, '[    ]')} {f.title}" + (f"\n        {f.detail.replace(chr(10), chr(10) + '        ')}" if f.detail else "")
        for f in findings) + "\n"

    entries: list[tuple[str, str]] = [
        ("summary.txt", summary_text),
        ("diagnostics.txt", "Результат диагностики (вкладка «Исправления»)\n\n" + diag_text),
        ("config.json", json.dumps(cfg, ensure_ascii=False, indent=2)),
    ]
    for entry in cfg["paths"]:
        if entry["kind"] != "log":
            continue
        for i, f in enumerate(logs.list_log_files(entry["path"].strip())[:3]):
            try:
                tail = "\n".join(logs.read_tail(f, cfg["tail_kb"]))
            except OSError:
                continue
            entries.append((f"logs/{entry['name']}/{i + 1}_{f.name}.tail.txt", tail))

    # Маскируем и содержимое, и имена файлов (в именах бывают заводские номера: fptr10-<номер>.log).
    # Один Masker на весь архив: одинаковое значение получает одну и ту же метку во всех файлах.
    if masker:
        for _, text in entries:
            masker.learn(text)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in entries:
            z.writestr(clean(name), clean(text))
    return masker.stats() if masker else ""
