@echo off
REM Knowledge-graph evolution cycle wrapper for Windows Task Scheduler.
REM Runs scripts/evolution_cycle.py, appends to output/kg_evolution_log.jsonl,
REM logs to output/evolution_cycle.log (rotated at 1 MB).
REM
REM See ~/.claude/rules/task-scheduler-cmd-wrapper.md for why a .cmd wrapper
REM (not direct python invocation) is mandatory for scheduled tasks.

cd /d "G:\Other computers\My Computer\Development\knowledge-graph"
if errorlevel 1 (
    REM Repo dir unreachable — likely Google Drive offline at fire time.
    REM Without this guard the rest of the script runs in C:\Windows\System32
    REM and writes the log to a wrong location. Exit 3 matches the "env
    REM missing" exit code in evolution_cycle.py.
    echo [%date% %time%] ERROR: cannot cd to repo root, GDrive offline? Exit 3.
    exit /b 3
)
set LOGFILE=G:\Other computers\My Computer\Development\knowledge-graph\output\evolution_cycle.log

REM Log rotation (>1 MB) — pure cmd, no PowerShell subprocess (Defender PowhidSubExec heuristic)
if exist "%LOGFILE%" (
    for %%I in ("%LOGFILE%") do if %%~zI gtr 1048576 (
        if exist "%LOGFILE%.old" del /q "%LOGFILE%.old"
        move /y "%LOGFILE%" "%LOGFILE%.old" >nul 2>&1
    )
)

if not exist output mkdir output

set KG_VAULT_PATH=%USERPROFILE%\Development\Obsidian data update
set KG_DATA_DIR=%USERPROFILE%\.local\share\knowledge-graph

REM Resolve Python — prefer the py launcher (Python upgrade-safe), then probe
REM known versions, finally fall back to PATH lookup. This survives 3.14 -> 3.15
REM upgrades without the .cmd needing edits.
set PYEXE=
where py >nul 2>&1
if %ERRORLEVEL%==0 set PYEXE=py -3
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python314\python.exe
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python315\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python315\python.exe
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if not defined PYEXE set PYEXE=python

echo [%date% %time%] Starting kg evolution cycle (python: %PYEXE%) >> "%LOGFILE%"
"%PYEXE%" scripts\evolution_cycle.py --quiet >> "%LOGFILE%" 2>&1
set RC=%ERRORLEVEL%
echo [%date% %time%] Completed with exit code %RC% >> "%LOGFILE%"

REM Surface HIGH lints (exit 1) as a visible alert file in Downloads.
REM The append-only JSON log captures full detail.
if %RC%==1 (
    echo [%date% %time%] HIGH lints detected, see output\kg_evolution_latest.json >> "%LOGFILE%"
    echo [%date%] kg evolution cycle reported HIGH lints. > "%USERPROFILE%\Downloads\kg_evolution_alert.txt"
    echo See: G:\Other computers\My Computer\Development\knowledge-graph\output\kg_evolution_latest.json >> "%USERPROFILE%\Downloads\kg_evolution_alert.txt"
)
