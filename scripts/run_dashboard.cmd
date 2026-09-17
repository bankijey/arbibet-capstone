@echo off
rem Local preview of the Streamlit dashboard, reading .streamlit/secrets.toml.
cd /d "%~dp0.."
"%~dp0..\..\arbibet-capstone\.venv\Scripts\streamlit.exe" run dashboard/app.py --server.headless true --server.port 8504 --browser.gatherUsageStats false
