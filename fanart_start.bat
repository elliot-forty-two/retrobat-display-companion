@echo off
setlocal

cd /D "%~dp0"

tasklist /FI "WINDOWTITLE eq RetroBat Backglass" | find /I "mpv.exe" >nul
if not errorlevel 1 exit /b

set BACKGLASS_PIPE=\\.\pipe\retrobat_backglass
start "" "mpv\mpv.exe" ^
  --no-taskbar-progress --input-gamepad=yes --no-osc --loop-file=inf --alpha=yes --no-audio --no-input-cursor --no-input-default-bindings --idle --player-operation-mode=pseudo-gui ^
  --fs --fs-screen=1 ^
  --keep-open=yes ^
  --idle=yes ^
  --input-ipc-server=%BACKGLASS_PIPE% ^
  --image-display-duration=inf ^
  --force-window=yes ^
  --title="RetroBat Backglass"

set DMD_PIPE=\\.\pipe\retrobat_dmd
start "" "mpv\mpv.exe" ^
  --no-taskbar-progress --input-gamepad=yes --no-osc --loop-file=inf --alpha=yes --no-audio --no-input-cursor --no-input-default-bindings --idle --player-operation-mode=pseudo-gui ^
  --fs --fs-screen=2 ^
  --keep-open=yes ^
  --idle=yes ^
  --input-ipc-server=%DMD_PIPE% ^
  --image-display-duration=inf ^
  --force-window=yes ^
  --title="RetroBat DMD"

start "" /min py -3 fanart_server.py