@echo off
rem Builds dist\PIoTHelper.exe (one file, no console, no Python needed on the target PC).
cd /d "%~dp0"

set PY=py
where py >nul 2>&1 || set PY=python

%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller...
  %PY% -m pip install pyinstaller || goto :fail
)

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name PIoTHelper ^
  --add-data "%~dp0piothelper\rules.json;piothelper" ^
  --distpath dist --workpath build --specpath build main.py || goto :fail

echo.
echo Done: %~dp0dist\PIoTHelper.exe
exit /b 0

:fail
echo Build failed.
pause
exit /b 1
