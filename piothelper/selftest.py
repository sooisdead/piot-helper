"""Самопроверка собранного exe без окна: PIoTHelper.exe --selftest --out файл.json"""
import json
import sys

from . import __version__, checks, config, discover, knowledge, portowner, tray


def run(out_path: str) -> None:
    res: dict = {"version": __version__, "frozen": bool(getattr(sys, "frozen", False)), "app_dir": str(config.app_dir())}

    def step(name, fn):
        try:
            res[name] = fn()
        except Exception as e:  # noqa: BLE001
            res[name] = f"ОШИБКА: {e}"

    cfg = config.load()
    step("services", lambda: len(checks.list_services(["windows"])))
    step("com_ports", lambda: portowner.list_com_ports())
    def scan():
        r = portowner.find_com_owners(["NUL"])  # устройство NUL: у него всегда есть владельцы
        return {"error": r.get("error"), "scanned": r.get("scanned"), "NUL_owners": len(r.get("owners", {}).get("NUL", []))}
    step("com_owner_scan_NUL", scan)
    step("tcp_owners_135", lambda: len(portowner.tcp_owners("", 135)))
    step("discover_known_keywords", lambda: discover.product_of("ProgramData\\ESP\\ESM", cfg))
    step("rules", lambda: len(knowledge.RULES))  # 0 — значит, rules.json не попал в сборку

    def tray_check():
        icon = tray.Tray(lambda: None, lambda: None, lambda: None)
        ok = icon.start()
        icon.stop()
        return bool(ok)
    step("tray_window", tray_check)
    step("is_admin", portowner.is_admin)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
