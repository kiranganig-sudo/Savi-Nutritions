# Deploying SAVI Nutrition on Namecheap (cPanel + Passenger)

Target:

| Portal | URL | Application root | Startup file |
|--------|-----|------------------|--------------|
| Admin | `https://admin.savinutritions.online` | `~/savi/admin` | `passenger_wsgi.py` |
| User | `https://user.savinutritions.online` | `~/savi/user` | `passenger_wsgi.py` |

Storage is **local disk on the hosting account** — no external database:

| What | Where |
|------|-------|
| Database | `~/savi/admin/savi.db` (SQLite, one file, shared by both portals) |
| Uploads (photos, slips, invoices) | `~/savi/uploads/` |
| Backups | `~/savi/backups/` |

There is nothing to connect to, nothing to pay for, and no outbound port to
open. The `savi.db` file that ships in the deployment zip already contains the
current data (users, products, materials, stock, production history), so the
hosted site starts with exactly the data you have locally.

> Two things to remember: the whole website state lives in that one `.db`
> file, so **download it regularly** (Admin → Settings → Daily Backup does it
> automatically), and never let a deploy overwrite it with an older copy.

---

## 0. Before you upload — check the package is intact

The zip is `SAVI-Namecheap-Deploy.zip` (about 735 KB). After extracting it on
the server you must see:

```
admin/   user/   config/   uploads/   deploy_env.py   requirements.txt
```

and inside `admin/` both `passenger_wsgi.py` and **`savi.db`**. If `savi.db` is
missing, the site would start empty — re-upload the zip instead of copying
folders one by one.

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
  admin/                 <- admin app root  (passenger_wsgi.py + savi.db inside)
  user/                  <- user app root   (passenger_wsgi.py inside)
  uploads/               <- shared by both portals (photos, slips, invoices)
  backups/               <- created automatically by the daily backup
```

Upload options:

- **cPanel → File Manager**: upload `SAVI-Namecheap-Deploy.zip` to
  `/home/USER/`, then **Extract**. Make sure the extracted folder is
  `/home/USER/savi` (not `savi apk` and not inside `public_html`).
- **FTP/SFTP**: upload everything into `/home/USER/savi/`.

Do **not** upload: `.git/`, `SaviNutrition-Production/`, `__pycache__/`,
`AGENTS.md`, `Vercel_Env_Values.txt`, `TiDB_backup*.json`. (The zip already
excludes all of them.)

---

## 2. Check `config/.env`

The zip already contains a working `.env` (the real `SECRET_KEY`, no database
URL). You normally do **not** need to create it. Verify it looks like this:

```
SECRET_KEY=<64 random hex characters>
FLASK_ENV=production
FLASK_DEBUG=0
SAVI_NO_SEED=1
SECURE_COOKIE=1
```

`DATABASE_URL` must stay **absent** — that is what makes both apps use the
local `admin/savi.db`. Only set it if you ever move back to MySQL.

Then set permissions: right-click `config/.env` → **Change Permissions** →
`600`. Do the same for `admin/savi.db` (should already be `644`).

Generate a new `SECRET_KEY` locally with:

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

If pip complains about a compiler, install the binary wheels instead:

```bash
pip install --only-binary=:all: -r /home/USER/savi/requirements.txt
```

(`boto3` is **not** installed: it is only imported when S3 storage is
configured, which this deployment never does. `PyMySQL`/`cryptography` are only
used if you set `DATABASE_URL` back to MySQL — they are harmless to install.)

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

### 5b. Hardening file (already included)

Both `savi/admin/.htaccess` and `savi/user/.htaccess` ship in the zip. They stop
Apache from ever handing out `savi.db`, `.env`, `*.py` or logs as static files,
and turn off directory listings.

cPanel normally merges them with the `# CLOUDLINUX PASSENGER CONFIGURATION`
block it writes itself. If a subdomain returns **HTTP 500** right after the
upload, your plan disallows `Options` overrides: delete the `Options -Indexes`
line from that `.htaccess` (keep the `FilesMatch` block) and reload.

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
`schema_meta` table records the version, so later boots take 1–2 seconds
instead of a minute. (Verified locally against this exact package: admin 1.3 s,
user 1.1 s.)

