@echo off
echo Removing the daily "Research Paper Fetcher" task and the settings shortcut.
echo Your downloaded papers are NOT deleted.
schtasks /Delete /TN "Research Paper Fetcher" /F
del "%~dp0Paper Search Settings.lnk" 2>nul
echo Done.
pause
