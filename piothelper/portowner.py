"""Что занимает порт ККМ: COM-порты (состояние + процесс) и TCP-подключения."""
import ctypes
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import winreg
from ctypes import wintypes as wt
from pathlib import Path

from .config import POWERSHELL

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def is_admin() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


def relaunch_as_admin() -> None:
    from .config import app_dir
    if getattr(sys, "frozen", False):  # собранный exe запускаем сам
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, None, str(app_dir()), 1)
        return
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":  # без чёрного окна консоли
        exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), f'"{app_dir() / "main.py"}"', str(app_dir()), 1)


def _powershell(script: str, timeout: int = 40):
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;" + script
    out = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", full],
                         capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    text = out.stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    data = json.loads(text)
    return [data] if isinstance(data, dict) else data


def list_com_ports() -> list[dict]:
    """[{port, device}] — по реестру SERIALCOMM и именам PnP-устройств."""
    ports: dict[str, str] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as key:
            for i in range(winreg.QueryInfoKey(key)[1]):
                ports[str(winreg.EnumValue(key, i)[1]).upper()] = ""
    except OSError:
        pass
    try:
        rows = _powershell("Get-CimInstance Win32_PnPEntity | Where-Object { $_.Name -match '\\(COM\\d+\\)' } "
                           "| Select-Object Name | ConvertTo-Json -Compress")
        for r in rows:
            m = re.search(r"\((COM\d+)\)", r["Name"])
            if m:
                ports[m.group(1).upper()] = r["Name"]
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return [{"port": p, "device": ports[p]} for p in sorted(ports, key=lambda s: int(s[3:]))]


def com_state(port: str) -> str:
    """Пробное открытие порта с эксклюзивным доступом: «свободен» / «занят» / причина."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.c_void_p]
    k32.CreateFileW.restype = wt.HANDLE
    k32.CloseHandle.argtypes = [wt.HANDLE]
    h = k32.CreateFileW(f"\\\\.\\{port}", 0xC0000000, 0, None, 3, 0, None)
    if h in (None, ctypes.c_void_p(-1).value):
        err = ctypes.get_last_error()
        return {5: "занят", 2: "порта нет", 1167: "устройство отключено"}.get(err, f"недоступен (код {err})")
    k32.CloseHandle(h)
    return "свободен"


def find_com_owners(ports: list[str], timeout: int = 90) -> dict:
    """Запускает handles.py отдельным процессом. Результат: {owners: {COM5: [...]}, admin: bool, error?: str}."""
    fd, out_path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        if getattr(sys, "frozen", False):  # собранный exe запускает сам себя в режиме --handles
            cmd = [sys.executable, "--handles", "--out", out_path, *ports]
        else:
            py = Path(sys.executable)
            if py.name.lower() == "pythonw.exe":
                py = py.with_name("python.exe")
            cmd = [str(py), str(Path(__file__).with_name("handles.py")), "--out", out_path, *ports]
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        text = Path(out_path).read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(proc.stderr.decode("utf-8", errors="replace").strip() or "пустой ответ")
        return json.loads(text)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def parse_endpoint(text: str) -> tuple[str, int]:
    """'5555' -> ('', 5555); '192.168.1.50:5555' -> ('192.168.1.50', 5555)."""
    text = text.strip()
    host, _, port = text.rpartition(":")
    if host:
        host = str(ipaddress.ip_address(host.strip("[]")))
    port_n = int(port)
    if not 0 < port_n < 65536:
        raise ValueError("порт должен быть от 1 до 65535")
    return host, port_n


def tcp_owners(host: str, port: int) -> list[dict]:
    """TCP-соединения, где локальный порт равен port, либо удалённый (и адрес, если задан)."""
    remote = f" -and $_.RemoteAddress -eq '{host}'" if host else ""
    script = (
        f"Get-NetTCPConnection -ErrorAction SilentlyContinue | Where-Object {{ $_.LocalPort -eq {int(port)} -or "
        f"($_.RemotePort -eq {int(port)}{remote}) }} | ForEach-Object {{ "
        "$p = Get-CimInstance Win32_Process -Filter \"ProcessId=$($_.OwningProcess)\" -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{Local=\"$($_.LocalAddress):$($_.LocalPort)\";Remote=\"$($_.RemoteAddress):$($_.RemotePort)\";"
        "State=\"$($_.State)\";Pid=$_.OwningProcess;Name=$p.Name;Path=$p.ExecutablePath} } | ConvertTo-Json -Compress"
    )
    return _powershell(script)
