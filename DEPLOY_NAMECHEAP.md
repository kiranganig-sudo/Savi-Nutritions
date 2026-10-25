# Deploying SAVI Nutrition on Namecheap (cPanel + Passenger)

Target:

| Portal | URL | Application root | Startup file |
|--------|-----|------------------|--------------|
| Admin | `https://admin.savinutritions.online` | `~/savi/admin` | `passenger_wsgi.py` |
| User | `https://user.savinutritions.online` | `~/savi/user` | `passenger_wsgi.py` |

The database stays exactly as it is today: **TiDB Cloud**, one shared database
for both portals. No data is copied or restructured by this deployment.

---

## 0. Before you upload — check the server can reach TiDB

TiDB Cloud listens on **port 4000**, not 3306. Many shared hosts block
outbound ports, so test this first.

cPanel → **Terminal** (or SSH):

```bash
python3 - <<'PY'
import socket
host = "gateway01.us-east-1.prod.aws.tidbcloud.com"
try:
    socket.create_connection((host, 4000), timeout=15).close()
    print("OK: port 4000 reachable")
except Exception as exc:
    print("BLOCKED:", exc)
PY
```

If it prints `BLOCKED`, open a Namecheap support ticket and ask them to allow
outbound TCP 4000 to that hostname. Nothing else will work until it does.

---

## 1. Upload the files

The repository must be uploaded **outside `public_html`** so the database,
`config/.env` and `uploads/` are not web-accessible.

Final layout on the server:

```
/home/USER/savi/
  config/.env            <- secrets (chmod 600)
  deploy_env.py
  requirements.txt
  savi_file_storage.py
  export_utils.py
  admin/                 <- admin app root  (passenger_wsgi.py inside)
  user/                  <- user app root   (passenger_wsgi.py inside)
  uploads/               <- created automatically, shared by both portals
```

Upload options:

- **cPanel → File Manager**: zip the project locally, upload the zip to
  `/home/USER/`, then **Extract**. Make sure the extracted folder is
  `/home/USER/savi` (not `savi apk` and not inside `public_html`).
- **FTP/SFTP**: upload everything into `/home/USER/savi/`.

Do **not** upload: `.git/`, `SaviNutrition-Production/`, `__pycache__/`,
`__pycache__` files, `*.db`, `backups/`, `TiDB_backup*.json`,
`Vercel_Env_Values.txt`, `AGENTS.md`.

---

## 2. Create `config/.env`

In cPanel → **File Manager**, copy `config/.env.example` to `config/.env`, then
edit it:

```
DATABASE_URL=mysql+pymysql://USER:PASSWORD@gateway01.us-east-1.prod.aws.tidbcloud.com:4000/test?ssl_verify_cert=true&ssl_verify_identity=true
SECRET_KEY=<64 random hex characters>
FLASK_ENV=production
FLASK_DEBUG=0
SAVI_NO_SEED=1
SECURE_COOKIE=1
```

Then set permissions: right-click `config/.env` → **Change Permissions** →
`600`.

Generate `SECRET_KEY` locally with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

> Both portals read this one file. `deploy_env.py` loads it before the Flask
> app is imported, and a real environment variable always wins over the file.

---

## 3. Create the two Python applications

cPanel → **Setup Python App** → **Create Application** (do this **twice**).

**Admin**

| Field | Value |
|-------|-------|
| Python version | 3.10 or newer (3.11/3.12 preferred) |
| Application root | `savi/admin` |
| Application URL | `admin.savinutritions.online` |
| Application startup file | `passenger_wsgi.py` |
| Passenger log file | leave default |

**User**

| Field | Value |
|-------|-------|
| Application root | `savi/user` |
| Application URL | `user.savinutritions.online` |
| Application startup file | `passenger_wsgi.py` |

cPanel may generate its own `passenger_wsgi.py`. **Keep ours** — it must contain
the `deploy_env.configure(...)` call and `from app import app as application`.
If cPanel overwrote it, restore the file from the repository.

---

## 4. Install the dependencies in each virtualenv

The **Setup Python App** page shows the activation command for each app, e.g.

```bash
source /home/USER/virtualenv/savi/admin/3.11/bin/activate
```

Run this **for both** apps:

```bash
pip install --upgrade pip
pip install -r /home/USER/savi/requirements.txt
```

If `cryptography` fails to build on the shared host, try:

```bash
pip install --only-binary=:all: cryptography
```

(`boto3` is **not** installed by default: it is only imported when S3 storage is
configured. If you ever set `S3_BUCKET`, run `pip install boto3` in that
virtualenv.)

---

## 5. Point the subdomains at the application

cPanel → **Domains** (or **Subdomains**):

- `admin.savinutritions.online` → document root `/home/USER/savi/admin`
- `user.savinutritions.online` → document root `/home/USER/savi/user`

The document root and the **Application root** from step 3 must be the **same
folder** — that is where cPanel writes its Passenger `.htaccess`, and it is how
the request reaches your `passenger_wsgi.py`. There is no separate `public`
folder to point at; Flask serves the static files itself.

Nameservers are already delegated to Namecheap, so no DNS change is needed.

### 5b. Optional hardening (recommended)

