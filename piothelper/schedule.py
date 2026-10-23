"""Ночной перезапуск служб: задача Планировщика Windows, которая запускает нашу программу в режиме --scheduled-restart.

Задача создаётся из XML (так можно задать то, чего нет в параметрах schtasks): запуск от SYSTEM с высшими правами (работает без
вошедшего пользователя), не откладывать при питании от батареи, не запускать вторую копию, прервать через 2 часа, если что-то
зависло. Если компьютер в это время выключен, задача пропускается: перезапускать службы сразу после загрузки бессмысленно.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import fixes
from .config import app_dir

TASK_NAME = "PIoTHelper-NightlyRestart"
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
NEVER_RAN = 267011  # 0x41303: «задача ещё не запускалась»
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCHTASKS = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "schtasks.exe")


def valid_time(text: str) -> bool:
    return bool(TIME_RE.fullmatch(text.strip()))


def command() -> tuple[str, str]:
    """(программа, аргументы) для задачи: собранный exe сам, из исходников — python main.py."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--scheduled-restart"
    return sys.executable, f'"{app_dir() / "main.py"}" --scheduled-restart'


def build_xml(time_text: str, system: bool = True, program: str | None = None, arguments: str | None = None) -> str:
    """XML задачи (Task Scheduler 1.2). system=False — от текущего пользователя (для проверки без прав администратора)."""
    prog, args = (program, arguments) if program else command()
    principal = ("<UserId>S-1-5-18</UserId><RunLevel>HighestAvailable</RunLevel>" if system
                 else "<LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel>")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>PIoT Helper: ежедневный перезапуск служб ЕСМ (ТС ПИоТ), ждёт паузы в продажах</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T{time_text.strip()}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author">{principal}</Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>false</StartWhenAvailable>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(prog)}</Command><Arguments>{escape(args)}</Arguments></Exec>
  </Actions>
</Task>
"""


def _schtasks(args: list[str], timeout: int = 40) -> subprocess.CompletedProcess:
    return subprocess.run([SCHTASKS, *args], capture_output=True, timeout=timeout, creationflags=NO_WINDOW)


def _text(data: bytes) -> str:
    for enc in ("utf-8", "cp866", "cp1251"):
        try:
            return data.decode(enc).strip()
        except UnicodeError:
            continue
    return data.decode("utf-8", errors="replace").strip()


def install(time_text: str, system: bool = True, name: str = TASK_NAME, program: str | None = None,
            arguments: str | None = None) -> str:
    """Создаёт или заменяет задачу. Для system=True нужны права администратора."""
    if not valid_time(time_text):
        raise ValueError("время должно быть в формате ЧЧ:ММ, например 05:00")
    fd, tmp = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(tmp).write_bytes(build_xml(time_text, system, program, arguments).encode("utf-16"))  # utf-16 с BOM
        res = _schtasks(["/Create", "/TN", name, "/XML", tmp, "/F"])
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if res.returncode != 0:
        raise RuntimeError(_text(res.stderr or res.stdout) or f"schtasks вернул код {res.returncode}")
    fixes._log(f"планировщик: задача {name} создана на {time_text}")
    return f"Задача «{name}» создана: каждый день в {time_text.strip()}."


def remove(name: str = TASK_NAME) -> str:
    res = _schtasks(["/Delete", "/TN", name, "/F"])
    if res.returncode != 0 and status(name) is not None:
        raise RuntimeError(_text(res.stderr or res.stdout) or f"schtasks вернул код {res.returncode}")
    fixes._log(f"планировщик: задача {name} удалена")
    return f"Задача «{name}» удалена."


STATUS_SCRIPT = (
    "$t = Get-ScheduledTask -TaskName $env:PIOT_ARG -ErrorAction Stop; "
    "$i = Get-ScheduledTaskInfo -InputObject $t; "
    "[pscustomobject]@{State=[string]$t.State; Program=$t.Actions[0].Execute; Arguments=$t.Actions[0].Arguments; "
    "Start=[string]$t.Triggers[0].StartBoundary; LastRun=$i.LastRunTime.ToString('s'); LastResult=[int64]$i.LastTaskResult; "
    "NextRun=$i.NextRunTime.ToString('s')} | ConvertTo-Json -Compress"
)


def status(name: str = TASK_NAME) -> dict | None:
    """None — задачи нет. Иначе словарь: state, program, arguments, time (ЧЧ:ММ), last_run, last_result, next_run
    (поля могут быть None: на Windows 7 командлетов планировщика нет, тогда известно только, что задача существует)."""
    if _schtasks(["/Query", "/TN", name]).returncode != 0:
        return None
    info = {"state": None, "program": None, "arguments": None, "time": None, "last_run": None, "last_result": None, "next_run": None}
    try:
        raw = json.loads(fixes.ps(STATUS_SCRIPT, name, timeout=40))
    except Exception:  # noqa: BLE001 — командлетов нет или ответ нечитаем: хотя бы сам факт наличия задачи
        return info
    start = raw.get("Start") or ""
    m = re.search(r"T(\d{2}:\d{2})", start)
    never = lambda s: not s or s.startswith(("1999", "1900", "0001"))  # noqa: E731 — «ни разу не запускалась»
    info.update(state=raw.get("State"), program=raw.get("Program"), arguments=raw.get("Arguments"),
                time=m.group(1) if m else None, last_result=raw.get("LastResult"),
                last_run=None if never(raw.get("LastRun")) else raw["LastRun"],
                next_run=None if never(raw.get("NextRun")) else raw["NextRun"])
    return info


def points_to_this_program(info: dict) -> bool:
    """Указывает ли задача на текущий exe/скрипт (после переноса программы задача молча перестаёт работать)."""
    prog, _ = command()
    return bool(info.get("program")) and os.path.normcase(str(info["program"]).strip('"')) == os.path.normcase(prog)
