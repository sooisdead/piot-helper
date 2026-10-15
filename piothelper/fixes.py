"""Действия, меняющие систему. Вызываются только после подтверждения пользователя; каждое пишется в fixes.log."""
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import POWERSHELL, app_dir

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log_path() -> Path:
    return app_dir() / "fixes.log"


def _log(text: str) -> None:
    try:
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {text}\n")
    except OSError:
        pass


def _clean_err(text: str) -> str:
    """Ошибки PowerShell при перенаправлении приходят как CLIXML — достаём читаемый текст."""
    if text.startswith("#< CLIXML"):
        parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
        text = "".join(parts).replace("_x000D__x000A_", "\n")
    return text.strip()


def ps(script: str, arg: str = "", timeout: int = 60) -> str:
    """PowerShell со значением в $env:PIOT_ARG — так имена и пути не попадают в текст команды."""
    full = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ErrorActionPreference='Stop';" + script
    p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", full],
                       env={**os.environ, "PIOT_ARG": arg}, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    out = p.stdout.decode("utf-8", errors="replace").strip()
    if p.returncode != 0:
        err = _clean_err(p.stderr.decode("utf-8", errors="replace")) or out or f"код {p.returncode}"
        raise RuntimeError(err.splitlines()[0])  # остальное — служебная трассировка PowerShell
    return out


def _do(action: str, fn):
    try:
        result = fn()
    except Exception as e:  # noqa: BLE001
        _log(f"{action}: ОШИБКА {e}")
        raise
    _log(f"{action}: OK {result}")
    return result


WAIT = "00:00:40"  # сколько ждём нужного состояния службы


def start_service(name: str) -> str:
    return _do(f"запуск службы {name}", lambda: "состояние: " + ps(
        "$s = Get-Service -Name $env:PIOT_ARG; if ($s.Status -ne 'Running') { Start-Service -Name $env:PIOT_ARG };"
        f"$s.Refresh(); $s.WaitForStatus('Running','{WAIT}'); $s.Status", name, timeout=90))


def stop_service(name: str) -> str:
    return _do(f"остановка службы {name}", lambda: "состояние: " + ps(
        "$s = Get-Service -Name $env:PIOT_ARG; if ($s.Status -ne 'Stopped') { Stop-Service -Name $env:PIOT_ARG -Force };"
        f"$s.Refresh(); $s.WaitForStatus('Stopped','{WAIT}'); $s.Status", name, timeout=90))


def restart_service(name: str) -> str:
    return _do(f"перезапуск службы {name}", lambda: "состояние: " + ps(
        "Restart-Service -Name $env:PIOT_ARG -Force; (Get-Service -Name $env:PIOT_ARG).Status", name))


def import_cert(path: str) -> str:
    def run():
        ps("Import-Certificate -FilePath $env:PIOT_ARG -CertStoreLocation Cert:\\LocalMachine\\Root | Out-Null", path)
        return "сертификат добавлен в доверенные корневые (LocalMachine\\Root)"
    return _do(f"импорт сертификата {path}", run)


def sync_time() -> str:
    script = ("Start-Service w32time -ErrorAction SilentlyContinue; $o = w32tm /resync /force 2>&1 | Out-String; "
              "if ($LASTEXITCODE -ne 0) { throw $o }; $o")
    return _do("синхронизация времени", lambda: ps(script, timeout=60))


def set_cdn_timeout(path: str, value: int) -> str:
    def run():
        p = Path(path)
        raw = p.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        try:
            text, enc = raw.decode("utf-8-sig"), "utf-8"
        except UnicodeError:
            text, enc = raw.decode("cp1251"), "cp1251"
        new, n = re.subn(r"(cdnCodesCheckTimeout\s*:\s*)\d+", rf"\g<1>{int(value)}", text)
        if not n:
            raise RuntimeError("параметр cdnCodesCheckTimeout в файле не найден")
        backup = p.with_name(f"{p.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_bytes(raw)
        p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode(enc))
        return f"cdnCodesCheckTimeout = {value}; резервная копия: {backup.name}. Перезапустите службу ЕСМ."
    return _do(f"правка {path}", run)


def kill_process(pid: int) -> str:
    def run():
        p = subprocess.run(["taskkill", "/PID", str(int(pid)), "/F"], capture_output=True, creationflags=NO_WINDOW)
        text = (p.stdout or p.stderr).decode("cp866", errors="replace").strip()
        if p.returncode != 0:
            raise RuntimeError(text)
        return text
    return _do(f"завершение процесса {pid}", run)
