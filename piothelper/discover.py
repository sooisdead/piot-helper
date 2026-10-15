"""Автопоиск путей: реестр, службы Windows и сканирование типовых папок."""
import ctypes
import json
import os
import re
import string
import subprocess
import winreg
import time
from pathlib import Path

from .config import POWERSHELL, keyword_match

SKIP = {
    "windows", "$recycle.bin", "system volume information", "node_modules", ".git", "winsxs", "microsoft",
    "windowsapps", "temp", "packages", "__pycache__", "google", "mozilla", "pip", "npm", "$windows.~bt",
}
UNINSTALL_KEYS = [
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
]


def product_of(text: str, cfg: dict) -> str | None:
    for product, words in cfg["search_keywords"].items():
        if keyword_match(text, words):
            return product
    return None


def _is_log(name: str) -> bool:
    low = name.lower()
    return low.endswith((".log", ".jsonl", ".trace")) or ".log" in low


def _exe_dir(cmd: str) -> str | None:
    """Папка исполняемого файла из строки вида `"C:\\x\\a.exe" -arg` или `C:\\x\\a.exe -arg`."""
    cmd = (cmd or "").strip()
    if not cmd:
        return None
    m = re.match(r'^"([^"]+)"', cmd) or re.match(r"^(.+?\.(?:exe|dll|ico))", cmd, re.I)
    path = (m.group(1) if m else cmd).split(",")[0]
    parent = Path(path).parent
    return str(parent) if parent.is_dir() else None


def _from_registry(cfg: dict):
    for hive, sub in UNINSTALL_KEYS:
        try:
            key = winreg.OpenKey(hive, sub)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as k:
                        def val(n):
                            try:
                                return winreg.QueryValueEx(k, n)[0]
                            except OSError:
                                return ""
                        name, loc = val("DisplayName"), val("InstallLocation")
                        icon, unins, publisher = val("DisplayIcon"), val("UninstallString"), val("Publisher")
                except OSError:
                    continue
                prod = product_of(f"{name} {publisher}", cfg)
                if not prod:
                    continue
                path = loc if loc and Path(loc).is_dir() else (_exe_dir(icon) or _exe_dir(unins))
                if path:
                    yield prod, os.path.normpath(path), f"Установленная программа «{name}»"


def _from_services(cfg: dict):
    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Service | Select-Object Name,DisplayName,PathName | ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=40, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        data = json.loads(out.stdout.decode("utf-8", errors="replace") or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return
    for s in [data] if isinstance(data, dict) else data:
        prod = product_of(f"{s.get('Name')} {s.get('DisplayName')} {s.get('PathName')}", cfg)
        folder = _exe_dir(s.get("PathName") or "")
        windir = os.path.normcase(os.environ.get("SystemRoot", r"C:\Windows"))
        if prod and folder and not os.path.normcase(folder).startswith(windir):  # системные службы не нужны
            yield prod, os.path.normpath(folder), f"Служба «{s.get('DisplayName') or s.get('Name')}»"


def _scan_roots():
    seen, roots = set(), []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "LOCALAPPDATA", "APPDATA", "PUBLIC", "USERPROFILE"):
        p = os.environ.get(var)
        if p and os.path.normcase(p) not in seen:
            seen.add(os.path.normcase(p))
            roots.append((p, 3))
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # фиксированные диски
            roots.append((root, 2))
    return roots


def _from_folders(cfg: dict):
    visited: set[str] = set()
    for root, depth in _scan_roots():
        stack = [(root, 0)]
        while stack:
            cur, d = stack.pop()
            key = os.path.normcase(cur)
            if key in visited:
                continue
            visited.add(key)
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if e.name.lower() in SKIP:
                    continue
                prod = product_of(e.name, cfg)
                if prod:
                    yield prod, os.path.normpath(e.path), "Найдена папка"
                elif d + 1 < depth:
                    stack.append((e.path, d + 1))


def _from_known(cfg: dict):
    for p in cfg.get("known_paths", []):
        if Path(p).is_dir():
            yield "ТС ПИоТ", os.path.normpath(p), "Типовой путь установки ЕСМ"


def _marker_roots():
    return [r for r in (os.environ.get("ProgramData"), os.environ.get("ProgramFiles"),
                        os.environ.get("ProgramFiles(x86)")) if r]


