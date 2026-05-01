@echo off
REM Knowledge-graph evolution cycle wrapper for Windows Task Scheduler.
REM Runs scripts/evolution_cycle.py, appends to output/kg_evolution_log.jsonl,
REM logs to output/evolution_cycle.log (rotated at 1 MB).
REM
REM See ~/.claude/rules/task-scheduler-cmd-wrapper.md for why a .cmd wrapper
REM (not direct python invocation) is mandatory for scheduled tasks.

cd /d "G:\Other computers\My Computer\Development\knowledge-graph"
set LOGFILE=G:\Other computers\My Computer\Development\knowledge-graph\output\evolution_cycle.log

REM Log rotation (>1 MB)
powershell -Command "if ((Test-Path '%LOGFILE%') -and (Get-Item '%LOGFILE%').Length -gt 1MB) { Move-Item '%LOGFILE%' '%LOGFILE%.old' -Force }" >nul 2>&1

if not exist output mkdir output

set KG_VAULT_PATH=C:\Users\Dmitry Liakhovets\Development\Obsidian data update
set KG_DATA_DIR=C:\Users\Dmitry Liakhovets\.local\share\knowledge-graph

echo [%date% %time%] Starting kg evolution cycle >> "%LOGFILE%"
"C:\Python314\python.exe" scripts\evolution_cycle.py --quiet >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] Completed with exit code %RC% >> "%LOGFILE%"

REM Surface HIGH lints (exit 1) as a visible alert file in Downloads.
REM The append-only JSON log captures full detail.
if %RC%==1 (
    echo [%date% %time%] HIGH lints detected, see output\kg_evolution_latest.json >> "%LOGFILE%"
    echo [%date%] kg evolution cycle reported HIGH lints. > "%USERPROFILE%\Downloads\kg_evolution_alert.txt"
    echo See: G:\Other computers\My Computer\Development\knowledge-graph\output\kg_evolution_latest.json >> "%USERPROFILE%\Downloads\kg_evolution_alert.txt"
)
