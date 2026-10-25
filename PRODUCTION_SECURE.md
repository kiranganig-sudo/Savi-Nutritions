# Savi Nutrition — Production Ready & Secure

Hosted deployment: **Namecheap shared hosting (cPanel + Passenger)** with two
subdomains sharing one TiDB Cloud database.

- Admin portal: `https://admin.savinutritions.online`
- User portal: `https://user.savinutritions.online`
- Step-by-step server setup: [`DEPLOY_NAMECHEAP.md`](DEPLOY_NAMECHEAP.md)

## Secure folders (never public)

| Path | What's inside | Why it must stay private | Protection |
|------|---------------|--------------------------|------------|
| `config/.env` | `DATABASE_URL` (TiDB password), `SECRET_KEY`, SMTP password | Secrets | `chmod 600`, outside every web root, `.gitignore: config/.env` |
| `.env` | Local/offline copy of the same secrets | Secrets | `chmod 600`, never committed |
| `admin/savi.db` | Offline SQLite database (users, stock, purchases) | All business data | `.gitignore: *.db`, never inside a public folder |
| `uploads/` | Purchase bills, driver slips, sales invoices, product photos | Supplier/driver PII, financial proofs | Outside both web roots; served only by authenticated Flask routes with `send_from_directory` |
| `backups/` | Daily Excel/PDF/JSON/ZIP backups | Full data export | `chmod 700`, `.gitignore` |
| `*.log` | Request and server logs | May leak IPs and stack traces | `chmod 640`, never served |
| `__pycache__/` | Compiled Python | Source structure | `.gitignore` |
| `SaviNutrition-Production/` | Stale duplicate tree | Confusion / old data copies | `.gitignore` — only the root tree is live |

## Public folders (safe to serve)

| Path | What's inside | Served how |
|------|---------------|------------|
| `admin/static/`, `user/static/` | `css/style.css`, `js/main.js`, `img/logo.png`, product photos | Flask `url_for('static', ...)` |
| `admin/templates/`, `user/templates/` | Jinja HTML | Rendered server-side only, never direct file access |

Both web roots are `<repo>/admin` and `<repo>/user`, so `config/`, `uploads/`,
`backups/` and the databases are all outside `public_html`.

## Production hardening already applied

1. **`SECRET_KEY`** — read from the environment; production (`FLASK_ENV=production`
   or `DATABASE_URL` set) raises at import when it is missing or shorter than 32
   characters. Generate one with
   `python -c "import secrets; print(secrets.token_hex(32))"`.
2. **Session cookies** — `HTTPONLY=True`, `SAMESITE=Lax`, distinct names
   (`savi_admin_session` / `savi_user_session`) so the two portals never
   overwrite each other. `SESSION_COOKIE_SECURE` follows `SECURE_COOKIE=1`
   (on for the hosted site; turn AutoSSL on before the first login).
3. **Security headers** — `X-Content-Type-Options: nosniff`, `X-Frame-Options:
   SAMEORIGIN`, `Referrer-Policy: same-origin`, `Permissions-Policy`, a
   `Content-Security-Policy` for HTML responses, and HSTS when the request is
   HTTPS.
4. **Database** — `DATABASE_URL` (`mysql+pymysql://...`) with `pool_pre_ping`;
   SQLite fallback only for local/offline use. Both portals must point at the
   same database.
5. **Login protection** — per-IP and per-login-id brute-force throttle
   (10 failures / 10 min, then a 10-minute lock), session id regeneration at
   login, absolute 12-hour session lifetime, and separate admin/user role gates.
6. **Boot-time schema marker** — `schema_meta` records the schema version so the
   migration chain runs once per schema change instead of on every boot. This
   keeps Passenger's start time around 10 seconds instead of 100+.
   **Bump `_SCHEMA_VERSION` in `admin/app.py` and `user/app.py` whenever a
   `_migrate_*` helper changes.**
7. **Debug off** — never run with `FLASK_DEBUG=1` on the server.

## Local / offline production run (Windows)

```powershell
python -m pip install -r requirements.txt
python run_production.py
```

Serves the admin portal at `http://localhost:5000/` and the user portal at
`http://localhost:5000/user`, using `admin/savi.db` unless `DATABASE_URL` is set.
Or double-click `Start SAVI Local.bat` (or `SaviNutrition.exe` with
`desktop_config.json` set to `"mode": "local"`).

**File permissions:** the database, `.env`/`config/.env` and `uploads/` should be
readable only by the account that runs the app. On cPanel the files are already
owned by your user and sit outside `public_html`.

## Backups

Admin → **Settings → Daily Backup** writes Excel, PDF, JSON and ZIP copies to:

- a local folder (default `<repo>/backups`, or `~/SaviNutrition/Backups` on Windows)
- an optional Google Drive synced folder

The scheduler fires at 12:00 AM and 6:00 PM. On shared hosting the web process
may be recycled, so add a cPanel cron job that calls
`admin/backup_once.py` at the same times (see `DEPLOY_NAMECHEAP.md`).
