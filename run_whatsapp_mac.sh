#!/bin/bash
# WhatsApp backend (OpenWA webhook). The Streamlit web app runs separately: ./run_mac.sh
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
python whatsapp_server.py
