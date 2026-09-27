@echo off
py -3.11 -m venv .venv
call .venv\Scripts\activate
python -m pip install -r requirements.txt
streamlit run app.py
