@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0" || goto error_directory
if "%~1"=="" goto check_tools
if "%~1"=="--setup-only" goto check_tools
echo Unknown option. Use --setup-only to install and build without starting.
exit /b 1

:check_tools
where py >nul 2>&1
if errorlevel 1 goto error_python
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto error_python
where node >nul 2>&1
if errorlevel 1 goto error_node
node -e "const [a,b]=process.versions.node.split('.').map(Number); process.exit(a>22 || (a===22 && b>=12) ? 0 : 1)"
if errorlevel 1 goto error_node
where npx.cmd >nul 2>&1
if errorlevel 1 goto error_npx
echo BookManager will download project dependencies; the first run may take several minutes.
if exist "%~dp0.venv" goto check_venv
echo [1/4] Creating the project Python virtual environment...
py -3 -m venv "%~dp0.venv"
if errorlevel 1 goto error_venv
:check_venv
if not exist "%~dp0.venv\Scripts\python.exe" goto error_venv
set "BOOKMANAGER_PYTHON=%~dp0.venv\Scripts\python.exe"
set "BOOKMANAGER_PROJECT_DIR=%~dp0"
echo [2/4] Installing Python dependencies...
"%BOOKMANAGER_PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto error_pip
cd /d "%~dp0electron" || goto error_directory
echo [3/4] Installing frontend dependencies...
call npx.cmd --yes pnpm@9.15.9 install --frozen-lockfile
if errorlevel 1 goto error_frontend
echo [4/4] Building frontend...
call npx.cmd --yes pnpm@9.15.9 run build
if errorlevel 1 goto error_build
if "%~1"=="--setup-only" goto success
if not exist "%~dp0electron\node_modules\.bin\electron.cmd" goto error_electron
echo Starting BookManager. Keep this window open until you close the application.
call "%~dp0electron\node_modules\.bin\electron.cmd" .
if errorlevel 1 goto error_electron
:success
echo BookManager finished successfully.
exit /b 0

:error_directory
set "ERROR_MESSAGE=Cannot access the project directory."
goto failed
:error_python
set "ERROR_MESSAGE=Python 3.10 or newer and the Windows Python launcher py are required. Install them from python.org."
goto failed
:error_node
set "ERROR_MESSAGE=Node.js 22.12 or newer is required. Install it from nodejs.org."
goto failed
:error_npx
set "ERROR_MESSAGE=npx was not found. Install Node.js with npm."
goto failed
:error_venv
set "ERROR_MESSAGE=The project virtual environment is missing or invalid. Check Python and folder permissions. It was not overwritten."
goto failed
:error_pip
set "ERROR_MESSAGE=Python dependency installation failed. Check network access, disk space and permissions."
goto failed
:error_frontend
set "ERROR_MESSAGE=Frontend dependency installation failed. Check network access, disk space and permissions."
goto failed
:error_build
set "ERROR_MESSAGE=Frontend build failed. Check the output above."
goto failed
:error_electron
set "ERROR_MESSAGE=Electron is missing or failed to start. Check the output above."
:failed
echo.
echo Initialization failed: %ERROR_MESSAGE%
if not "%~1"=="--setup-only" pause
exit /b 1
