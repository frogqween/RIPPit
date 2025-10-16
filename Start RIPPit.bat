@echo off
setlocal ENABLEDELAYEDEXPANSION

REM One-click starter for RIPPit on Windows
REM - Creates Python venv
REM - Installs requirements
REM - Ensures FFmpeg (portable if needed)
REM - Starts server and opens browser

set "PROJ=%~dp0"
pushd "%PROJ%"

REM Logging setup
set "LOGDIR=%PROJ%logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\startup.log"
>"%LOG%" echo ==== RIPPit startup %date% %time% ====

set "VENV=%PROJ%.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "BIN=%PROJ%bin"

REM Choose Python (py launcher preferred)
where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py"
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    set "PY=python"
  ) else (
    echo Python not found. Attempting install via winget...>>"%LOG%"
    where winget >nul 2>nul
    if %errorlevel%==0 (
      winget install --id Python.Python.3.11 --source winget --accept-package-agreements --accept-source-agreements --silent >>"%LOG%" 2>>&1
    ) else (
      echo Winget not available. Please install Python 3.11+ from https://www.python.org/downloads/ and rerun.>>"%LOG%"
      goto fail
    )
    set "PY=py"
  )
)

REM Create venv if missing
if not exist "%VENV%" (
  %PY% -m venv "%VENV%" >>"%LOG%" 2>>&1 || goto fail
)

REM Upgrade pip and install requirements
"%VENV_PY%" -m pip install -U pip >>"%LOG%" 2>>&1 || goto fail
"%VENV_PY%" -m pip install -r "%PROJ%requirements.txt" >>"%LOG%" 2>>&1 || goto fail

REM Ensure default download folder exists (Windows)
if not exist "%USERPROFILE%\Downloads" mkdir "%USERPROFILE%\Downloads"

REM Check for ffmpeg in PATH
where ffmpeg >nul 2>nul
if %errorlevel%==0 goto haveffmpeg

echo FFmpeg not found in PATH; using portable fallback.>>"%LOG%"

REM Download portable ffmpeg as fallback (no admin required)
if not exist "%BIN%" mkdir "%BIN%"
set "FFZIP=%BIN%\ffmpeg.zip"

echo Downloading portable FFmpeg (first run only)...
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile '%FFZIP%'" >>"%LOG%" 2>>&1
if not exist "%FFZIP%" (
  echo Failed to download FFmpeg.>>"%LOG%"
  goto fail
)

echo Extracting FFmpeg...
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%FFZIP%' -DestinationPath '%BIN%' -Force" >>"%LOG%" 2>>&1
for /f "delims=" %%F in ('powershell -NoLogo -NoProfile -Command "(Get-ChildItem -Recurse -Filter ffmpeg.exe -Path '%BIN%' | Select-Object -First 1).DirectoryName"') do set "FFDIR=%%F"
if not defined FFDIR (
  echo Failed to locate ffmpeg.exe after extraction.>>"%LOG%"
  goto fail
)
set "PATH=%FFDIR%;%PATH%"

:haveffmpeg

echo Starting RIPPit server...
set "VENV_PYW=%VENV%\Scripts\pythonw.exe"
if exist "%VENV_PYW%" (
  start "" "%VENV_PYW%" "%PROJ%run_web.pyw"
) else (
  start "" "%VENV_PY%" "%PROJ%run_web.pyw"
)

REM Give the launcher a moment to open the browser itself; then exit
ping -n 3 127.0.0.1 >nul

popd
exit /b 0
