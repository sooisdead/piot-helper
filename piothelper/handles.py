"""Поиск процессов, у которых открыт COM-порт.

Запускается отдельным процессом (python handles.py COM5 COM6): перебор чужих дескрипторов
может зависнуть на редких драйверах, поэтому родитель ждёт с таймаутом и при необходимости убивает его.
Печатает в stdout JSON. Без прав администратора видны только процессы того же пользователя.
"""
import ctypes
import json
import os
import sys
from ctypes import wintypes as wt

ntdll = ctypes.WinDLL("ntdll")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)

PVOID = ctypes.c_void_p
k32.GetCurrentProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.DuplicateHandle.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE, ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.BOOL, wt.DWORD]
k32.DuplicateHandle.restype = wt.BOOL
k32.GetFileType.argtypes = [wt.HANDLE]
k32.GetFileType.restype = wt.DWORD
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, PVOID, wt.DWORD, wt.DWORD, PVOID]
k32.CreateFileW.restype = wt.HANDLE
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
k32.QueryDosDeviceW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
ntdll.NtQuerySystemInformation.argtypes = [wt.ULONG, PVOID, wt.ULONG, ctypes.POINTER(wt.ULONG)]
ntdll.NtQuerySystemInformation.restype = ctypes.c_long
ntdll.NtQueryObject.argtypes = [wt.HANDLE, wt.ULONG, PVOID, wt.ULONG, ctypes.POINTER(wt.ULONG)]
ntdll.NtQueryObject.restype = ctypes.c_long

SYSTEM_EXTENDED_HANDLE_INFORMATION = 0x40
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
FILE_TYPE_CHAR = 2
# Права доступа дескрипторов, на которых NtQueryObject известным образом виснет (синхронные каналы).
HANG_ACCESS = {0x0012019F, 0x001A019F, 0x00120189, 0x00100000}


class HandleEntry(ctypes.Structure):
    _fields_ = [
        ("Object", PVOID), ("Pid", ctypes.c_size_t), ("Handle", ctypes.c_size_t), ("Access", wt.ULONG),
        ("Backtrace", wt.USHORT), ("TypeIndex", wt.USHORT), ("Attributes", wt.ULONG), ("Reserved", wt.ULONG),
    ]


def system_handles():
    size = 1 << 22
    while True:
        buf = ctypes.create_string_buffer(size)
        ret = wt.ULONG()
        status = ntdll.NtQuerySystemInformation(SYSTEM_EXTENDED_HANDLE_INFORMATION, buf, size, ctypes.byref(ret))
        status &= 0xFFFFFFFF
        if status == STATUS_INFO_LENGTH_MISMATCH:
            size = max(size * 2, ret.value + (1 << 20))
            continue
        if status != 0:
            raise OSError(f"NtQuerySystemInformation: 0x{status:08x}")
        count = ctypes.c_size_t.from_buffer(buf).value
        return (HandleEntry * count).from_buffer(buf, 16)  # заголовок: 2 * size_t


def enable_debug_privilege() -> None:
    class LUID(ctypes.Structure):
        _fields_ = [("Low", wt.DWORD), ("High", wt.LONG)]

    class TokenPriv(ctypes.Structure):
        _fields_ = [("Count", wt.DWORD), ("Luid", LUID), ("Attr", wt.DWORD)]

    advapi.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
    advapi.AdjustTokenPrivileges.argtypes = [wt.HANDLE, wt.BOOL, PVOID, wt.DWORD, PVOID, PVOID]
    token = wt.HANDLE()
    if not advapi.OpenProcessToken(k32.GetCurrentProcess(), 0x28, ctypes.byref(token)):
        return
    luid = LUID()
    if advapi.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
        tp = TokenPriv(1, luid, 2)  # SE_PRIVILEGE_ENABLED
        advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), ctypes.sizeof(tp), None, None)
    k32.CloseHandle(token)


def object_name(handle) -> str:
    buf = ctypes.create_string_buffer(2048)
    ret = wt.ULONG()
    if ntdll.NtQueryObject(handle, 1, buf, 2048, ctypes.byref(ret)) != 0:
        return ""
    length = int.from_bytes(buf.raw[:2], "little")
    ptr = ctypes.c_void_p.from_buffer(buf, 8).value
    return ctypes.wstring_at(ptr, length // 2) if ptr and length else ""


def process_path(pid: int) -> str:
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        k32.CloseHandle(h)


def find_owners(ports: list[str]) -> dict:
    # NT-имя устройства для каждого COM: COM5 -> \Device\Serial0 (у USB-адаптеров бывают \Device\VCP0 и т.п.)
    targets = {}
    for port in ports:
        buf = ctypes.create_unicode_buffer(1024)
        if k32.QueryDosDeviceW(port, buf, 1024):
            targets[buf.value.lower()] = port
    result = {"owners": {p: [] for p in ports}, "devices": {v: k for k, v in targets.items()}, "scanned": 0}
    if not targets:
        return result

    # Индекс типа объекта «File»: открываем свой файл и ищем свой дескриптор в таблице.
    # Любой реально существующий файл (__file__ внутри собранного exe файлом не является).
    own = k32.CreateFileW(os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), 0x80000000, 7, None, 3, 0, None)
    entries = system_handles()
    my_pid = os.getpid()
    file_type = next((e.TypeIndex for e in entries if e.Pid == my_pid and e.Handle == own), None)
    k32.CloseHandle(own)
    if file_type is None:
        raise OSError("не удалось определить тип объекта File")

    me = k32.GetCurrentProcess()
    opened: dict[int, object] = {}
    seen = set()
    for e in entries:
        if e.TypeIndex != file_type or e.Pid == my_pid or e.Access in HANG_ACCESS:
            continue
        if e.Pid not in opened:
            opened[e.Pid] = k32.OpenProcess(0x40, False, e.Pid) or None  # PROCESS_DUP_HANDLE
        proc = opened[e.Pid]
        if not proc:
            continue
        dup = wt.HANDLE()
        if not k32.DuplicateHandle(proc, e.Handle, me, ctypes.byref(dup), 0, False, 2):
            continue
        try:
            result["scanned"] += 1
            if k32.GetFileType(dup) != FILE_TYPE_CHAR:
                continue
            port = targets.get(object_name(dup).lower())
            if port and (port, e.Pid) not in seen:
                seen.add((port, e.Pid))
                path = process_path(e.Pid)
                result["owners"][port].append({"pid": e.Pid, "name": os.path.basename(path) or f"PID {e.Pid}", "path": path})
        finally:
            k32.CloseHandle(dup)
    for h in opened.values():
        if h:
            k32.CloseHandle(h)
    return result


def main(args: list[str]) -> None:
    """args: [--out ФАЙЛ] COM5 COM6 … Результат — JSON в файл (надёжно и для exe без консоли) или в stdout."""
    out_path = None
    if args[:1] == ["--out"]:
        out_path, args = args[1], args[2:]
    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    if admin:
        enable_debug_privilege()
    try:
        out = find_owners([p.upper() for p in args])
        out["admin"] = admin
    except Exception as exc:  # noqa: BLE001
        out = {"error": str(exc), "admin": admin, "owners": {}}
    text = json.dumps(out, ensure_ascii=False)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main(sys.argv[1:])
