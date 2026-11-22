@echo off
rem Block WeChat auto-update by denying write access to its update cache.
setlocal
set U=%APPDATA%\Tencent\xwechat\update
if not exist "%U%" mkdir "%U%"
if not exist "%U%\update.data" type nul > "%U%\update.data"
icacls "%U%\update.data" /deny "%USERNAME%":(W) >nul 2>nul
if not exist "%U%\patch" mkdir "%U%\patch"
icacls "%U%\patch" /deny "%USERNAME%":(OI)(CI)(W) >nul 2>nul
if not exist "%U%\download" mkdir "%U%\download"
icacls "%U%\download" /deny "%USERNAME%":(OI)(CI)(W) >nul 2>nul
exit /b 0