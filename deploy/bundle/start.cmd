@echo off
rem Start aitmation (Windows). A double-click is enough: Python and all
rem packages ship in the bundle (no network needed); at the first start a
rem household with one private mandant is created and the password is shown once.
rem Data lives under %USERPROFILE%\.aitmation
cd /d "%~dp0"
aitmation-launcher.exe run -core-dir "%~dp0core" -apps-dir "%~dp0apps" -auto-init -open-browser
if errorlevel 1 pause