def _from_markers(cfg: dict, roots=None, depth: int = 5, max_dirs: int = 60000):
    """Папки ЕСМ по характерным файлам: ca.crt + server.crt (после регистрации) и config_…ККТ.yml."""
    seen_dirs = 0
    for root in roots or _marker_roots():
        stack = [(root, 0)]
        while stack and seen_dirs < max_dirs:
            cur, d = stack.pop()
            seen_dirs += 1
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            names = {e.name.lower() for e in entries if e.is_file()}
            if {"ca.crt", "server.crt"} <= names:
                yield "ТС ПИоТ", os.path.normpath(cur), "Найдены сертификаты ca.crt/server.crt"
            elif any(re.fullmatch(r"config_.*ккт.*\.ya?ml", n) for n in names):
                yield "ТС ПИоТ", os.path.normpath(cur), "Найден конфиг config_…ККТ.yml"
            if d + 1 < depth:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False) and e.name.lower() not in SKIP:
                            stack.append((e.path, d + 1))
                    except OSError:
                        pass


def find_recent(hours: float, limit: int = 80) -> list[dict]:
    """Папки, созданные или изменённые за последние hours часов, без привязки к названию.
    Нужен, когда программа только что установлена, а как она называется на диске — неизвестно."""
    border = time.time() - hours * 3600
    result, visited = [], set()
    for root, depth in _scan_roots():
        stack = [(root, 0)]
        while stack:
            cur, d = stack.pop()
            if os.path.normcase(cur) in visited:
                continue
            visited.add(os.path.normcase(cur))
            try:
                entries = list(os.scandir(cur))
            except OSError:
                continue
            for e in entries:
                try:
                    if not e.is_dir(follow_symlinks=False) or e.name.lower() in SKIP or e.name.startswith("."):
                        continue
                    st = e.stat()
                except OSError:
                    continue
                created = st.st_ctime >= border  # на Windows ctime — время создания
                if created or st.st_mtime >= border:
                    when = "создана" if created else "изменена"
                    result.append({"product": "Недавнее", "kind": "dir", "path": os.path.normpath(e.path),
                                   "source": f"Папка {when} за последние {hours:g} ч",
                                   "mtime": max(st.st_mtime, st.st_ctime)})
                if d + 1 < min(depth, 2):  # вглубь на 2 уровня: достаточно для ProgramData\Вендор\Продукт
                    stack.append((e.path, d + 1))
    result.sort(key=lambda r: -r["mtime"])
    return result[:limit]


def _find_log_dirs(base: str, limit: int = 400):
    """Верхние по вложенности папки с файлами логов внутри base: [(путь, mtime новейшего лога)]."""
    found, stack, visited = [], [(base, 0)], 0
    while stack and visited < limit:
        cur, d = stack.pop()
        visited += 1
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        newest = None
        for e in entries:
            try:
                if e.is_file() and _is_log(e.name):
                    newest = max(newest or 0, e.stat().st_mtime)
            except OSError:
                pass
        if newest is not None:
            found.append((cur, newest))
            continue
        if d < 3:
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False) and e.name.lower() not in SKIP:
                        stack.append((e.path, d + 1))
                except OSError:
                    pass
    return found


def find_candidates(cfg: dict) -> list[dict]:
    """[{product, kind, path, source, mtime}] — отсортировано по продукту, типу и свежести."""
    from .config import app_dir
    own = os.path.normcase(str(app_dir()))  # саму программу в результатах не показываем
    dirs: dict[str, tuple[str, str]] = {}
    for gen in (_from_registry, _from_services, _from_known, _from_markers, _from_folders):
        try:
            found = list(gen(cfg))
        except Exception:  # noqa: BLE001 — один сбойный источник не должен рушить остальные
            continue
        for prod, path, source in found:
            if os.path.normcase(path) != own:
                dirs.setdefault(os.path.normcase(path), (prod, path, source))
    result, seen = [], set()

    def add(prod, kind, path, source, mtime):
        key = (kind, os.path.normcase(path))
        if key not in seen:
            seen.add(key)
            result.append({"product": prod, "kind": kind, "path": path, "source": source, "mtime": mtime})

    for prod, path, source in dirs.values():
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        add(prod, "dir", path, source, mtime)
        for log_dir, log_mtime in _find_log_dirs(path):
            add(prod, "log", os.path.normpath(log_dir), f"Логи внутри «{Path(path).name}»", log_mtime)
    order = {"dir": 0, "log": 1}
    result.sort(key=lambda r: (r["product"], order[r["kind"]], -(r["mtime"] or 0)))
    return result
