@echo off
cd /d "%~dp0"

rem 1) launcher "py" (installed with Python by default, PATH is not needed)
where pyw >nul 2>&1
if %errorlevel%==0 (
  start "" pyw main.py
  exit /b
)

rem 2) pythonw from PATH
where pythonw >nul 2>&1
if %errorlevel%==0 (
  start "" pythonw main.py
  exit /b
)

rem 3) standard install folders (per-user and for all users)
for /d %%D in ("%LocalAppData%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "%ProgramFiles(x86)%\Python3*") do (
  if exist "%%~D\pythonw.exe" (
    start "" "%%~D\pythonw.exe" main.py
    exit /b
  )
)

echo Python was not found.
echo Install Python 3.10+ from python.org and tick "Add python.exe to PATH"
echo and the "tcl/tk and IDLE" component, then run this file again.
pause