If the host ever serves a file directly (instead of passing it to Passenger),
these rules stop source code, databases and logs from being downloaded. In
cPanel → **File Manager**, edit the `.htaccess` that cPanel created inside
`savi/admin` (and again in `savi/user`) and append this **outside** the
`# CLOUDLINUX PASSENGER CONFIGURATION` block:

```apache
Options -Indexes
<FilesMatch "\.(py|pyc|pyo|db|sqlite3?|log|bak|env|ini|toml|json)$">
  Require all denied
</FilesMatch>
```

If the site returns HTTP 500 after saving, the plan disallows `AllowOverride`
for `Options` — remove the `Options -Indexes` line and keep the `FilesMatch`
block.

---

## 6. Turn on HTTPS before the first login

cPanel → **SSL/TLS Status** → **Run AutoSSL** for both subdomains.

`SECURE_COOKIE=1` means the login cookie is only sent over HTTPS. If you visit
over plain `http://`, login appears to "bounce" back to the login page.

While testing over http you may set `SECURE_COOKIE=0`, then change it back.

---

## 7. Restart both applications

cPanel → **Setup Python App** → **Restart** for each app.

The first boot after a schema change runs the migration chain once. The
`schema_meta` table records the version, so later boots take ~10 seconds
instead of 100+.

**After any template or `app.py` edit on the server, restart the application.**
Flask runs non-debug, so compiled templates and imported modules are cached.

---

## 8. Verify

Open each URL and check:

- [ ] `https://admin.savinutritions.online/login` loads with the padlock
- [ ] Admin login works (6-digit ID + password)
- [ ] `https://user.savinutritions.online/login` loads and a user account logs in
- [ ] Existing data is visible (dashboard counts, resources, products)
- [ ] `https://admin.savinutritions.online/api/health` returns 200 with
      `"storage": "local"`
- [ ] Upload a product photo in the admin dashboard → it appears on **both**
      portals (proves the shared `uploads/` folder works)
- [ ] Open a purchase invoice "View" link → the file is served
- [ ] Admin **Settings → Daily Backup → Backup Now** writes files, and the
      status line reports the folder
- [ ] The admin Production Update shows the shift the user picked at login

Health endpoint check from a terminal:

```bash
curl -sI https://admin.savinutritions.online/api/health | head -1
```

---

## 9. Daily backups via cron (recommended)

The in-app scheduler fires at 12:00 AM and 6:00 PM, but Passenger may recycle
an idle process, so add a cron job as a safety net.

cPanel → **Cron Jobs** → add two entries:

```
0 0 * * *  /home/USER/virtualenv/savi/admin/3.11/bin/python /home/USER/savi/admin/backup_once.py 00:00
0 18 * * * /home/USER/virtualenv/savi/admin/3.11/bin/python /home/USER/savi/admin/backup_once.py 18:00
```

Replace `3.11` with the Python version you chose. Backup files land in
`/home/USER/savi/backups/<date>/` (Excel, PDF, JSON, ZIP).

To also copy them off the server, set `backup_drive_dir` in Admin → Settings to
a folder you sync (or leave blank for local-only).

---

## 10. Updating the site later

1. Upload the changed files over the existing ones.
2. **Never overwrite** `config/.env` or `uploads/`.
3. If any `_migrate_*` helper changed, bump `_SCHEMA_VERSION` in
   `admin/app.py` **and** `user/app.py`.
4. Restart both applications.

From a local clone:

```bash
git add -A
git commit -m "describe the change"
git push origin main
```

then re-upload (or `git pull` on the server if you cloned it there).

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| 503 / "Passenger could not start" | Check the app's error log in cPanel. Usually a missing dependency (`pip install -r requirements.txt`) or a syntax error. |
| First page load takes >90s then 504 | Migrations are running against TiDB. Confirm `schema_meta` exists and its `version` equals `_SCHEMA_VERSION`. |
| `Can't connect to MySQL server` | Outbound port 4000 blocked (step 0), or a wrong `DATABASE_URL`. |
| `RuntimeError: SECRET_KEY must be...` | `config/.env` is missing, not readable, or the key is shorter than 32 chars. |
| Login returns to the login page | `SECURE_COOKIE=1` without HTTPS. Run AutoSSL, or set it to `0` temporarily. |
| Login works, then instantly logged out | Same cookie issue, or the browser blocks cookies for the domain. |
| Uploads 503 "Durable upload storage is not configured" | `UPLOAD_DIR` points somewhere unwritable. Check permissions on `savi/uploads` (755/775). |
| Uploaded photo not visible on the other portal | Both apps must share `UPLOAD_DIR`; the loader sets it to `<repo>/uploads` automatically. |
| "Data Updated" dialog keeps appearing | The two apps must point at the **same** database. Compare `DATABASE_URL`. |
| Changes to templates do nothing | Restart the application (step 7). |
| 500 with `Duplicate column name` at boot | Harmless race when admin and user boot together; the app logs it and continues. |

---

## What was removed from the old Vercel setup

- `vercel.json`, `vercel_build.py`, `.vercelignore`, `.python-version`
- the root `app.py` Vercel/combined WSGI entry point
- the generated `public/` static tree
- `[tool.vercel]` from `pyproject.toml`
- all Vercel documentation

Nothing in the database, templates, models or business logic changed.
