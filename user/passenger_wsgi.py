"""Passenger / cPanel entry point for the USER portal.

cPanel -> Setup Python App
    Application root :  /home/USER/savi/user
    Application URL  :  user.savinutritions.online
    Startup file     :  passenger_wsgi.py
    Entry point      :  application

Secrets come from ``/home/USER/savi/config/.env`` (outside every web root) and
are loaded before ``app`` is imported, because the Flask app reads
``DATABASE_URL`` / ``SECRET_KEY`` at import time.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

for _extra in (str(ROOT_DIR), str(APP_DIR)):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import deploy_env  # noqa: E402  (needs ROOT_DIR on sys.path first)

deploy_env.configure(app_root=APP_DIR)

from app import app as application  # noqa: E402,F401

# Passenger looks for ``application``. ``app`` is exported too so the same file
# also works with gunicorn/uwsgi (``passenger_wsgi:app``).
app = application
