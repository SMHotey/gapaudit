@echo off
rem Запуск GapAudit одной командой: run.bat [аргументы desktop.py]
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist ".venv\bin\python" (
    set "PY=.venv\bin\python"
) else (
    echo [run.bat] окружение .venv не найдено, ставлю его через uv...
    uv venv --python 3.11 || goto :fail
    uv pip install -e ".[dev]" || goto :fail
    set "PY=.venv\Scripts\python.exe"
)

"%PY%" desktop.py %*
exit /b %errorlevel%

:fail
echo [run.bat] не удалось подготовить окружение. Смотри README.md, раздел "Установка на чистой машине".
exit /b 1
