@echo off
cd /d "%~dp0"
python -m venv .venv 2>nul
call .venv\Scripts\activate
pip install -q -r requirements.txt
python app.py
