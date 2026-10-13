@echo off
rem ============================================================
rem  每日发布任务（供 Windows 任务计划程序调用）
rem  每次唤醒只判断「此刻是否该发」，状态由模块自持久化，
rem  因此重复唤醒也不会重复发布。
rem ============================================================
setlocal
set ROOT=%~dp0..
cd /d "%ROOT%"

set PYTHON_EXE=python
if exist "%ROOT%.venv\Scripts\python.exe" set PYTHON_EXE=%ROOT%.venv\Scripts\python.exe

"%PYTHON_EXE%" main.py schedule --once >> "%ROOT%logs\task.log" 2>&1
exit /b %errorlevel%
