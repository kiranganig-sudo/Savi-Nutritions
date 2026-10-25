# SAVI Nutrition

Flask ERP with a separate **admin** and **user** portal that share one database.

Hosted on **Namecheap (cPanel + Passenger)** with a **TiDB Cloud** database:

| Portal | URL | App root | Startup file |
|--------|-----|----------|--------------|
| Admin | https://admin.savinutritions.online | `~/savi/admin` | `passenger_wsgi.py` |
| User | https://user.savinutritions.online | `~/savi/user` | `passenger_wsgi.py` |

Server setup instructions: **[`DEPLOY_NAMECHEAP.md`](DEPLOY_NAMECHEAP.md)**.

## Structure

```
config/                 Shared secrets + uploads live OUTSIDE the web roots
  .env                  DATABASE_URL, SECRET_KEY, SMTP (never committed)
  .env.example          Template that IS committed
deploy_env.py           Loads config/.env for both Passenger entry points
admin/                  Admin portal
  app.py                Flask app, all routes & APIs
  passenger_wsgi.py     cPanel entry point -> application
  backup_once.py        One-shot backup for a cPanel cron job
  models.py seed.py export_utils.py backup_service.py
  templates/ static/
user/                   User portal (same layout, user-facing pages only)
uploads/                Shared upload storage (created at runtime, not in git)
backups/                Daily backup output (not in git)
run_production.py       One-process local/offline server (both portals, port 5000)
desktop_app.py          Windows launcher (hosted mode by default)
desktop_config.json     Launcher mode + URLs
SaviNutrition.exe       Double-click launcher (runs desktop_app.py)
```

`admin/` and `user/` are two independent Flask applications. They are **not**
mounted under one another: on the server each subdomain serves its own portal
at `/`. For local use `run_production.py` combines them (admin at `/`, user at
`/user`).

## Local / offline run

```bat
python -m pip install -r requirements.txt
python run_production.py
```

- Admin: `http://localhost:5000`
- User: `http://localhost:5000/user`
- Database: `admin/savi.db` unless `DATABASE_URL` is set in `.env`
- Uploads: `uploads/`

Or double-click `Start SAVI Local.bat`.

## Desktop app

`SaviNutrition.exe` (or `desktop_app.py`) opens the **hosted** website in a
Chrome/Edge app window - nothing runs on the PC, so every computer sees the
same data. `desktop_config.json`:

```json
{
  "mode": "hosted",
  "portal": "admin",
  "admin_url": "https://admin.savinutritions.online",
  "user_url": "https://user.savinutritions.online"
}
```

- `"mode": "local"` (or `Start SAVI Local.bat`) starts the offline server instead.
- `Start SAVI Hosted.bat` always opens the hosted site.
- `Stop SAVI.bat` stops a local server that is still running.
- `desktop-apps\` holds the separate Admin/User app-window shortcuts.

## Configuration

Production settings come from `config/.env` (see `config/.env.example`):

| Key | Meaning |
|-----|---------|
| `DATABASE_URL` | TiDB Cloud / MySQL connection string (**required**) |
| `SECRET_KEY` | Session signing key, 32+ random characters (**required**) |
| `SECURE_COOKIE` | `1` = HTTPS-only login cookies (on for the hosted site) |
| `SAVI_NO_SEED` | `1` = never write demo data |
| `UPLOAD_DIR` | Shared upload folder (defaults to `<repo>/uploads`) |
| `SMTP_*`, `MAIL_FROM`, `ADMIN_RESET_EMAIL` | Optional admin Forgot-Password email OTP |
| `SAVI_BACKUP_DIR` | Override the daily backup folder |

A real environment variable always wins over the file, so cPanel's
**Setup Python App → Environment variables** panel works too.

## Backups

Admin → **Settings → Daily Backup** writes Excel, PDF, JSON and ZIP files to a
local folder and (optionally) a Google Drive synced folder at 12:00 AM and
6:00 PM. On shared hosting add the `admin/backup_once.py` cron job described in
the deployment guide, because the web process may be recycled when idle.

## Security notes

See [`PRODUCTION_SECURE.md`](PRODUCTION_SECURE.md). Short version:

- `config/.env` and `.env` are git-ignored and live outside both web roots.
- Login IDs are exactly 6 digits; sessions last 12 hours; repeated failures are
  throttled and locked.
- Uploaded files are served only through authenticated Flask routes.
- Never enable `FLASK_DEBUG=1` on the server.

## Requirements

- Python 3.10+ (the server venv created by cPanel is fine)
- Flask, Flask-SQLAlchemy, openpyxl, reportlab, PyMySQL, waitress
  (`cryptography` and `boto3` are only needed for some MySQL auth modes and
  S3 uploads)
