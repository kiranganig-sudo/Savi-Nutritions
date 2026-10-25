# SAVI Nutrition — Inventory Management System

A Flask + SQLite ERP for managing products, raw materials, site production
machines, vendors, purchases, resources, production batches, reports and
backups.

## Features
- **Dashboard** — live KPIs, low-stock alerts, presence.
- **Finished Goods / Inventory** — 6 products, per-product raw-material
  Production Update sheets (Opening / Consumed / Closing / Batch / Old+New
  batches / Total Output), daily history, undo/redo, Excel export.
- **Production Formula** — per-product formula sheets across
  Raw Materials (40), Chemicals (18) and Medicines (34), with Old/New
  machine quantities.
- **Raw Materials** register, **Vendors** (GSTIN, credit tracking),
  **Purchases**, **Orders**, **Resources** (maintenance & overview),
  **Tracking**, **Settings**, **Reports** (Excel/PDF) and **Backup** (ZIP).

## Requirements
- Python 3.10+ (developed on 3.14)
- Dependencies listed in `requirements.txt`

## Quick start
```bat
pip install -r requirements.txt
start.bat
```
Then open http://localhost:5000

> On a local development run, a fresh `savi.db` may be seeded with demo data.
> Production startup uses `SAVI_NO_SEED=1` and requires an explicit migration
> or bootstrap process for a new database.

## Production run

Install the root dependencies and start both local portals with the root
launcher:

```bat
python -m pip install -r ..\requirements.txt
python ..\run_production.py
```

The hosted site runs each portal as its own cPanel/Passenger application
(`passenger_wsgi.py` here is the admin entry point) against the shared TiDB
Cloud database. See the root `DEPLOY_NAMECHEAP.md` for the server steps. The
local SQLite database is only for the packed two-port offline installation.

## Backup / Export
- **Admin panel → Backup** downloads a full ZIP (all data as JSON).
- **Admin panel → Reports** downloads Excel or PDF reports.
- The app also exposes `/api/backup` and `/api/export/excel`.

## Project layout
```
app.py              Flask app, all routes & APIs
passenger_wsgi.py   cPanel/Passenger entry point (reads ../config/.env)
backup_once.py      One-shot backup for a cPanel cron job
models.py           SQLAlchemy models
seed.py             Demo seed data (only seeds an empty DB)
export_utils.py     Excel / PDF / ZIP export builders
backup_service.py   Daily Excel/PDF/JSON/ZIP + Google Drive copy
run_server.py       Windows two-port dev runner (admin on :5000)
start.bat           Windows quick start
templates/          Jinja2 HTML templates
static/             CSS, JS, images
```