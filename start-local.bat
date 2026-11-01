@echo off
chcp 65001 >nul
cd /d "%~dp0"
setlocal

rem --- 1. портативный Python внутри проекта ---
if exist "runtime\python\python.exe" (
  echo Запускаю через runtime\python ...
  "runtime\python\python.exe" server.py
  goto done
)

rem --- 2. системный Python ---
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  echo Запускаю через py -3 ...
  py -3 server.py
  goto done
)

python -c "import sys" >nul 2>nul
if not errorlevel 1 (
  echo Запускаю через python ...
  python server.py
  goto done
)

echo.
echo Python не найден.
echo Поставь Python 3 с https://python.org (при установке отметь "Add to PATH")
echo либо положи портативный Python в папку runtime\python
echo.
pause
exit /b 1

:done
echo.
echo Сервер остановлен.
pause
