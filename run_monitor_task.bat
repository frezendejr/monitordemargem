@echo off
cd /d "C:\Users\User\Documents\margin-monitor"
set MARGIN_MONITOR_CLOUD=1
echo. >> monitor_task.log
echo ==== %date% %time% ==== >> monitor_task.log
".venv\Scripts\python.exe" monitor_cloud.py >> monitor_task.log 2>&1
