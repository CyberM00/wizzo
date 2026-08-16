@echo off
REM Double-click launcher for the Wizzo.
REM Checks GitHub for a newer version, then starts the board.
REM Any arguments passed to this file are forwarded, e.g. --no-update or --port 5001

cd /d "%~dp0"
python wizzo.py %*

REM Keep the window open if something went wrong, so the error is readable.
if errorlevel 1 (
  echo.
  echo The kneeboard exited with an error.
  pause
)
