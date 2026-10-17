@echo off
rem ===========================================================================
rem  PaperMind one-click launcher (Windows / cmd)
rem
rem  NOTE: this file is intentionally ASCII-only.
rem  cmd.exe parses .bat files with the machine's OEM code page (936 on zh-CN),
rem  so non-ASCII text saved as UTF-8 gets mis-decoded -- which can even turn
rem  bytes into stray "&" / "|" and break the script. Chinese UI text lives in
rem  the docs; everything here stays printable ASCII on purpose.
rem
rem  What it does:
rem    1) locate python from your conda env (default: scenemind)
rem    2) check key deps (offers: pip install -r requirements.txt)
rem    3) init the database (idempotent; --skip-init to skip)
rem    4) start the FastAPI backend and open the browser
rem
rem  Usage:
rem    scripts\start.bat
rem    scripts\start.bat --port 8800
rem    scripts\start.bat --env py312 --skip-init --no-browser
rem    scripts\start.bat --reload            (dev hot reload)
rem
rem  Optional environment variables:
rem    PR_PYTHON   full path to python.exe (skips conda detection)
rem    PR_ENV      conda env name          (default: scenemind)
rem    PR_HOST     bind address            (default: 127.0.0.1)
rem    PR_PORT     port                    (default: 8710)
rem ===========================================================================

chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."

rem Force UTF-8 inside the Python process so backend logs render correctly
rem regardless of the console code page.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "ENV_NAME=%PR_ENV%"
if "%ENV_NAME%"=="" set "ENV_NAME=scenemind"
set "HOST_ADDR=%PR_HOST%"
if "%HOST_ADDR%"=="" set "HOST_ADDR=127.0.0.1"
set "PORT=%PR_PORT%"
if "%PORT%"=="" set "PORT=8710"
set "SKIP_INIT=0"
set "NO_BROWSER=0"
set "RELOAD="
set "NO_AGENT=0"

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--env"        ( set "ENV_NAME=%~2"   & shift & shift & goto parse_args )
if /I "%~1"=="--port"       ( set "PORT=%~2"       & shift & shift & goto parse_args )
if /I "%~1"=="--host"       ( set "HOST_ADDR=%~2"  & shift & shift & goto parse_args )
if /I "%~1"=="--skip-init"  ( set "SKIP_INIT=1"    & shift & goto parse_args )
if /I "%~1"=="--no-browser" ( set "NO_BROWSER=1"   & shift & goto parse_args )
if /I "%~1"=="--reload"     ( set "RELOAD=--reload" & shift & goto parse_args )
if /I "%~1"=="--no-agent"   ( set "NO_AGENT=1"    & shift & goto parse_args )
if /I "%~1"=="-h"           goto usage
if /I "%~1"=="--help"       goto usage
echo [ERROR] unknown argument: %~1
goto usage

:args_done
echo.
echo   PaperMind - paper reading + knowledge base
echo   ------------------------------------------
echo   repo : %CD%
echo   env  : %ENV_NAME%    host: %HOST_ADDR%    port: %PORT%

rem --------------------------------------------------------------- 1. python
echo.
echo [1/4] locating Python for conda env "%ENV_NAME%" ...

set "PY="
if not "%PR_PYTHON%"=="" if exist "%PR_PYTHON%" set "PY=%PR_PYTHON%"

rem Probe the usual Anaconda / Miniconda install roots.
rem ("if not defined" inside a for body is evaluated per iteration, which is
rem  the classic way to stop at the first match.)
for %%R in (
  "%USERPROFILE%\anaconda3"
  "%USERPROFILE%\miniconda3"
  "%USERPROFILE%\Anaconda3"
  "%LOCALAPPDATA%\anaconda3"
  "%LOCALAPPDATA%\miniconda3"
  "C:\ProgramData\anaconda3"
  "C:\ProgramData\miniconda3"
  "C:\anaconda3"
  "D:\anaconda3"
  "D:\Anaconda3"
  "E:\anaconda3"
) do (
  if not defined PY if exist "%%~R\envs\%ENV_NAME%\python.exe" set "PY=%%~R\envs\%ENV_NAME%\python.exe"
)

