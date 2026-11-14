@echo off
rem Agent Duet installer for Windows. Extra arguments go to scripts\install.py,
rem for example:  install-windows.cmd --default-mode work
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.10+ was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
%PY% -X utf8 "%~dp0scripts\install.py" %*
if errorlevel 1 (
  echo Installation failed. Read the error above.
) else (
  echo Installed. Reload Codex and Claude once to load the tools.
)
pause
