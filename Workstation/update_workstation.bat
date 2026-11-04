@echo off
setlocal
cd /d "%~dp0"

git rev-parse --show-toplevel >nul 2>&1
if errorlevel 1 goto not_repo

git rev-parse --abbrev-ref --symbolic-full-name "@{u}" >nul 2>&1
if errorlevel 1 goto no_upstream

git diff --quiet --
if errorlevel 1 goto unstaged_changes

git diff --cached --quiet --
if errorlevel 1 goto staged_changes

echo Updating workstation from its configured upstream...
echo Close the workstation application before updating its files.
pause
git pull --ff-only
if errorlevel 1 goto update_failed

echo.
echo Update completed. You can reopen the workstation application.
pause
exit /b 0

:not_repo
echo This file must be placed in the root of the workstation Git repository.
pause
exit /b 1

:no_upstream
echo This repository branch has no configured upstream. No update was performed.
pause
exit /b 1

:unstaged_changes
echo Tracked files have uncommitted changes. Commit or stash them before updating.
echo This script will not overwrite or discard local changes.
pause
exit /b 1

:staged_changes
echo Staged changes are present. Commit or unstage them before updating.
echo This script will not overwrite or discard local changes.
pause
exit /b 1

:update_failed
echo Update failed. No reset, force update, or cleanup was attempted.
pause
exit /b 1