**After any template or `app.py` edit on the server, restart the application.**
Flask runs non-debug, so compiled templates and imported modules are cached.

---

## 8. Verify

Open each URL and check:

- [ ] `https://admin.savinutritions.online/login` loads with the padlock
- [ ] Admin login works (6-digit ID + password)
- [ ] `https://user.savinutritions.online/login` loads and a user account logs in
- [ ] Existing data is visible (dashboard counts, resources, products — the
      SQLite file was shipped with your current data)
- [ ] `https://admin.savinutritions.online/api/health` returns 200 with
      `"storage": "local"` and `"database": "sqlite"`
- [ ] Save something small (e.g. edit a stock value), reload → the change is
      still there (proves `savi.db` is writable on the host)
- [ ] Upload a product photo in the admin dashboard → it appears on **both**
      portals (proves the shared `uploads/` folder works)
- [ ] Open a purchase invoice "View" link → the file is served
- [ ] Admin **Settings → Daily Backup → Backup Now** writes files, and the
      status line reports the folder
- [ ] The admin Production Update shows the shift the user picked at login

Health endpoint check from a terminal:

```bash
curl -s https://admin.savinutritions.online/api/health
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
`/home/USER/savi/backups/<date>/` (Excel, PDF, JSON, ZIP — the ZIP contains a
copy of `savi.db`).

To also copy them off the server, set `backup_drive_dir` in Admin → Settings to
a folder you sync (or leave blank for local-only).

---

## 9b. Downloading the database on demand

The whole website state is the single file `~/savi/admin/savi.db`. To take a
snapshot, either press **Backup Now** in Admin → Settings (the ZIP in
`backups/<date>/` contains it) or download the file directly in File Manager.
Restoring is the same in reverse: upload the `.db` over the existing one while
both applications are stopped, then restart them.

---

## 10. Updating the site later

1. Upload the changed files over the existing ones.
2. **Never overwrite** `config/.env`, `admin/savi.db` or `uploads/`.
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
| First page load takes a minute, then fast | Normal once: the migration chain runs on the first boot and stamps `schema_meta`. |
| Site starts with no data / empty dashboard | `admin/savi.db` did not survive the upload (zip not extracted, or folders copied by hand). Re-upload the zip and extract it into `~/savi`. |
| `attempt to write a readonly database` | `savi.db` or its folder is not writable. Set the file to `644` and `~/savi/admin` to `755`. |
| `RuntimeError: SECRET_KEY must be...` | `config/.env` is missing, not readable, or the key is shorter than 32 chars. |
| Login returns to the login page | `SECURE_COOKIE=1` without HTTPS. Run AutoSSL, or set it to `0` temporarily. |
| Login works, then instantly logged out | Same cookie issue, or the browser blocks cookies for the domain. |
| Uploads 503 "Durable upload storage is not configured" | `UPLOAD_DIR` points somewhere unwritable. Check permissions on `savi/uploads` (755/775). |
| Uploaded photo not visible on the other portal | Both apps must share `UPLOAD_DIR`; the loader sets it to `<repo>/uploads` automatically. |
| "Data Updated" dialog keeps appearing | The two apps must point at the **same** database file. Confirm `admin/savi.db` exists and `DATABASE_URL` is unset. |
| HTTP 500 immediately after upload | The `.htaccess` `Options -Indexes` line is not allowed on this plan — delete that line (see step 5b). |
| Changes to templates do nothing | Restart the application (step 7). |

---

## What was removed from the old Vercel setup

- `vercel.json`, `vercel_build.py`, `.vercelignore`, `.python-version`
- the root `app.py` Vercel/combined WSGI entry point
- the generated `public/` static tree
- `[tool.vercel]` from `pyproject.toml`
- all Vercel documentation
- the TiDB Cloud dependency: the database is now the local SQLite file
  `admin/savi.db`, so no external database, no port-4000 outbound rule and no
  monthly database bill

Nothing in the templates, models or business logic changed.
