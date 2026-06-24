#!/usr/bin/env bash
cd "$(dirname "$0")"
python3 -m venv .venv 2>/dev/null
source .venv/bin/activate
pip install -q -r requirements.txt
python app.py
