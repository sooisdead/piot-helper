"""Значок в области уведомлений Windows на чистом ctypes (без сторонних библиотек).

Свой поток с скрытым окном и циклом сообщений. Состояние значка меняется стандартными иконками Windows
(информация / предупреждение / ошибка), уведомления — «пузырь» Shell_NotifyIcon. Из других потоков пользоваться
только методами set_state / notify / stop: они ставят задачу в очередь и будят цикл сообщений.
"""
import ctypes
import queue
import threading
from ctypes import wintypes as wt
from typing import Callable

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WM_DESTROY, WM_COMMAND, WM_APP = 0x0002, 0x0111, 0x8000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
NIN_BALLOONUSERCLICK = 0x0405
WM_TRAY = WM_APP + 1        # события значка
WM_WORK = WM_APP + 2        # задача из очереди
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
NIIF_INFO, NIIF_WARNING, NIIF_ERROR = 1, 2, 3
MF_STRING, MF_SEPARATOR = 0x0, 0x800
TPM_RIGHTBUTTON, TPM_BOTTOMALIGN = 0x2, 0x20
ID_OPEN, ID_CHECK, ID_EXIT = 1001, 1002, 1003
STOCK_ICON = {"ok": 32516, "warn": 32515, "fail": 32513}      # IDI_INFORMATION / IDI_WARNING / IDI_ERROR
BALLOON_ICON = {"ok": NIIF_INFO, "warn": NIIF_WARNING, "fail": NIIF_ERROR}


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT), ("uCallbackMessage", wt.UINT),
                ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128), ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD),
                ("szInfo", wt.WCHAR * 256), ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM), ("lParam", wt.LPARAM), ("time", wt.DWORD),
                ("pt", wt.POINT)]


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.DestroyWindow.argtypes = [wt.HWND]
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadIconW.restype = wt.HICON
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.PostQuitMessage.argtypes = [ctypes.c_int]
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wt.BOOL
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE

_class_counter = 0


class Tray:
    """Значок в трее. on_open / on_check / on_exit вызываются из потока значка: переводите их в поток окна сами."""

    def __init__(self, on_open: Callable[[], None], on_check: Callable[[], None], on_exit: Callable[[], None],
                 tooltip: str = "PIoT Helper"):
        self.on_open, self.on_check, self.on_exit, self.tooltip = on_open, on_check, on_exit, tooltip[:127]
        self.hwnd = None
        self.icon_added = False
        self.level = "ok"
        self._queue: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._wndproc = WNDPROC(self._proc)  # ссылку держим, иначе сборщик мусора убьёт callback
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")

    # ----- публичный интерфейс (из любых потоков) -----

    def start(self, timeout: float = 5.0) -> bool:
        self._thread = threading.Thread(target=self._run, name="piot-tray", daemon=True)
        self._thread.start()
        return self._ready.wait(timeout) and bool(self.hwnd)

    def set_state(self, level: str, tooltip: str | None = None) -> None:
        def work():
            self.level = level
            if tooltip:
                self.tooltip = tooltip[:127]
            self._modify(NIF_ICON | NIF_TIP)
        self._post(work)

    def notify(self, title: str, text: str, level: str = "warn") -> None:
        def work():
            data = self._data(NIF_INFO)
            data.szInfoTitle = title[:63]
            data.szInfo = text[:255]
            data.dwInfoFlags = BALLOON_ICON.get(level, NIIF_INFO)
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))
        self._post(work)

    def stop(self) -> None:
        self._post(lambda: user32.DestroyWindow(self.hwnd))
        if self._thread:
            self._thread.join(3)

    # ----- внутреннее -----

    def _post(self, fn: Callable[[], None]) -> None:
        self._queue.put(fn)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_WORK, 0, 0)

    def _data(self, flags: int) -> NOTIFYICONDATAW:
        d = NOTIFYICONDATAW()
        d.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        d.hWnd, d.uID, d.uFlags = self.hwnd, 1, flags
        d.uCallbackMessage = WM_TRAY
        d.hIcon = user32.LoadIconW(None, ctypes.cast(STOCK_ICON.get(self.level, STOCK_ICON["ok"]), wt.LPCWSTR))
        d.szTip = self.tooltip
        return d

    def _modify(self, flags: int) -> None:
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._data(flags)))

    def _add(self) -> None:
        self.icon_added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._data(NIF_MESSAGE | NIF_ICON | NIF_TIP))))

    def _run(self) -> None:
        global _class_counter
        _class_counter += 1
        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = f"PIoTHelperTray{_class_counter}"
        if user32.RegisterClassW(ctypes.byref(wc)):
            self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "PIoT Helper tray", 0, 0, 0, 0, 0, None, None, wc.hInstance, None)
        if self.hwnd:
            self._add()
        self._ready.set()
        if not self.hwnd:
            return
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
                    self.on_open()
                elif event == WM_RBUTTONUP:
                    self._menu(hwnd)
                return 0
            if msg == WM_COMMAND:
                {ID_OPEN: self.on_open, ID_CHECK: self.on_check, ID_EXIT: self.on_exit}.get(wparam & 0xFFFF, lambda: None)()
                return 0
            if msg == WM_WORK:
                while True:
                    try:
                        self._queue.get_nowait()()
                    except queue.Empty:
                        break
                return 0
            if msg == self._taskbar_created:  # Проводник перезапустился — вернуть значок
                self._add()
                return 0
            if msg == WM_DESTROY:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data(0)))
                user32.PostQuitMessage(0)
                return 0
        except Exception:  # noqa: BLE001 — исключение в callback Windows недопустимо
            pass
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self, hwnd) -> None:
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Открыть PIoT Helper")
        user32.AppendMenuW(menu, MF_STRING, ID_CHECK, "Проверить сейчас")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "Выход")
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(hwnd)  # без этого меню не закрывается кликом мимо
        user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_BOTTOMALIGN, pt.x, pt.y, 0, hwnd, None)
        user32.DestroyMenu(menu)
