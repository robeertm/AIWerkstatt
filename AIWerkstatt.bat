@echo off
REM -- AIWerkstatt -- one-click start for Windows --------------------------------
REM Double-click this file. First run builds the images (a few minutes); every run
REM after that is quick. Needs Docker Desktop installed once.
REM   Stop later with:  docker compose down   (in this folder)
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo == AIWerkstatt ==

where docker >nul 2>&1
if errorlevel 1 (
  echo Docker isn't installed yet.
  echo Get Docker Desktop ^(free^):  https://www.docker.com/products/docker-desktop
  echo Install it, then double-click AIWerkstatt.bat again.
  pause
  exit /b 1
)

docker info >nul 2>&1
if not errorlevel 1 goto run
echo Starting Docker Desktop... ^(first launch can take a minute^)
start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
set n=0
:waitdocker
timeout /t 2 >nul
docker info >nul 2>&1
if not errorlevel 1 goto run
set /a n+=1
if !n! lss 90 goto waitdocker
echo Docker didn't come up. Open Docker Desktop manually, then run this again.
pause
exit /b 1

:run
echo Building ^& starting AIWerkstatt ^(first run takes a few minutes^)...
docker compose up -d --build
if errorlevel 1 (
  echo Something went wrong starting the containers. Scroll up for the error.
  pause
  exit /b 1
)
echo Opening http://localhost:8095 ...
timeout /t 4 >nul
start "" http://localhost:8095
echo.
echo AIWerkstatt is running -^>  http://localhost:8095
echo    Stop it later with:  docker compose down   ^(in this folder^)
pause
