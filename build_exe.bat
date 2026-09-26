@echo off
rem ------------------------------------------------------------------
rem  Rebuilds CompetitorMonitor.exe from launcher.py.
rem
rem  You only need this if launcher.py itself changes, or the exe is
rem  missing (for example after cloning the project from GitHub).
rem  Editing monitor.py, competitors.txt, selectors.json or .env does
rem  NOT need a rebuild - the exe runs those files fresh every time.
rem ------------------------------------------------------------------
cd /d "%~dp0"

set PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if not exist "%PY%" set PY=py -3

%PY% -m pip install --quiet pyinstaller
%PY% -m PyInstaller --noconfirm --onefile --console ^
    --name CompetitorMonitor ^
    --distpath . --workpath build --specpath build ^
    launcher.py

if exist CompetitorMonitor.exe (
    echo.
    echo   Built CompetitorMonitor.exe
) else (
    echo.
    echo   Build failed - see the messages above.
)
pause
