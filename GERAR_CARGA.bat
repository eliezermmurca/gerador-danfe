@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
set /p CARGA=Numero da carga (separe varias por espaco): 
if "%CARGA%"=="" goto fim
"%PY%" executar.py --carga %CARGA%
if exist "saida\cargas" start "" "saida\cargas"
:fim
echo.
pause
