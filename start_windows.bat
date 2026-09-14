@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import tkinterdnd2, pymupdf" >nul 2>&1
if errorlevel 1 (
    echo Installing BookManager dependencies...
    py -3 -m pip install -r "%~dp0requirements.txt"
    if errorlevel 1 (
        echo Failed to install BookManager dependencies.
        pause
        exit /b 1
    )
)
py -3 run.py
