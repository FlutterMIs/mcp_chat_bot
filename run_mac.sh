#!/bin/bash
set -e
python3.11 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
python -m pip install -r requirements.txt
streamlit run app.py