rem Last resort: let conda resolve the env (output gets buffered -> fallback only).
if not defined PY (
  where conda >nul 2>nul
  if not errorlevel 1 (
    echo       python.exe not found in the usual roots, falling back to conda run
    set "PY=conda run --no-capture-output -n %ENV_NAME% python"
  )
)

if not defined PY (
  echo [ERROR] cannot find Python for conda env "%ENV_NAME%".
  echo         create it first : conda create -n %ENV_NAME% python=3.12
  echo         or point at it  : set PR_PYTHON=C:\path\to\python.exe
  goto fail
)
echo       python: %PY%

rem --------------------------------------------------------------- 2. deps
echo.
echo [2/4] checking dependencies ...
%PY% -c "import fastapi, uvicorn, pydantic, sqlalchemy" >nul 2>nul
if not errorlevel 1 goto deps_ok

echo       missing: fastapi / uvicorn / pydantic / sqlalchemy
set "ANS="
set /p "ANS=      run pip install -r requirements.txt now? [y/N] "
if /I not "%ANS%"=="y" (
  echo [ERROR] dependencies incomplete, aborted.
  goto fail
)
%PY% -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] pip install failed.
  goto fail
)
:deps_ok
echo       dependencies ok

rem Frontend bundle (the backend serves frontend/dist directly).
rem dist is a build artifact and is NOT in the repo, so a fresh clone has none.
if exist "frontend\dist\index.html" goto dist_ok
echo.
echo [WARN] frontend\dist\index.html is missing - there is no UI to serve.
where npm >nul 2>nul
if errorlevel 1 (
  echo        npm not found. build it yourself: cd frontend ^&^& npm run build
  goto dist_ok
)
set "ANS2="
set /p "ANS2=      run npm run build now? [Y/n] "
if /I "%ANS2%"=="n" goto dist_ok
pushd frontend
call npm run build
popd
:dist_ok

rem --------------------------------------------------------------- 3. init db
if "%SKIP_INIT%"=="1" (
  echo.
  echo [3/4] database init skipped ^(--skip-init^)
  goto init_done
)
echo.
echo [3/4] initializing database (idempotent) ...
%PY% cli\pr.py init
if errorlevel 1 (
  echo [ERROR] init failed. No MySQL? set PR_DB_BACKEND=sqlite in .env and retry.
  goto fail
)
:init_done

rem --------------------------------------------------------------- 4. agent
rem The Q&A agent is a SEPARATE process on its own port (on purpose: if the
rem agent dies, parsing / translation must keep running). This launcher used to
rem start the API only, so after double-clicking it the UI worked but every Q&A
rem call failed with "agent not reachable" on :8711. It gets its own window so
rem its logs stay readable and closing it does not kill the API.
if "%NO_AGENT%"=="1" (
  echo.
  echo [4/5] agent service skipped ^(--no-agent^)
  goto agent_done
)
echo.
echo [4/5] starting agent service on port 8711 ^(separate window^) ...
start "PaperMind agent" /min cmd /c ""%PY%" cli\pr.py agent"
:agent_done

rem --------------------------------------------------------------- 5. serve
echo.
echo [5/5] serving http://%HOST_ADDR%:%PORT%    UI: /cylit/    API docs: /docs
echo       press Ctrl+C in this window to stop
echo.

if "%NO_BROWSER%"=="1" goto no_browser
start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 3; Start-Process 'http://%HOST_ADDR%:%PORT%/cylit/'"
:no_browser

%PY% cli\pr.py serve --host %HOST_ADDR% --port %PORT% %RELOAD%
goto end

:usage
echo.
echo   Usage: scripts\start.bat [options]
echo     --env ^<name^>     conda env name    (default: scenemind)
echo     --port ^<n^>       listen port       (default: 8710)
echo     --host ^<addr^>    bind address      (default: 127.0.0.1)
echo     --skip-init      skip database init
echo     --no-browser     do not open the browser
echo     --reload         dev hot reload
echo     --no-agent       do not start the agent service (:8711)
echo.
endlocal
exit /b 0

:fail
echo.
echo   Startup aborted.
endlocal
exit /b 1

:end
endlocal
