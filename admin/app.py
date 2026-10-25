"""Savi Nutrition persistent SQLAlchemy backend.

Local installations use SQLite; Vercel uses the configured DATABASE_URL.
"""
import os
import sys
try:
    import pymysql; pymysql.install_as_MySQLdb()
except ImportError:
    pass
import re
import json
import time
import hmac
import hashlib
import secrets
import smtplib
import ssl
import mimetypes
from decimal import Decimal, InvalidOperation
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from io import BytesIO

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for, make_response
from werkzeug.exceptions import NotFound
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import inspect, or_

try:
    from .models import (
        db, Product, ProductMaterial, MaterialDaily, Resource, RawMaterial,
        ProductionEntry, MachineProductionEntry, Purchase, Vendor, Formula,
        FormulaSheet, ThirdPartyItem, ConsumptionLog, StockMovement, Setting,
        Message, NotificationDismiss, DriverDetails, User, PasswordReset,
        compute_product_status, compute_raw_status, compute_resource_status,
        RawMaterialSale, ProductSale, SalesRemark,
    )
    from .seed import seed_database, RESOURCE_CATEGORIES, PRODUCTS
except ImportError:
    from models import (
        db, Product, ProductMaterial, MaterialDaily, Resource, RawMaterial,
        ProductionEntry, MachineProductionEntry, Purchase, Vendor, Formula,
        FormulaSheet, ThirdPartyItem, ConsumptionLog, StockMovement, Setting,
        Message, NotificationDismiss, DriverDetails, User, PasswordReset,
        compute_product_status, compute_raw_status, compute_resource_status,
        RawMaterialSale, ProductSale, SalesRemark,
    )
    from seed import seed_database, RESOURCE_CATEGORIES, PRODUCTS

try:
    from .export_utils import (
        backup_label, backup_stamp, build_backup_zip, build_excel_bytes,
        build_pdf_bytes, THIN_BORDER, GREEN,
    )
except ImportError:
    from export_utils import (
        backup_label, backup_stamp, build_backup_zip, build_excel_bytes,
        build_pdf_bytes, THIN_BORDER, GREEN,
    )

try:
    from . import backup_service
except ImportError:
    import backup_service

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
from savi_file_storage import FileStorageError, FileStorageUnavailable, StoredFileNotFound, load_upload, save_upload, storage_status

DB_PATH = os.path.join(BASE_DIR, "savi.db")

# Hard size cap for dashboard product photos (both /upload-image endpoints).
# The browser already crops + compresses every photo to fit this limit before
# it uploads; this is the server-side backstop for scripted/old clients.
PRODUCT_IMAGE_MAX_BYTES = 2 * 1024 * 1024

# Input restriction for Resource Maintenance item names and stock numbers:
# both are capped at 40 characters (numbers for stock, letters + digits for
# the item name). Anything longer is rejected with an "excessive value"
# message instead of being silently truncated.
RESOURCE_FIELD_MAX_CHARS = 40

# ...and the largest value a stock column can actually hold (signed 64-bit,
# the type both SQLite and MySQL use for INTEGER). A longer number is refused
# with the same "excessive value" message instead of crashing the save.
RESOURCE_MAX_VALUE = 9223372036854775807

# Item names take letters AND numbers (the user asked for both) plus the few
# punctuation marks real item names already use on the live data - e.g.
# "Biotin 2%", "Clostat-365", "Copra Cake (AK)", "DCP (Dicalcium
# Phosphate)". Anything else (symbols, slashes, quotes) is refused so the
# stock tables stay clean and searchable.
RESOURCE_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9 %()+./&,-]*"
RESOURCE_NAME_RE = re.compile(RESOURCE_NAME_PATTERN)


def _stock_field_too_large(value):
    return abs(int(value)) > RESOURCE_MAX_VALUE


def _resource_name_problem(name):
    """Return an error string when a Resource name is too long or contains
    characters that are not allowed, else None."""
    if len(name) > RESOURCE_FIELD_MAX_CHARS:
        return ("Item name is excessive - only %d characters are allowed."
                % RESOURCE_FIELD_MAX_CHARS)
    if not RESOURCE_NAME_RE.fullmatch(name):
        return ("Item name allows only letters, numbers, spaces and the "
                "characters % - ( ) . / & ,")
    return None


class _BytesFileStorage:
    """Minimal FileStorage stand-in so an already-read upload can be handed
    to save_upload() a second time without touching request.files."""

    def __init__(self, filename, data):
        self.filename = filename
        self._data = data

    def read(self, *_args, **_kwargs):
        return self._data

    def seek(self, *_args, **_kwargs):
        return 0


# Bytes that identify each image format we accept. The stored extension is
# derived from the CONTENT, never from the client's filename, so a photo is
# never saved under a name that contradicts its bytes.
_IMAGE_SIGNATURES = (
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'RIFF', 'webp'),
)


def _sniff_image_ext(data):
    for signature, ext in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            if ext == 'webp':
                # RIFF....WEBP - check the real format tag at offset 8.
                return 'webp' if data[8:12] == b'WEBP' else None
            return ext
    return None


def _read_capped_upload(file):
    """Read at most PRODUCT_IMAGE_MAX_BYTES + 1 bytes so an oversized or
    malicious upload can never be pulled fully into memory before the size
    check rejects it."""
    return file.read(PRODUCT_IMAGE_MAX_BYTES + 1)


app = Flask(__name__)
# --- Database: MySQL via DATABASE_URL or fallback to SQLite ---
# MySQL example: mysql+pymysql://user:password@localhost:3306/savi_nutrition?charset=utf8mb4
# Set DATABASE_URL env var to use MySQL. Both admin and user must use SAME database.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL:
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "pool_pre_ping": True,
        "pool_recycle": 3600,
        "connect_args": {"connect_timeout": 10, "read_timeout": 30, "write_timeout": 30},
    }
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# --- Production security: SECRET_KEY must be set via env in production ---
_secret = os.environ.get("SECRET_KEY", "")
if not _secret:
    if os.environ.get("FLASK_ENV") == "production" or os.environ.get("DATABASE_URL"):
        raise RuntimeError("SECRET_KEY env var is required in production. Set a strong random value in .env")
    _secret = "savi-nutrition-secret-dev-only"
app.config["SECRET_KEY"] = _secret
app.config["SESSION_COOKIE_NAME"] = "savi_admin_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Secure cookies only when served over HTTPS (set SECURE_COOKIE=1 in production with HTTPS)
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SECURE_COOKIE", "0") == "1"
app.config["JSON_SORT_KEYS"] = False
# Change #42: Content-Security-Policy for HTML pages only (never on images,
# PDFs or other file responses). 'unsafe-inline' is required by the existing
# inline event handlers / <script> blocks; the policy still stops injected
# EXTERNAL scripts, exfiltrating forms, <base> tricks and foreign frames.
_CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdnjs.cloudflare.com; "
    "font-src 'self' data: https://fonts.gstatic.com https://cdnjs.cloudflare.com; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)

# Security headers
@app.after_request
def _set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["X-XSS-Protection"] = "1; mode=block"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=(), payment=(), usb=()"
    if (resp.mimetype or "").startswith("text/html"):
        resp.headers["Content-Security-Policy"] = _CSP_POLICY
    if app.config["SESSION_COOKIE_SECURE"]:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return resp

PURCHASE_UPLOAD_DIR = os.path.join(ROOT_DIR, 'uploads', 'purchases')
DRIVER_SLIP_DIR = os.path.join(ROOT_DIR, 'uploads', 'driver_slips')
SALES_SLIP_DIR = os.path.join(ROOT_DIR, 'uploads', 'sales_slips')
if not (os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV')):
    os.makedirs(PURCHASE_UPLOAD_DIR, exist_ok=True)
    os.makedirs(DRIVER_SLIP_DIR, exist_ok=True)
    os.makedirs(SALES_SLIP_DIR, exist_ok=True)
ALLOWED_SLIP_EXTENSIONS = {'.jpg', '.jpeg', '.png'}

db.init_app(app)


def _table_columns(table_name):
    return {column['name'] for column in inspect(db.engine).get_columns(table_name)}


# ---------------------------------------------------------------------------
# Fast-boot schema marker
# ---------------------------------------------------------------------------
# The startup migration chain below costs ~30 `_migrate_*` calls, each doing a
# table reflection. Against a remote TiDB cluster that is 100+ seconds, which
# can exceed Passenger's start timeout on shared hosting. A tiny marker table
# records the schema version the chain last brought the database to, so warm
# boots skip the chain entirely.
#
# RULE: bump _SCHEMA_VERSION whenever any `_migrate_*` helper changes, or the
# new migration would be skipped on an already-warm database.
_SCHEMA_VERSION = 63


class _SchemaCurrent(Exception):
    """Raised inside a startup block when the schema is already up to date."""


def _startup_schema_current():
    """True when the database already carries _SCHEMA_VERSION."""
    from sqlalchemy import text
    try:
        with app.app_context():
            row = db.session.execute(
                text("SELECT version FROM schema_meta WHERE id = 1")
            ).fetchone()
            db.session.rollback()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return False
    try:
        return bool(row) and int(row[0]) >= _SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def _stamp_schema_current():
    """Record _SCHEMA_VERSION after the last startup block completed."""
    from sqlalchemy import text
    try:
        db.session.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            "id INT NOT NULL PRIMARY KEY, version INT NOT NULL)"
        ))
        existing = db.session.execute(
            text("SELECT COUNT(*) FROM schema_meta WHERE id = 1")
        ).scalar()
        if existing:
            db.session.execute(
                text("UPDATE schema_meta SET version = :v WHERE id = 1"),
                {"v": _SCHEMA_VERSION},
            )
        else:
            db.session.execute(
                text("INSERT INTO schema_meta (id, version) VALUES (1, :v)"),
                {"v": _SCHEMA_VERSION},
            )
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass


def _lookup_slip_blob(folder, filename):
    """Database fallback for slip uploads stored in the shared DB.

    On Vercel there is no durable file storage (no S3 credentials), so
    `_prepare_slip` in user/app.py keeps the uploaded bytes in
    purchases.slip_data / driver_details.slip_data. Returns (bytes, mime)
    or None.
    """
    name = os.path.basename(filename)
    if folder == 'sales_slips':
        for sale_model in (RawMaterialSale, ProductSale):
            row = sale_model.query.filter(sale_model.invoice_attachment == name).first()
            if row is not None and row.invoice_data:
                mime = row.invoice_mime or mimetypes.guess_type(name)[0] or 'application/octet-stream'
                return bytes(row.invoice_data), mime
            if hasattr(sale_model, 'weighment_attachment'):
                row = sale_model.query.filter(
                    sale_model.weighment_attachment == name).first()
                if row is not None and row.weighment_data:
                    mime = row.weighment_mime or mimetypes.guess_type(name)[0] or 'application/octet-stream'
                    return bytes(row.weighment_data), mime
        return None
    model = {'driver_slips': DriverDetails, 'purchases': Purchase}.get(folder)
    if model is None:
        return None
    row = model.query.filter(model.slip_attachment == name).first()
    if row is not None and row.slip_data:
        mime = row.slip_mime or mimetypes.guess_type(name)[0] or 'application/octet-stream'
        return bytes(row.slip_data), mime
    row = model.query.filter(model.weighment_attachment == name).first()
    if row is not None and row.weighment_data:
        mime = row.weighment_mime or mimetypes.guess_type(name)[0] or 'application/octet-stream'
        return bytes(row.weighment_data), mime
    return None


def _send_stored_file(folder, filename, local_dir, blob_lookup=None):
    # Legacy rows stored the folder-prefixed key ("driver_slips/x.png");
    # the storage layout is flat, so the basename is the stored filename.
    filename = os.path.basename(str(filename or ''))
    if not filename:
        return "File not found.", 404
    try:
        data, content_type = load_upload(folder, filename)
    except StoredFileNotFound:
        try:
            return send_from_directory(local_dir, filename, max_age=3600)
        except NotFound:
            pass
        stored = blob_lookup(filename) if blob_lookup is not None else None
        if stored is None:
            stored = _lookup_slip_blob(folder, filename)
        if stored is None:
            return "File not found.", 404
        data, content_type = stored
    except (FileStorageUnavailable, FileStorageError):
        return "File storage is temporarily unavailable.", 503
    return send_file(BytesIO(data), mimetype=content_type,
                     download_name=os.path.basename(filename), max_age=3600)


def _stored_whole_bag_value(value):
    if value is None or str(value).strip() == '':
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if number < 0 or number != number.to_integral_value():
        return None
    return int(number)


def _whole_bag_count(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    return int(max(number, 0) // 1)


def _ensure_standard_products():
    """Ensure the original six finished products always exist.

    Older databases may already contain some products, so seed_database() is
    intentionally not enough: it only seeds when the Product table is empty.
    This keeps the six original products visible in both Production Formula
    and Production Update without removing any user-created products.
    """
    changed = False
    for data in PRODUCTS:
        row = Product.query.filter_by(name=data["name"]).first()
        if row:
            continue
        row = Product(name=data["name"], category=data.get("category", "Finished Goods"),
                      price=data.get("price", 0), stock=data.get("stock", 0),
                      image=data.get("image", ""),
                      status=compute_product_status(data.get("stock", 0)))
        db.session.add(row)
        changed = True
    if changed:
        db.session.commit()
    return changed


def _ensure_standard_formula_rows():
    """Populate the six original product formula pages from their material rows.

    This is only done when a standard product has no FormulaSheet rows, so
    existing formula edits are preserved.
    """
    changed = False
    for product in PRODUCTS:
        name = product["name"]
        if FormulaSheet.query.filter_by(product=name).first():
            continue
        material_rows = ProductMaterial.query.filter_by(product_name=name).order_by(ProductMaterial.id).all()
        for row in material_rows:
            category = _vendor_category_for(row.material)
            if category not in FORMULA_CATEGORIES:
                category = "Raw Materials"
            # Preserve the material even if an old database uses a legacy
            # spelling; only valid current category entries get a category.
            db.session.add(FormulaSheet(product=name, category=category, material=row.material,
                                        old_machine=row.old_batches or 0,
                                        new_machine=(row.old_batches or 0) * 2,
                                        updated=datetime.now().strftime('%d %b %Y')))
            changed = True
    if changed:
        db.session.commit()
    return changed

def _migrate_product_material_batch():
    """Add batch column to existing savi.db files and backfill values."""
    from sqlalchemy import text, or_
    cols = _table_columns("product_materials")
    if "batch" not in cols:
        db.session.execute(text("ALTER TABLE product_materials ADD COLUMN batch VARCHAR(80) DEFAULT ''"))
        db.session.commit()
    # Backfill empty batches deterministically per product.
    empties = ProductMaterial.query.filter((ProductMaterial.batch.is_(None)) | (ProductMaterial.batch == "")).order_by(
        ProductMaterial.product_name, ProductMaterial.id).all()
    counters = {}
    for row in empties:
        counters[row.product_name] = counters.get(row.product_name, 0) + 1
        prefix = "".join(w[0] for w in row.product_name.split() if w).upper()[:3] or "BAT"
        row.batch = f"{prefix}-{counters[row.product_name]:03d}"
    if empties:
        db.session.commit()


def _refresh_product_statuses():
    """Recompute stored Product.status so new stock thresholds apply to existing rows."""
    changed = False
    for p in Product.query.all():
        st = compute_product_status(p.stock)
        if p.status != st:
            p.status = st
            changed = True
    if changed:
        db.session.commit()
    return changed


_OLD_PRODUCT_STOCK = {
    "Savi Gold Plus": 150, "Promin Gold": 85, "Promin Plus": 12,
    "Promin": 200, "Savi Gold": 5, "Savi Supreme": 180,
}


def _upgrade_product_demo_stock():
    """DEMO-STOCK UPGRADER - permanently disabled (Change #45/#63).

    Change #45 replaced the whole catalog with fresh zero-stock products; the
    empty-table guards below would now fabricate demo rows on every non-Vercel
    boot. Kept as a no-op so a fresh start stays a fresh start.
    """
    return False
    changed = False
    for data in PRODUCTS:
        row = Product.query.filter_by(name=data["name"]).first()
        if not row:
            continue
        old = _OLD_PRODUCT_STOCK.get(data["name"])
        if old is None or row.stock != old:
            continue
        row.stock = data["stock"]
        row.status = compute_product_status(row.stock)
        changed = True
    if changed:
        db.session.commit()
    return changed


def _migrate_purchase_phone():
    """Add vendor_phone column to existing savi.db files.

    Change #63: the demo phone-number backfill that used to run here is removed -
    a fresh start must not fabricate sample phone numbers into real rows. Only
    the column ADD (idempotent) remains.
    """
    from sqlalchemy import text, or_
    cols = _table_columns("purchases")
    if "vendor_phone" not in cols:
        db.session.execute(text("ALTER TABLE purchases ADD COLUMN vendor_phone VARCHAR(30) DEFAULT ''"))
        db.session.commit()
    return


def _migrate_purchase_details():
    """Add the extra purchase columns (vehicle, bill/contain qty, slip, arrival)."""
    from sqlalchemy import text
    cols = _table_columns("purchases")
    additions = [
        ("vehicle_number", "VARCHAR(30) DEFAULT ''"),
        ("bill_quantity", "VARCHAR(40) DEFAULT ''"),
        ("bill_unit", "VARCHAR(10) DEFAULT 'Kgs'"),
        ("contain_quantity", "VARCHAR(40) DEFAULT ''"),
        ("contain_unit", "VARCHAR(10) DEFAULT 'Kgs'"),
        ("slip_attachment", "VARCHAR(255) DEFAULT ''"),
        ("arrival_date", "VARCHAR(20) DEFAULT ''"),
        ("driver_name", "VARCHAR(120) DEFAULT ''"),
        ("driver_phone", "VARCHAR(30) DEFAULT ''"),
        ("invoice_number", "VARCHAR(60) DEFAULT ''"),
    ]
    changed = False
    for name, spec in additions:
        if name not in cols:
            db.session.execute(text(f"ALTER TABLE purchases ADD COLUMN {name} {spec}"))
            changed = True
    if changed:
        db.session.commit()


def _migrate_purchase_credit():
    """Add the credit-period columns (credit_days, credit_due_date) to purchases."""
    from sqlalchemy import text
    cols = _table_columns("purchases")
    additions = [
        ("credit_days", "INTEGER DEFAULT 0"),
        ("credit_due_date", "VARCHAR(20) DEFAULT ''"),
    ]
    changed = False
    for name, spec in additions:
        if name not in cols:
            db.session.execute(text(f"ALTER TABLE purchases ADD COLUMN {name} {spec}"))
            changed = True
    if changed:
        db.session.commit()


def _migrate_slip_blobs():
    """Add the DB blob columns for uploaded slips (Vercel has no durable file
    storage, so purchase/driver slip bytes are stored in the shared DB)."""
    from sqlalchemy import text
    for table in ("purchases", "driver_details"):
        cols = _table_columns(table)
        additions = [
            ("slip_data", "MEDIUMBLOB"),
            ("slip_mime", "VARCHAR(100) DEFAULT ''"),
        ]
        changed = False
        for name, spec in additions:
            if name not in cols:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {spec}"))
                changed = True
        if changed:
            db.session.commit()


def _migrate_weighment_files():
    """Add the second attachment (Weigment) columns to purchases + driver_details.
    Mirror in user/app.py - admin runs first in combined mode."""
    from sqlalchemy import text
    for table in ("purchases", "driver_details"):
        cols = _table_columns(table)
        additions = [
            ("weighment_attachment", "VARCHAR(255) DEFAULT ''"),
            ("weighment_data", "MEDIUMBLOB"),
            ("weighment_mime", "VARCHAR(100) DEFAULT ''"),
        ]
        changed = False
        for name, spec in additions:
            if name not in cols:
                db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {spec}"))
                changed = True
        if changed:
            db.session.commit()

def _migrate_product_last_update():
    """Add products.last_update (Production page + sales stamp)."""
    from sqlalchemy import text
    cols = _table_columns("products")
    if "last_update" not in cols:
        db.session.execute(text("ALTER TABLE products ADD COLUMN last_update VARCHAR(80) DEFAULT ''"))
        db.session.commit()


def _migrate_third_party_last_update():
    """Change #53: third_party_items.last_update - production-save stamp for
    the Product Details 'Last Update' column (same format as Product)."""
    from sqlalchemy import text
    cols = _table_columns("third_party_items")
    if "last_update" not in cols:
        db.session.execute(text("ALTER TABLE third_party_items ADD COLUMN last_update VARCHAR(60) DEFAULT ''"))
        db.session.commit()



def _migrate_vendor_products():
    """Add the products Text column to vendors (before any full-row Vendor query)."""
    from sqlalchemy import text
    cols = _table_columns("vendors")
    if "products" not in cols:
        db.session.execute(text("ALTER TABLE vendors ADD COLUMN products TEXT"))
        db.session.commit()


def _migrate_raw_material_sales():
    """RM/Product Sales re-spec columns: customer contact, invoice number,
    bill qty, balance amount, weigment file. Also widen the sales blob columns -
    create_all builds db.LargeBinary as BLOB (64KB) which is too small for
    4MB invoice photos on MySQL/TiDB."""
    from sqlalchemy import text
    insp = inspect(db.engine)
    if "raw_material_sales" not in insp.get_table_names():
        return
    changed = False
    sales_adds = {
        "raw_material_sales": (
            ("customer_contact", "VARCHAR(20) DEFAULT ''"),
            ("invoice_number", "VARCHAR(60) DEFAULT ''"),
            ("bill_quantity", "INTEGER DEFAULT 0"),
            ("balance_amount", "INTEGER DEFAULT 0"),
            ("total_amount", "INTEGER DEFAULT 0"),
            ("weighment_attachment", "VARCHAR(255) DEFAULT ''"),
            ("weighment_data", "MEDIUMBLOB"),
            ("weighment_mime", "VARCHAR(100) DEFAULT ''"),
        ),
        "product_sales": (
            ("invoice_number", "VARCHAR(60) DEFAULT ''"),
            ("customer_contact", "VARCHAR(20) DEFAULT ''"),
            ("balance_amount", "INTEGER DEFAULT 0"),
            ("total_amount", "INTEGER DEFAULT NULL"),
            # Change #56 (2026-10-09): multi-product Add Sales.
            ("location", "VARCHAR(120) DEFAULT ''"),
            ("items", "TEXT"),
            # Change #59 (2026-10-09): Salesman Name box + column.
            ("salesman_name", "VARCHAR(120) DEFAULT ''"),
            # Change #60 (2026-10-09): Globle Discount (amount OR percentage);
            # Amount to be paid = Total Amount - discount, computed at render.
            ("discount_amount", "FLOAT DEFAULT NULL"),
            ("discount_percent", "FLOAT DEFAULT NULL"),
        ),
    }
    for table, specs in sales_adds.items():
        if table not in insp.get_table_names():
            continue
        cols = {column['name']: str(column['type'])
                for column in insp.get_columns(table)}
        for name, spec in specs:
            if name not in cols:
                db.session.execute(text(
                    f"ALTER TABLE {table} ADD COLUMN {name} {spec}"))
                changed = True
    if db.engine.dialect.name == "mysql":
        for table in ("raw_material_sales", "product_sales"):
            if table not in insp.get_table_names():
                continue
            for column in insp.get_columns(table):
                if column['name'] == 'invoice_data' and str(column['type']).upper() == 'BLOB':
                    db.session.execute(text(
                        f"ALTER TABLE {table} MODIFY invoice_data MEDIUMBLOB"))
                    changed = True
    if changed:
        db.session.commit()


def _migrate_settings_contact():
    """Add the admin contact phone column (user portal Contact Admin buttons)."""
    from sqlalchemy import text
    cols = _table_columns("settings")
    if "admin_contact_phone" not in cols:
        db.session.execute(text("ALTER TABLE settings ADD COLUMN admin_contact_phone VARCHAR(30) DEFAULT ''"))
        db.session.commit()


def _migrate_backup_settings():
    """Daily backup settings (local folder + Google Drive synced folder)."""
    from sqlalchemy import text
    cols = _table_columns("settings")
    additions = (
        ("backup_enabled", "BOOLEAN DEFAULT 1"),
        ("backup_local_dir", "VARCHAR(500) DEFAULT ''"),
        ("backup_drive_dir", "VARCHAR(500) DEFAULT ''"),
        ("backup_keep_days", "INTEGER DEFAULT 30"),
        ("backup_last_run", "VARCHAR(60) DEFAULT ''"),
        ("backup_last_status", "VARCHAR(500) DEFAULT ''"),
        ("backup_last_slot", "VARCHAR(40) DEFAULT ''"),
    )
    changed = False
    for name, spec in additions:
        if name not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN %s %s" % (name, spec)))
            changed = True
    if changed:
        db.session.commit()


def _migrate_remarks_notes():
    """Change #57: free-text Notes box on the Remarks Add modal."""
    from sqlalchemy import text
    cols = _table_columns("sales_remarks")
    if cols and "notes" not in cols:
        db.session.execute(text(
            "ALTER TABLE sales_remarks ADD COLUMN notes TEXT"))
        db.session.commit()


def _migrate_third_party_image():
    """3rd-party product photo column (admin dashboard camera upload)."""
    from sqlalchemy import text
    if "image" not in _table_columns("third_party_items"):
        db.session.execute(text("ALTER TABLE third_party_items ADD COLUMN image VARCHAR(120) DEFAULT ''"))
        db.session.commit()


def _migrate_product_image_blobs():
    """Dashboard product/3rd-party photos stored in the DATABASE.

    On Vercel there is no S3 bucket, so save_upload() used to raise and every
    camera upload answered 503 - the photo never saved. The bytes now go into
    MEDIUMBLOB columns (max 2 MB each) and /product-images serves them, the
    same fallback the invoice/weighment slips already use. create_all builds
    db.LargeBinary as a 64 KB BLOB, so MySQL needs the explicit MEDIUMBLOB.
    """
    from sqlalchemy import text
    is_mysql = db.engine.dialect.name.startswith("mysql")
    blob_type = "MEDIUMBLOB" if is_mysql else "BLOB"
    for table in ("products", "third_party_items"):
        cols = _table_columns(table)
        changed = False
        if "image_data" not in cols:
            db.session.execute(text(
                f"ALTER TABLE {table} ADD COLUMN image_data {blob_type}"))
            changed = True
        if "image_mime" not in cols:
            db.session.execute(text(
                f"ALTER TABLE {table} ADD COLUMN image_mime VARCHAR(100)"))
            changed = True
        if changed:
            db.session.commit()


def _image_mime_for_ext(ext):
    return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "webp": "image/webp"}.get((ext or "").lower().lstrip("."), "image/jpeg")


def _migrate_resource_stock_flows():
    """Add the purchased/sold counters so closing = opening + purchased - consumed - sold."""
    from sqlalchemy import text
    cols = _table_columns("resources")
    changed = False
    for name, spec in (("purchased", "INTEGER DEFAULT 0"), ("sold", "INTEGER DEFAULT 0")):
        if name not in cols:
            db.session.execute(text(f"ALTER TABLE resources ADD COLUMN {name} {spec}"))
            changed = True
    if changed:
        db.session.commit()
    # Older builds subtracted production usage straight from closing. Move any
    # such baked-in gap into consumed so the formula reproduces the SAME number.
    drifted = False
    for r in Resource.query.all():
        gap = ((r.opening or 0) + (r.purchased or 0)
               - (r.consumed or 0) - (r.sold or 0) - (r.closing or 0))
        if gap > 0:
            r.consumed = (r.consumed or 0) + gap
            drifted = True
    if drifted:
        db.session.commit()


def _backfill_vendor_products():
    """Populate the products JSON for existing vendors that have no products yet."""
    changed = False
    for v in Vendor.query.all():
        if (v.products or "").strip():
            continue
        name = (v.product or "").strip()
        if not name:
            continue
        key = v.category if v.category in ("Raw Materials", "Medicines", "Chemicals") else "Products"
        v.products = json.dumps({key: [p.strip() for p in name.split(',') if p.strip()]})
        changed = True
    if changed:
        db.session.commit()


VENDOR_CATEGORIES = ("Raw Materials", "Medicines", "Chemicals", "All Materials")

FORMULA_CATEGORIES = {
    # Change #45 (2026-10-06): user's replacement catalog - names are verbatim
    # (his spellings are the labels). Keep in sync with RESOURCE_CATEGORIES in
    # admin/seed.py + user/seed.py.
    "Raw Materials": [
        "Maize", "Maize DDGS", "Maize Bran", "Maize DOC", "Varagu",
        "Copra(Coconut Doc)", "Rice Polish", "Hullar", "Coffee Husk", "Rapeseed",
        "Ragi husk", "Rice", "DORB", "Soya", "Cotton DOC", "GNC cake", "Rice DDGS",
        "Maize Powder", "Wheat Bran", "Jowari", "Molasses", "Turmeric Spent",
        "Hallar polish", "Fat", "Molasses Plus", "Stone grit", "Micro fat",
        "Copra Cake (AK)", "Pomegranate Powder", "Liver Powder", "Dried Paneer",
        "HUSK POWDER", "Biscuit Animal Feed",
    ],
    "Chemicals": [
        "Calcium LSP(Calcite powder)", "Salt", "Urea", "Bentonite", "DCP",
        "Rumikon (soda)", "Alkarb (sodium carbonate)", "Magnesium sulpate", "Sodakarb",
    ],
    "Medicines": [
        "Mineral powder", "Methinione", "sugarcap", "Yeast",
        "Heptacare(Toxin binder)", "Kemzyme fibroforte", "kemtrace maxim", "Mycocurb",
        "Relimax 9", "Endox dry", "Lysine", "Toxifin 360", "Metipearl", "Toxifin 300",
        "Kemtrace chromium", "Selliseo", "Biotin 2%", "Liv 52", "Kemtrace BMX",
        "Choline 60", "Layer Spl", "Aleta", "Clostat-365", "Lexi-vit",
        "Kemzyme One advance hc Dry", "Addarome vanco", "Nipro-55", "Zues Enzyme",
        "Poultry Methionine", "Aromax", "Yea-sacc", "Biochal BAD3E", "Metipro",
        "PROTEASE", "PHYTASE", "NPS Enzyme",
    ],
}


def _vendor_category_for(product):
    """Guess a vendor category from the product name."""
    text = str(product or "").lower()
    medicine_keys = ("liv ", "liv52", "clostat", "aleta", "aletta", "mycocurb",
                     "toxifin", "heptacare", "relimax", "metipearl", "metipro",
                     "kemtrace", "biocahal", "lexi", "vanco", "addarome", "vitamin")
    chemical_keys = ("dicalcium", "phosphate", "limestone", "mineral", "calcium",
                     "bentonite", "salt", "soda", "dcp", "choline", "grit",
                     "molasses", "urea", "carbonate")
    if any(k in text for k in medicine_keys):
        return "Medicines"
    if any(k in text for k in chemical_keys):
        return "Chemicals"
    return "Raw Materials"


def _migrate_vendor_category():
    """Add category column to existing savi.db vendors and backfill it."""
    from sqlalchemy import text, or_
    cols = _table_columns("vendors")
    if "category" not in cols:
        db.session.execute(text("ALTER TABLE vendors ADD COLUMN category VARCHAR(40) DEFAULT 'Raw Materials'"))
        db.session.commit()
    empties = Vendor.query.filter(
        (Vendor.category.is_(None)) | (Vendor.category == "")).all()
    for v in empties:
        v.category = _vendor_category_for(v.product)
    if empties:
        db.session.commit()


def _valid_gstin(value):
    """Return uppercased GSTIN if exactly 15 alphanumeric chars, else None."""
    import re
    v = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{15}", v):
        return v
    return None


def _migrate_production_machine():
    """Add machine/consumed columns to production; legacy rows -> Old Machine."""
    from sqlalchemy import text, or_
    cols = _table_columns("production")
    if "machine" not in cols:
        db.session.execute(text("ALTER TABLE production ADD COLUMN machine VARCHAR(20) DEFAULT 'Old Machine'"))
    if "consumed" not in cols:
        db.session.execute(text("ALTER TABLE production ADD COLUMN consumed INTEGER DEFAULT 0"))
    db.session.commit()
    db.session.execute(text("UPDATE production SET machine='Old Machine' WHERE machine IS NULL OR machine=''"))
    db.session.execute(text("UPDATE production SET consumed=0 WHERE consumed IS NULL"))
    db.session.commit()


def _seed_formula_sheet():
    """One-time seed: every register item gets an old-machine sample qty."""
    import hashlib
    if FormulaSheet.query.count() > 0:
        return False
    _RC = RESOURCE_CATEGORIES
    for group, items in _RC.items():
        for name, _cat in items:
            digest = hashlib.md5(f"formula-sheet|{group}|{name}".encode()).hexdigest()
            old = 10 + (int(digest[:8], 16) % 51)
            db.session.add(FormulaSheet(category=group, material=name,
                                        old_machine=old, new_machine=0,
                                        updated=datetime.now().strftime('%d %b %Y')))
    db.session.commit()
    return True


def _migrate_third_party():
    """Add materials/price columns to existing third_party_items."""
    from sqlalchemy import text, or_
    cols = _table_columns("third_party_items")
    if "materials" not in cols:
        db.session.execute(text("ALTER TABLE third_party_items ADD COLUMN materials TEXT"))
    if "price" not in cols:
        db.session.execute(text("ALTER TABLE third_party_items ADD COLUMN price FLOAT DEFAULT 0"))
    db.session.commit()


def _migrate_formula_sheet_product():
    """Add product column to existing formula_sheet rows (product-scoped sheets)."""
    from sqlalchemy import text, or_
    cols = _table_columns("formula_sheet")
    if "product" not in cols:
        db.session.execute(text("ALTER TABLE formula_sheet ADD COLUMN product VARCHAR(120) DEFAULT ''"))
        db.session.commit()


def _migrate_material_daily_supervisor():
    """Add supervisor_name column to existing material_daily rows."""
    from sqlalchemy import text
    cols = _table_columns("material_daily")
    if "supervisor_name" not in cols:
        db.session.execute(text("ALTER TABLE material_daily ADD COLUMN supervisor_name VARCHAR(120) DEFAULT ''"))
        db.session.commit()


def _migrate_material_daily_opening():
    """Add opening column to existing material_daily rows (legacy safety)."""
    from sqlalchemy import text
    cols = _table_columns("material_daily")
    if "opening" not in cols:
        db.session.execute(text("ALTER TABLE material_daily ADD COLUMN opening INTEGER DEFAULT 0"))
        db.session.commit()


def _migrate_material_daily_shift():
    """Add the shift column (morning/night) to existing material_daily rows."""
    from sqlalchemy import text
    cols = _table_columns("material_daily")
    if "shift" not in cols:
        db.session.execute(text("ALTER TABLE material_daily ADD COLUMN shift VARCHAR(10) DEFAULT 'morning'"))
        db.session.execute(text("UPDATE material_daily SET shift = 'morning' WHERE shift IS NULL OR shift = ''"))
        db.session.commit()


def _migrate_material_daily_bags():
    """Add per-shift typed bag columns (old_bags/new_bags) to material_daily.

    NULL on legacy rows means 'bags were never stored for this shift'.
    Also adds old_qty/new_qty (3rd-party supervisor-typed machine qty in kg;
    NULL = never stored for this shift).
    """
    from sqlalchemy import text
    try:
        cols = _table_columns("material_daily")
        if "old_bags" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN old_bags INTEGER"))
        if "new_bags" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN new_bags INTEGER"))
        if "old_qty" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN old_qty FLOAT"))
        if "new_qty" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN new_qty FLOAT"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_pm_batches():
    """Add old/new batch columns to existing product_materials rows."""
    from sqlalchemy import text, or_
    cols = _table_columns("product_materials")
    if "old_batches" not in cols:
        db.session.execute(text("ALTER TABLE product_materials ADD COLUMN old_batches INTEGER DEFAULT 0"))
    if "new_batches" not in cols:
        db.session.execute(text("ALTER TABLE product_materials ADD COLUMN new_batches INTEGER DEFAULT 0"))
    if "qty_per_batch" in cols:
        try:
            db.session.execute(text("ALTER TABLE product_materials DROP COLUMN qty_per_batch"))
        except Exception:
            db.session.rollback()
    db.session.commit()


def _migrate_maintenance_mode():
    """Add maintenance_mode column to existing settings rows."""
    from sqlalchemy import text, or_
    cols = _table_columns("settings")
    if "maintenance_mode" not in cols:
        db.session.execute(text("ALTER TABLE settings ADD COLUMN maintenance_mode BOOLEAN DEFAULT 0"))
        db.session.commit()
    if "maintenance_message" not in cols:
        db.session.execute(text("ALTER TABLE settings ADD COLUMN maintenance_message TEXT"))
        db.session.commit()


def _migrate_product_published():
    """Add published column to existing products table."""
    from sqlalchemy import text, or_
    cols = _table_columns("products")
    if "published" not in cols:
        # Products that existed before the publish feature stay visible by default.
        db.session.execute(text("ALTER TABLE products ADD COLUMN published BOOLEAN DEFAULT 0"))
        db.session.execute(text("UPDATE products SET published = 1"))
        db.session.commit()


def _migrate_third_party_published():
    """Add published column to existing third_party_items table."""
    from sqlalchemy import text, or_
    cols = _table_columns("third_party_items")
    if "published" not in cols:
        # Items that existed before the publish feature stay visible by default.
        db.session.execute(text("ALTER TABLE third_party_items ADD COLUMN published BOOLEAN DEFAULT 0"))
        db.session.execute(text("UPDATE third_party_items SET published = 1"))
        db.session.commit()


def _backfill_legacy_published():
    """One-time safety net: if nobody has ever used the publish feature on a
    given table, keep its existing rows visible on the user side.

    Each table is handled independently. Once the admin publishes (or
    unpublishes) any single row in that table this stops running for it, so
    it never overrides deliberate choices afterwards.
    """
    try:
        from sqlalchemy import text
        pub_products = db.session.execute(text("SELECT COUNT(*) FROM products WHERE published = 1")).scalar() or 0
        if pub_products == 0:
            db.session.execute(text("UPDATE products SET published = 1"))
        pub_thirds = db.session.execute(text("SELECT COUNT(*) FROM third_party_items WHERE published = 1")).scalar() or 0
        if pub_thirds == 0:
            db.session.execute(text("UPDATE third_party_items SET published = 1"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_vendor_gstin():
    """Add gstin column to existing savi.db vendors."""
    from sqlalchemy import text, or_
    cols = _table_columns("vendors")
    if "gstin" not in cols:
        db.session.execute(text("ALTER TABLE vendors ADD COLUMN gstin VARCHAR(15) DEFAULT ''"))
        db.session.commit()


def _next_v_number() -> str:
    """Generate the next VO record number (internal reference)."""
    max_num = 0
    for (vnum,) in db.session.query(Purchase.v_number).all():
        digits = "".join(ch for ch in (vnum or "") if ch.isdigit())
        if digits:
            try:
                max_num = max(max_num, int(digits))
            except ValueError:
                pass
    return f"VO{max_num + 1:04d}"


def _daily_sample(product_name, material, datestr):
    """Deterministic daily consumption sample (stable across restarts)."""
    import hashlib
    digest = hashlib.md5(f"{product_name}|{material}|{datestr}".encode()).hexdigest()
    return 5 + (int(digest[:8], 16) % 18)


def _daily_backfill_row(row, start_date_str, days=7):
    """Create the stored daily log row for a material on its snapshot day.

    Change #63: the 7-day backward loop that used to fabricate deterministic
    sample consumption (5-22 kg/day via _daily_sample) is REMOVED - it invented
    production history on every boot and defeated the fresh-start wipe. Only the
    legitimate snapshot-day row (opening from the shift chain / Resource
    fallback when the snapshot opening is empty) is still created, which the
    PUC opening-stock chain depends on.
    """
    try:
        base = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    except ValueError:
        return 0
    created = 0
    start_opening = row.opening or 0
    start_consumed = row.consumed or 0
    start_closing = row.closing if row.closing is not None else max(start_opening - start_consumed, 0)
    if start_opening <= 0:
        # Empty snapshot: seed from the shift chain / Resource closing instead
        # of writing a flat zero (zero rows poisoned the whole rolling chain).
        start_opening = _shift_opening(
            row.product_name, row.material, start_date_str, 'morning',
            _resource_closing_map().get(str(row.material).strip(), 0))
        start_closing = max(start_opening - start_consumed, 0)
    db.session.add(MaterialDaily(
        product_name=row.product_name, material=row.material, date=start_date_str,
        opening=int(start_opening), consumed=start_consumed, closing=int(start_closing),
        batch=row.batch or ""))
    created += 1
    return created


def _daily_backfill_all(product_id, product_name, days=7):
    """Backfill daily logs for every material row of a (new) product."""
    today = datetime.now().strftime("%Y-%m-%d")
    for row in ProductMaterial.query.filter_by(product_id=product_id).order_by(ProductMaterial.id).all():
        _daily_backfill_row(row, today, days=days)


def _seed_material_daily():
    """DEMO daily-log seeder - permanently disabled (Change #63).

    It used to fabricate a 7-day consumption history for every product
    material on an empty (or partially-filled) daily table. Kept as a no-op
    so a fresh start stays a fresh start; the runtime helpers
    (_daily_backfill_row on product create, _roll_daily_forward on page
    render) cover the legitimate snapshot-day chain seeding when needed.
    """
    return False


def _prod_batch_prefix(product_name):
    known = {
        "Savi Gold Plus": "SGP", "Promin Gold": "PGL", "Promin Plus": "PPL",
        "Promin": "PRM", "Savi Gold": "SGD", "Savi Supreme": "SSP",
    }
    if product_name in known:
        return known[product_name]
    return ("".join(w[0] for w in product_name.split() if w).upper()[:3] or "PRD")


def _prod_next_batch(prefix, datestr):
    compact = datestr.replace("-", "")
    candidate = f"{prefix}-{compact}"
    suffix = 2
    while ProductionEntry.query.filter_by(batch=candidate).first():
        candidate = f"{prefix}-{compact}-{suffix}"
        suffix += 1
    return candidate


def _seed_production_sample():
    """One-time stored sample: ~30 days of daily production per product.

    Historical sample only — product stock is NOT changed (current stock
    already reflects the live position) and no extra stock movements are
    logged, so reports stay clean while History/Monthly look alive.
    """
    # Change #45: demo production history was removed with the fresh catalog -
    # do not fabricate sample rows (the empty-table guards below would now
    # fire on every non-Vercel boot).
    return False
    if os.environ.get("SAVI_NO_SEED") == "1":
        return False
    import hashlib
    from datetime import timedelta
    if ProductionEntry.query.filter(ProductionEntry.date < "2026-09-03").count() > 0:
        return False
    start = datetime.strptime("2026-08-04", "%Y-%m-%d").date()
    names = [p.name for p in Product.query.order_by(Product.id).all()]
    for offset in range(30):
        day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        if day >= "2026-09-03":
            continue
        for name in names:
            digest = hashlib.md5(f"prod-sample|{name}|{day}".encode()).hexdigest()
            qty = 10 + (int(digest[:8], 16) % 41)  # 10..50 bags
            batch = _prod_next_batch(_prod_batch_prefix(name), day)
            db.session.add(ProductionEntry(product=name, date=day, quantity=qty,
                                           batch=batch, status="Completed", notes=""))
    db.session.commit()
    return True


def _seed_production_sample_new():
    """One-time stored sample for the New Machine tab (14 days x products).

    Historical sample only — no stock change, no extra movements.
    """
    # Change #45: demo production history removed - stay disabled (see above).
    return False
    if os.environ.get("SAVI_NO_SEED") == "1":
        return False
    import hashlib
    from datetime import timedelta
    if ProductionEntry.query.filter_by(machine="New Machine").count() > 0:
        return False
    start = datetime.strptime("2026-07-21", "%Y-%m-%d").date()
    names = [p.name for p in Product.query.order_by(Product.id).all()]
    for offset in range(14):
        day = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for name in names:
            digest = hashlib.md5(f"prod-sample-new|{name}|{day}".encode()).hexdigest()
            qty = 8 + (int(digest[:8], 16) % 33)  # 8..40 bags
            batch = _prod_next_batch(_prod_batch_prefix(name), day)
            db.session.add(ProductionEntry(product=name, date=day, quantity=qty,
                                           batch=batch, status="Completed", notes="",
                                           machine="New Machine", consumed=0))
    db.session.commit()
    return True


def _roll_daily_forward(product_name=None):
    """Ensure the daily log reaches today. Missing past days get chained
    sample rows; today always mirrors the live snapshot. Returns True if added."""
    from datetime import timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    query = ProductMaterial.query.order_by(ProductMaterial.id)
    if product_name:
        query = query.filter_by(product_name=product_name)
    mats = query.all()
    if not mats:
        return False
    res_map = _resource_closing_map()
    changed = False
    for row in mats:
        logs = MaterialDaily.query.filter_by(
            product_name=row.product_name, material=row.material).order_by(MaterialDaily.date).all()
        if not logs:
            _daily_backfill_row(row, today, days=7)
            changed = True
            continue
        last_date = logs[-1].date
        if last_date >= today:
            continue
        prev_closing = logs[-1].closing
        day = datetime.strptime(last_date, "%Y-%m-%d").date() + timedelta(days=1)
        while day.strftime("%Y-%m-%d") <= today:
            ds = day.strftime("%Y-%m-%d")
            if ds == today:
                open_today = row.opening or 0
                cons_today = row.consumed or 0
                if open_today <= 0:
                    # Mirror day must not start from an empty snapshot - take
                    # the rolled chain / Resource closing instead of a zero.
                    open_today = _shift_opening(row.product_name, row.material, ds, 'morning',
                                                res_map.get(str(row.material).strip(), 0))
                    close_today = max(open_today - cons_today, 0)
                else:
                    close_today = row.closing if row.closing is not None else max(open_today - cons_today, 0)
                db.session.add(MaterialDaily(
                    product_name=row.product_name, material=row.material, date=ds,
                    opening=int(open_today), consumed=cons_today, closing=int(close_today),
                    batch=row.batch or ""))
            else:
                # Change #63: middle missing days used to be filled with
                # fabricated _daily_sample consumption (5-22 kg/day) - that
                # invented history on every render. A missing middle day is
                # now a zero-consumption carry-forward (opening = previous
                # closing, consumed = 0) so the chain stays numerically
                # consistent without inventing production.
                consumed = 0
                closing = max(prev_closing - consumed, 0)
                db.session.add(MaterialDaily(
                    product_name=row.product_name, material=row.material, date=ds,
                    opening=prev_closing, consumed=consumed, closing=closing,
                    batch=row.batch or ""))
                prev_closing = closing
            changed = True
            day += timedelta(days=1)
    if changed:
        db.session.commit()
    return changed



def _sync_resource_register_items():
    """Keep Resource Maintenance registers exactly in sync with the configured item lists."""
    valid = {group: {name: category for name, category in items}
             for group, items in RESOURCE_CATEGORIES.items()}
    # Rename the legacy UI/database group to the new "Chemicals" register.
    legacy = Resource.query.filter_by(group='Chemicals / Feed Additives').all()
    for row in legacy:
        row.group = 'Chemicals'

    # Change #63: a custom resource a user added via Add Resource is NOT demo
    # data - do not delete it on boot just because it is absent from the static
    # catalog. (Previously this loop wiped every non-catalog resource each
    # startup, silently destroying user data.)
    # for row in Resource.query.all():
    #     if row.group not in valid or row.name not in valid[row.group]:
    #         db.session.delete(row)

    db.session.flush()
    changed = bool(legacy)
    now = datetime.now().strftime('%d %b %Y, %I:%M %p')
    for group, items in valid.items():
        for name, item_category in items.items():
            row = Resource.query.filter_by(group=group, name=name).first()
            if not row:
                # Change #63: a missing catalog resource is created EMPTY (0
                # kg), never at a fabricated 500 kg demo stock - a fresh start
                # must stay at zero.
                opening, consumed = 0, 0
                closing = opening
                row = Resource(group=group, name=name, item_category=item_category, unit='kg',
                               opening=opening, consumed=consumed, closing=closing, min_stock=250,
                               status=compute_resource_status(closing, 250), last_update='')
                db.session.add(row)
                changed = True
            elif row.item_category != item_category:
                row.item_category = item_category
                changed = True
    if changed:
        db.session.commit()
    return changed


def _migrate_drop_resource_expiry():
    """Remove the obsolete expire_date column from resources (dropped from UI)."""
    from sqlalchemy import text
    try:
        cols = _table_columns("resources")
        if "expire_date" in cols:
            db.session.execute(text("ALTER TABLE resources DROP COLUMN expire_date"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_active_shift():
    """Add the active_shift column used to lock the admin Production Update."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        if "active_shift" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN active_shift VARCHAR(10) DEFAULT 'morning'"))
            db.session.commit()
        settings = Setting.query.order_by(Setting.id).first()
        if not settings:
            settings = Setting()
            db.session.add(settings)
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_change_version():
    """Add the change_version column the admin uses to detect user-side updates."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        if "change_version" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN change_version INTEGER DEFAULT 0"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_admin_change_version():
    """Add an extra counter the user portal uses to detect admin-side updates."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        if "admin_change_version" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN admin_change_version INTEGER DEFAULT 0"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _change_version_value():
    """Current user-update version number."""
    settings = _get_settings()
    try:
        return int(getattr(settings, 'change_version', 0) or 0)
    except Exception:
        return 0


def _admin_change_version_value():
    """Current admin-update version number (watched by the user portal)."""
    settings = _get_settings()
    try:
        return int(getattr(settings, 'admin_change_version', 0) or 0)
    except Exception:
        return 0


@app.after_request
def _flag_admin_changes(response):
    """Every successful admin-portal write bumps admin_change_version so the
    user portal knows data was updated here (never logs anyone out, just
    prompts a refresh on the user side)."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and response.status_code < 400:
        path = request.path.rstrip('/') or '/'
        excluded = {'/login', '/logout', '/api/health',
                    '/api/change-monitor', '/api/admin-change-monitor',
                    '/api/maintenance-status', '/api/presence'}
        if path not in excluded and not path.startswith('/static/'):
            settings = _get_settings()
            try:
                settings.admin_change_version = int(getattr(settings, 'admin_change_version', 0) or 0) + 1
                db.session.commit()
            except Exception:
                db.session.rollback()
    return response


def _migrate_active_user():
    """Add the columns that track the currently logged-in user-portal user."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        wanted = {
            "active_user_id": "INTEGER DEFAULT 0",
            "active_user_name": "VARCHAR(120) DEFAULT ''",
            "active_user_login": "VARCHAR(120) DEFAULT ''",
            "active_user_phone": "VARCHAR(30) DEFAULT ''",
            "active_user_shift": "VARCHAR(20) DEFAULT ''",
            "active_user_at": "VARCHAR(40) DEFAULT ''",
        }
        changed = False
        for name, ddl in wanted.items():
            if name not in cols:
                db.session.execute(text("ALTER TABLE settings ADD COLUMN %s %s" % (name, ddl)))
                changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_user_password_plain():
    """Add a plain-text copy of user passwords so the admin User Access table
    can display the password it created. Authentication still verifies the
    werkzeug hash in `password`; the plain copy is display convenience only."""
    from sqlalchemy import text
    try:
        cols = _table_columns("users")
        if "password_plain" not in cols:
            db.session.execute(text("ALTER TABLE users ADD COLUMN password_plain VARCHAR(255) DEFAULT ''"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _active_user_info():
    """Details of the user-portal account currently logged in, for the admin UI."""
    try:
        settings = _get_settings()
        return {
            "id": getattr(settings, 'active_user_id', 0) or 0,
            "full_name": getattr(settings, 'active_user_name', '') or "",
            "login_id": getattr(settings, 'active_user_login', '') or "",
            "phone": getattr(settings, 'active_user_phone', '') or "",
            "shift": getattr(settings, 'active_user_shift', '') or "",
            "at": getattr(settings, 'active_user_at', '') or "",
        }
    except Exception:
        return {"id": 0, "full_name": "", "login_id": "", "phone": "", "shift": "", "at": ""}


def _seed_default_admin():
    """Default admin seeding is intentionally disabled."""
    return False


def _active_shift_value():
    """Return the current locked shift ('morning' or 'night') for the admin."""
    settings = _get_settings()
    shift = str(getattr(settings, 'active_shift', '') or 'morning').strip().lower()
    return shift if shift in ('morning', 'night') else 'morning'


def _set_active_shift(shift):
    """Persist the shift the admin Production Update page is locked to."""
    shift = (shift or '').strip().lower()
    if shift not in ('morning', 'night'):
        return
    settings = _get_settings()
    settings.active_shift = shift
    db.session.commit()


def _normalize_shift(shift, default='morning'):
    value = str(shift or '').strip().lower()
    return value if value in ('morning', 'night') else default


def _shift_opening(product_name, material, date_str, shift, fallback=0):
    """Opening stock for a shift = closing stock of the previous shift.

    Morning follows the previous date's night shift (or morning shift when no
    night shift was recorded); night follows the same date's morning shift.
    Stored rows with opening<=0 are broken zero snapshots (created before real
    stock existed / from an empty formula opening) and are SKIPPED so the chain
    falls through to the supplied fallback (Resource Maintenance closing, which
    Purchase Maintenance feeds). Legacy shift-NULL rows count as morning, same
    as _shift_daily_rows. When a chain value exists it is returned UNLESS the
    fallback (live stock fed by purchases) is higher - a purchase after the
    last snapshot must show.
    """
    from datetime import timedelta
    shift = _normalize_shift(shift)
    _morning_or_legacy = or_(MaterialDaily.shift == 'morning',
                             MaterialDaily.shift.is_(None),
                             MaterialDaily.shift == '')

    def _usable(m):
        return m is not None and m.closing is not None and (m.opening or 0) > 0

    chain = None
    try:
        if shift == 'night':
            m = MaterialDaily.query.filter_by(product_name=product_name, material=material,
                                              date=date_str).filter(_morning_or_legacy).first()
            if _usable(m):
                chain = m.closing
        else:
            try:
                prev = (datetime.strptime(date_str, '%Y-%m-%d').date() - timedelta(days=1)).strftime('%Y-%m-%d')
            except (TypeError, ValueError):
                prev = None
            if prev:
                n = MaterialDaily.query.filter_by(product_name=product_name, material=material,
                                                  date=prev, shift='night').first()
                if _usable(n):
                    chain = n.closing
                else:
                    pm = MaterialDaily.query.filter_by(product_name=product_name, material=material,
                                                       date=prev).filter(_morning_or_legacy).first()
                    if _usable(pm):
                        chain = pm.closing
    except Exception:
        pass
    if chain is None:
        return fallback
    # Item 9 (2026-10-01): Opening Stock also auto-fetches Purchase Maintenance
    # for the material - the fallback is Resource Maintenance closing (which
    # purchases feed), so a purchase made after the last shift snapshot shows
    # up instead of the stale chain value. Never show LESS than the live stock.
    return max(chain, fallback)


def _norm_material(name):
    """Case/space-insensitive key for matching a formula material to a Resource.

    Formula rows and Resource Maintenance rows are typed independently, so the
    same material can exist as 'Maize Doc' on the formula and 'Maize DOC' on
    Resource Maintenance. Matching must be tolerant of that, otherwise the
    purchase-fed stock is never found and Opening Stock falls back to the
    (usually 0) formula snapshot instead.
    """
    return re.sub(r"\s+", " ", str(name or "")).strip().casefold()


def _resource_by_name(name):
    """Tolerant Resource lookup (exact -> case/space-insensitive), matching
    the user app's _purchase_resource first two steps. Used by the admin bulk
    consume path so a formula material typed slightly differently still finds
    the live stock row to decrement."""
    text = (name or '').strip()
    if not text:
        return None
    resource = Resource.query.filter(Resource.name.ilike(text)).first()
    if resource:
        return resource
    normalized = _norm_material(text)
    for candidate in Resource.query.all():
        if _norm_material(candidate.name) == normalized:
            return candidate
    return None


class _ClosingMap(dict):
    """Resource closing lookup keyed by material name.

    Exact names win (behaviour unchanged). A case/space-insensitive match is
    also accepted, because formula rows and Resource Maintenance rows are typed
    independently ('Maize Doc' vs 'Maize DOC'), but ONLY when that match
    actually has stock - a case-insensitive hit of 0 must never override the
    formula's own opening snapshot.
    """

    def put(self, name, value):
        nm = str(name or "").strip()
        self[nm] = value
        self.setdefault(_norm_material(nm), value)

    def get(self, key, default=None):
        nm = str(key or "").strip()
        if nm in self:
            return dict.get(self, nm)
        nk = _norm_material(nm)
        if nk in self:
            v = dict.get(self, nk)
            if v:
                return v
        return default


def _resource_closing_map():
    """Material name -> Resource Maintenance closing stock (fed by purchases)."""
    mapping = _ClosingMap()
    try:
        for res in Resource.query.all():
            mapping.put(res.name, int(res.closing or 0))
    except Exception:
        pass
    return mapping


def _parse_batch_pair(batch_str):
    """Split a stored 'old/new' batch string into (old, new) integers."""
    s = str(batch_str or '').strip()
    if '/' in s:
        a, b = s.split('/', 1)
        return _stored_whole_bag_value(a) or 0, _stored_whole_bag_value(b) or 0
    return _stored_whole_bag_value(s) or 0, 0


def _shift_daily_rows(product_name, selected_daily, selected_shift):
    """Stored material_daily rows for a product/date/shift (legacy rows count as morning)."""
    rows = MaterialDaily.query.filter_by(
        product_name=product_name, date=selected_daily,
        shift=selected_shift).order_by(MaterialDaily.id).all()
    if selected_shift == 'morning':
        legacy = MaterialDaily.query.filter(
            MaterialDaily.product_name == product_name,
            MaterialDaily.date == selected_daily,
            or_(MaterialDaily.shift.is_(None), MaterialDaily.shift == '')).order_by(MaterialDaily.id).all()
        rows = rows + legacy
    return rows


def _latest_supervisor(product_name):
    """Item 8 (2026-10-05): most recent saved supervisor for a product.

    The admin Production Update page defaults to TODAY, but the update is often
    saved on an earlier date (or by the other shift), so the Supervisor chip
    rendered '-' even though an update existed for this product. Returns
    ``(name, date, shift)`` or ``('', '', '')`` when the product never had one.
    """
    row = (MaterialDaily.query
           .filter(MaterialDaily.product_name == product_name,
                   MaterialDaily.supervisor_name != '')
           .order_by(MaterialDaily.date.desc(), MaterialDaily.id.desc())
           .first())
    if not row:
        return '', '', ''
    return (row.supervisor_name or ''), (row.date or ''), (row.shift or 'morning')


def _third_party_production_rows(third_item, product_name):
    """Production Update rows for a 3rd-party product built from its formula.

    Same row interface as ProductMaterial (see _render_inventory_product) so the
    Production Summary template renders 3rd-party products unchanged.
    """
    opening_map = {}
    for line in third_item.mat_lines():
        try:
            opening_map[str(line.get('material', '')).strip()] = float(line.get('opening', 0) or 0)
        except (TypeError, ValueError):
            opening_map[str(line.get('material', '')).strip()] = 0
    formula_rows = FormulaSheet.query.filter_by(product=product_name).order_by(FormulaSheet.id).all()

    class ThirdPartyProductionRow:
        def __init__(self, formula_row, opening_qty, old_qty, new_qty):
            self.id = formula_row.id
            self.material = formula_row.material
            self.unit = 'kg'
            self.opening = opening_qty
            self.consumed = 0
            self.closing = opening_qty
            self.batch = ''
            self.status = 'Available'
            self.old_batches = old_qty
            self.new_batches = new_qty
            self.old_machine_qty = old_qty
            self.new_machine_qty = new_qty

        @property
        def total_batches(self):
            return (self.old_batches or 0) + (self.new_batches or 0)

        @property
        def total_output(self):
            return self.consumed * self.total_batches

        def to_dict(self):
            return {
                'id': self.id, 'material': self.material, 'unit': self.unit,
                'opening': self.opening, 'consumed': self.consumed,
                'closing': self.closing, 'batch': self.batch,
                'status': self.status, 'old_batches': self.old_batches or 0,
                'new_batches': self.new_batches or 0,
                'total_batches': self.total_batches,
                'total_output': self.total_output,
            }

    return [ThirdPartyProductionRow(f, opening_map.get(f.material, 0),
                                    f.old_machine or 0, f.new_machine or 0)
            for f in formula_rows]


def _compute_production_summary(product_name, selected_daily, selected_shift, rows, res_closing, today,
                                is_third_party=False):
    """Per-shift production summary for the admin Production Update page.

    Prefers the stored shift rows (which carry the rolled-forward opening);
    falls back to the live formula snapshot only when no shift has been saved
    for that date yet.
    """
    selected_shift = _normalize_shift(selected_shift)
    daily_rows = _shift_daily_rows(product_name, selected_daily, selected_shift)
    summary_rows = []
    old_bags = 0
    new_bags = 0
    if daily_rows:
        current_by_material = {r.material: r for r in rows}
        for d in daily_rows:
            cur = current_by_material.get(d.material)
            old_b, new_b = _parse_batch_pair(getattr(d, 'batch', ''))
            opening = d.opening or 0
            consumed = d.consumed or 0
            closing = d.closing if d.closing is not None else max(opening - consumed, 0)
            if opening <= 0:
                # Broken zero snapshot: rebuild Opening/Closing from the shift
                # chain (other zero rows are skipped) with the Resource closing
                # as first-stock fallback, so Opening Stock never shows a flat
                # 0 while real stock exists in Resource/Purchase Maintenance.
                opening = _shift_opening(product_name, d.material, selected_daily, selected_shift,
                                         res_closing.get(str(d.material).strip(), 0) or 0)
                closing = max(opening - consumed, 0)
            total_b = old_b + new_b
            old_c = (consumed * old_b / total_b) if total_b else 0
            new_c = (consumed * new_b / total_b) if total_b else 0
            # Standard products: the stored batch is per-shift truth - NEVER fall
            # back to the shared ProductMaterial batches (they hold whichever shift
            # saved last, so the morning view showed the night update's numbers).
            # 3rd-party rows have no stored batch, so they keep the formula spread.
            _show_old, _show_new = old_b, new_b
            if is_third_party and cur is not None:
                _show_old = old_b or (cur.old_batches or 0)
                _show_new = new_b or (cur.new_batches or 0)
            row = {**d.to_dict(),
                   'opening': opening, 'consumed': consumed, 'closing': closing,
                   'old_batches': _show_old,
                   'new_batches': _show_new,
                   'total_output': _whole_bag_count(consumed / 50.0),
                   'old_machine_bags': _whole_bag_count(old_c / 50.0),
                   'new_machine_bags': _whole_bag_count(new_c / 50.0)}
            row['total_batches'] = (row['old_batches'] or 0) + (row['new_batches'] or 0)
            summary_rows.append(row)
            old_bags += _whole_bag_count(old_c / 50.0)
            new_bags += _whole_bag_count(new_c / 50.0)
        return summary_rows, old_bags, new_bags
    has_any = MaterialDaily.query.filter_by(product_name=product_name, date=selected_daily).count() > 0
    if selected_daily == today and not has_any:
        old_m_entries = {e.material: e for e in MachineProductionEntry.query.filter_by(product_name=product_name, machine='old').all()}
        new_m_entries = {e.material: e for e in MachineProductionEntry.query.filter_by(product_name=product_name, machine='new').all()}
        for r in rows:
            d = r.to_dict()
            opening = _shift_opening(product_name, r.material, selected_daily, selected_shift,
                                     res_closing.get(str(r.material).strip(), d.get('opening') or 0))
            old_qty = float(getattr(r, 'old_machine_qty', 0) or 0)
            new_qty = float(getattr(r, 'new_machine_qty', 0) or 0)
            old_batch = float(getattr(r, 'old_batches', 0) or 0)
            new_batch = float(getattr(r, 'new_batches', 0) or 0)
            old_e = old_m_entries.get(r.material)
            new_e = new_m_entries.get(r.material)
            if is_third_party:
                # 3rd-party formula old/new values are batch counts (not per-batch
                # qty) - never multiply them (old_qty x old_batch double-counted).
                old_consumed = 0
                new_consumed = 0
            elif old_e or new_e:
                old_consumed = (old_e.consumed if old_e else 0) or 0
                new_consumed = (new_e.consumed if new_e else 0) or 0
            else:
                old_consumed = old_qty * old_batch
                new_consumed = new_qty * new_batch
            consumed = old_consumed + new_consumed
            d.update({'opening': opening, 'consumed': round(consumed, 2),
                      'closing': max(opening - consumed, 0),
                      'total_output': _whole_bag_count(consumed / 50.0),
                      'old_machine_bags': _whole_bag_count(old_consumed / 50.0),
                      'new_machine_bags': _whole_bag_count(new_consumed / 50.0)})
            old_bags += d['old_machine_bags']
            new_bags += d['new_machine_bags']
            summary_rows.append(d)
    return summary_rows, old_bags, new_bags


with app.app_context():
    try:
        if _startup_schema_current():
            raise _SchemaCurrent()
        db.create_all()
        _migrate_purchase_details()
        _migrate_purchase_credit()
        _migrate_slip_blobs()
        _migrate_weighment_files()
        _migrate_product_last_update()
        _migrate_third_party_last_update()
        _migrate_raw_material_sales()
        _migrate_settings_contact()
        _migrate_backup_settings()
        _migrate_remarks_notes()
        _migrate_resource_stock_flows()
        _migrate_third_party_image()
        _migrate_product_image_blobs()
        _migrate_vendor_products()
        _migrate_drop_resource_expiry()
        _migrate_pm_batches()
        _migrate_maintenance_mode()
        _migrate_production_machine()
        _migrate_product_published()
        _migrate_third_party_published()
        _migrate_material_daily_supervisor()
        _migrate_material_daily_shift()
        _migrate_material_daily_bags()
        _migrate_product_material_batch()
        _migrate_purchase_phone()
        _backfill_legacy_published()
        _migrate_active_shift()
        _migrate_change_version()
        _migrate_admin_change_version()
        _seed_default_admin()
        if not os.environ.get("SAVI_NO_SEED"):
            seed_database(db)
            _ensure_standard_products()
            _sync_resource_register_items()
            _seed_material_daily()
            _seed_production_sample()
            _seed_production_sample_new()
            _roll_daily_forward()
            _upgrade_product_demo_stock()
            _refresh_product_statuses()
            _backfill_vendor_products()


    except _SchemaCurrent:
        pass
    except Exception as _startup_exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning("Startup schema sync skipped: %s", _startup_exc)
def _seed_vendor_samples():
    """One-time demo vendors for Medicines and Chemicals registers."""
    samples = [
        {"vendor_name": "VetCare Pharma Distributors", "address": "#33, Hoskote Industrial Area\nBangalore - 562114", "vendor_phone": "+91 97310 44556", "gstin": "29VETCP1234K1Z5", "date_display": "02 Jun 2026", "category": "Medicines", "product": "Liv 52", "quantity": "500 Bottles", "amount_paid": 45000, "remaining": 0, "payment_mode": "UPI", "status": "Paid"},
        {"vendor_name": "Animal Health Suppliers", "address": "#9, Sayaji Rao Road\nMysore - 570001", "vendor_phone": "+91 97420 55667", "gstin": "29AHSTP5678M1Z3", "date_display": "28 May 2026", "category": "Medicines", "product": "Toxifin 360", "quantity": "200 Kg", "amount_paid": 38000, "remaining": 5000, "payment_mode": "Credit", "status": "Partial"},
        {"vendor_name": "Deccan Chemical Traders", "address": "#21, GIDC Phase 2\nHubli - 580021", "vendor_phone": "+91 97530 66778", "gstin": "29DCTRD9012N1Z7", "date_display": "25 May 2026", "category": "Chemicals", "product": "Dicalcium Phosphate", "quantity": "1,000 Kg", "amount_paid": 20000, "remaining": 0, "payment_mode": "Bank Transfer", "status": "Paid"},
        {"vendor_name": "Mineral & Additives Co", "address": "#14, Udyambag Industrial Estate\nBelgaum - 590008", "vendor_phone": "+91 97640 77889", "gstin": "29MINAD3456P1Z9", "date_display": "20 May 2026", "category": "Chemicals", "product": "Bentonite", "quantity": "750 Kg", "amount_paid": 15000, "remaining": 3000, "payment_mode": "Credit", "status": "Partial"},
    ]
    added = False
    for s in samples:
        if not Vendor.query.filter_by(vendor_name=s["vendor_name"]).first():
            db.session.add(Vendor(**s))
            added = True
    if added:
        db.session.commit()
    return added


with app.app_context():
    try:
        if _startup_schema_current():
            raise _SchemaCurrent()
        db.create_all()
        _migrate_purchase_details()
        _migrate_purchase_credit()
        _migrate_slip_blobs()
        _migrate_weighment_files()
        _migrate_product_last_update()
        _migrate_third_party_last_update()
        _migrate_raw_material_sales()
        _migrate_settings_contact()
        _migrate_backup_settings()
        _migrate_remarks_notes()
        _migrate_resource_stock_flows()
        _migrate_third_party_image()
        _migrate_product_image_blobs()
        _migrate_vendor_products()
        _migrate_drop_resource_expiry()
        _migrate_production_machine()
        _migrate_active_shift()
        _migrate_change_version()
        _migrate_admin_change_version()
        _migrate_active_user()
        _migrate_user_password_plain()
        _seed_default_admin()
        if not os.environ.get("SAVI_NO_SEED"):
            seed_database(db)
            _ensure_standard_products()
            _sync_resource_register_items()
            _seed_material_daily()
            _seed_production_sample()
            _seed_production_sample_new()
            _roll_daily_forward()
        _migrate_vendor_gstin()
        _migrate_vendor_category()
        _migrate_third_party()
        _migrate_formula_sheet_product()
        _migrate_pm_batches()
        _migrate_material_daily_supervisor()
        _migrate_material_daily_opening()
        _migrate_material_daily_shift()
        _migrate_material_daily_bags()
        _migrate_maintenance_mode()
        if Vendor.query.count() == 0:
            for p in Purchase.query.order_by(Purchase.id).all():
                db.session.add(Vendor(
                    vendor_name=p.vendor_name, address=p.address or "",
                    vendor_phone=p.vendor_phone or "", date_display=p.date_display,
                    category=_vendor_category_for(p.product),
                    product=p.product, quantity=p.quantity, amount_paid=p.amount_paid,
                    remaining=p.remaining, payment_mode=p.payment_mode, status=p.status))
            db.session.commit()
        if not os.environ.get("SAVI_NO_SEED"):
            _seed_vendor_samples()
        _backfill_vendor_products()
        _stamp_schema_current()


    except _SchemaCurrent:
        pass
    except Exception as _startup_exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        app.logger.warning("Startup schema sync skipped: %s", _startup_exc)
# ---------- helpers ----------
def _ok(payload=None, **extra):
    body = {"success": True}
    if payload is not None:
        body.update(payload)
    body.update(extra)
    return jsonify(body)


def _fail(message, code=400):
    return jsonify({"success": False, "error": message}), code


def _parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_settings() -> Setting:
    settings = Setting.query.order_by(Setting.id).first()
    if not settings:
        settings = Setting()
        db.session.add(settings)
        db.session.commit()
    return settings


# ---------- pages (same template contexts as before) ----------
@app.route('/')
@app.route('/dashboard')
def dashboard():
    products = Product.query.order_by(Product.id).all()
    third_party = ThirdPartyItem.query.order_by(ThirdPartyItem.id).all()
    return render_template('dashboard.html',
        products=[p.to_dict() for p in products],
        third_party=[t.to_dict() for t in third_party],
        resource_groups=_resource_overview_groups(),
        total_products=Product.query.count(),
        low_stock=Resource.query.filter(Resource.status == 'Critical').count(),
        today_label=datetime.now().strftime('%B %d, %Y').replace(' 0', ' '),
        active_page='dashboard'
    )


def _resource_overview_groups():
    groups = []
    for group in RESOURCE_CATEGORIES.keys():
        items = Resource.query.filter_by(group=group).order_by(Resource.closing).all()
        dicts = [r.to_dict() for r in items]
        maxc = max([d["closing"] for d in dicts] + [1])
        groups.append({
            "group": group,
            "rows": dicts,
            "max_closing": maxc,
            "total": len(dicts),
            "good": sum(1 for d in dicts if d["status"] == "Good"),
            "low": sum(1 for d in dicts if d["status"] == "Low Stock"),
            "critical": sum(1 for d in dicts if d["status"] == "Critical"),
        })
    return groups


@app.route('/resource-maintenance/<path:category>')
def resource_maintenance(category):
    if category not in RESOURCE_CATEGORIES:
        return redirect(url_for('resource_maintenance', category='Raw Materials'))
    resources = Resource.query.filter_by(group=category).order_by(Resource.id).all()
    # Keep the displayed Status synchronized with the current stock even for
    # resources created/updated by older database versions. Do not alter the
    # Last Update timestamp merely by opening the page.
    changed = False
    for r in resources:
        expected_status = compute_resource_status(r.closing or 0, r.min_stock or 250)
        if r.status != expected_status:
            r.status = expected_status
            changed = True
    if changed:
        db.session.commit()
    # group -> [names] map so saveNewMaterial() can tell a genuinely new item
    # from an existing one (existing items must never be re-created with a
    # fresh stock - that wiped e.g. 50000 kg rows when an old name was typed
    # into the Add New Material modal).
    resource_names = {}
    for _g, _n in Resource.query.with_entities(Resource.group, Resource.name).all():
        resource_names.setdefault(_g, []).append(_n)
    return render_template('resource_maintenance.html',
        resource_category=category,
        resources=[r.to_dict() for r in resources],
        categories=list(RESOURCE_CATEGORIES.keys()),
        resource_names=resource_names,
        active_page='resource_maintenance'
    )


@app.route('/resource-maintenance/low-stock')
def resource_low_stock():
    resources = Resource.query.filter(Resource.status == 'Critical').order_by(Resource.group, Resource.id).all()
    return render_template('resource_low_stock.html',
        resources=[r.to_dict() | {'group': r.group} for r in resources],
        active_page='resource_maintenance',
        resource_category='Stock Alerts'
    )


@app.route('/raw-materials')
def raw_materials():
    materials = RawMaterial.query.order_by(RawMaterial.id).all()
    return render_template('raw_materials.html',
        materials=[m.to_dict() for m in materials],
        active_page='raw_materials'
    )


@app.route('/finished-goods')
def finished_goods():
    products = Product.query.order_by(Product.id).all()
    return render_template('finished_goods.html',
        inventory_title='Finished Goods',
        products=[p.to_dict() for p in products],
        active_page='finished_goods'
    )


@app.route('/inventory-product/<path:product_name>')
def inventory_product(product_name):
    selected_shift = _normalize_shift(request.args.get('shift') or _active_shift_value())
    return _render_inventory_product(product_name, request.args.get('date', ''), selected_shift)


@app.route('/inventory-product/<path:product_name>/<shift>')
def inventory_product_shift(product_name, shift):
    # The admin can switch between the Morning and Night shifts to review both;
    # the choice is remembered so the default Production Update view (sidebar
    # links, no ?shift=) keeps opening on the shift the admin last chose.
    _set_active_shift(_normalize_shift(shift))
    return _render_inventory_product(product_name, request.args.get('date', ''), _normalize_shift(shift))


def _render_inventory_product(product_name, date_arg='', selected_shift='morning'):
    from datetime import timedelta
    active_shift = _active_shift_value()
    selected_shift = _normalize_shift(selected_shift, active_shift)
    shift_locked = False
    product = Product.query.filter_by(name=product_name).first()
    is_third_party = False
    third_item = None
    if product:
        # Change #45: the seed catalog is the user's 21 products - every
        # seeded product keeps direct Production Update access (the old six
        # names were hardcoded here); custom products still need a saved
        # formula with machine quantities first.
        standard_names = {p["name"] for p in PRODUCTS}
        if product.name not in standard_names:
            has_formula = FormulaSheet.query.filter(
                FormulaSheet.product == product_name,
                or_(FormulaSheet.old_machine > 0, FormulaSheet.new_machine > 0)
            ).first()
            if not has_formula:
                return redirect(url_for('formulas', list=product_name))
    if not product:
        third_item = ThirdPartyItem.query.filter_by(name=product_name).first()
        if not third_item:
            return redirect(url_for('finished_goods'))
        # Do not create a Product row for 3rd-party products. They are shown
        # once in Production Update when a saved formula exists.
        has_formula = FormulaSheet.query.filter(
            FormulaSheet.product == product_name,
            or_(FormulaSheet.old_machine > 0, FormulaSheet.new_machine > 0)
        ).first()
        if not has_formula:
            return redirect(url_for('formulas', list=product_name))
        is_third_party = True
        product = {
            'id': 'tp-' + str(third_item.id), 'name': third_item.name,
            'category': '3rd Party Product', 'price': third_item.price or 0,
            'stock': third_item.quantity or 0, 'image': '', 'status': 'In Stock'
        }
    rows = ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).all()
    if is_third_party:
        import json as _json
        opening_map = {}
        for line in third_item.mat_lines():
            try:
                opening_map[str(line.get('material', '')).strip()] = float(line.get('opening', 0) or 0)
            except (TypeError, ValueError):
                opening_map[str(line.get('material', '')).strip()] = 0
        formula_rows = FormulaSheet.query.filter_by(product=product_name).order_by(FormulaSheet.id).all()
        # Formula materials become the production-update rows, using the
        # opening quantity entered for the 3rd-party item where available.
        rows = []
        for f in formula_rows:
            opening = opening_map.get(f.material, 0)
            old_batches = f.old_machine or 0
            new_batches = f.new_machine or 0
            # Use the same row interface as ProductMaterial so the existing
            # Production Update template can render 3rd-party products without
            # a separate page/layout.
            class ThirdPartyProductionRow:
                def __init__(self, formula_row, opening_qty, old_qty, new_qty):
                    self.id = formula_row.id
                    self.material = formula_row.material
                    self.unit = 'kg'
                    self.opening = opening_qty
                    self.consumed = 0
                    self.closing = opening_qty
                    self.batch = ''
                    self.status = 'Available'
                    self.old_batches = old_qty
                    self.new_batches = new_qty
                    self.old_machine_qty = old_qty
                    self.new_machine_qty = new_qty

                @property
                def total_batches(self):
                    return (self.old_batches or 0) + (self.new_batches or 0)

                @property
                def total_output(self):
                    return self.consumed * self.total_batches

                def to_dict(self):
                    return {
                        'id': self.id, 'material': self.material, 'unit': self.unit,
                        'opening': self.opening, 'consumed': self.consumed,
                        'closing': self.closing, 'batch': self.batch,
                        'status': self.status, 'old_batches': self.old_batches or 0,
                        'new_batches': self.new_batches or 0,
                        'total_batches': self.total_batches,
                        'total_output': self.total_output,
                    }

            rows.append(ThirdPartyProductionRow(f, opening, old_batches, new_batches))
    all_materials = sorted(
        m[0] for m in db.session.query(ProductMaterial.material).distinct().all())
    _roll_daily_forward(product_name)
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    selected_daily = (date_arg or today).strip() or today
    try:
        datetime.strptime(selected_daily, "%Y-%m-%d")
    except ValueError:
        selected_daily = yesterday
    if selected_daily > today:
        selected_daily = today
    daily_rows = _shift_daily_rows(product_name, selected_daily, selected_shift)
    supervisor_name = ''
    for _d in daily_rows:
        if getattr(_d, 'supervisor_name', None):
            supervisor_name = _d.supervisor_name or ''
            break
    supervisor_note = ''
    if not supervisor_name:
        # Item 8: nothing saved for this exact date+shift - surface the most
        # recent one so the chip never looks broken when an update exists.
        _ln, _ld, _ls = _latest_supervisor(product_name)
        if _ln:
            supervisor_name = _ln
            if _ld or _ls:
                supervisor_note = 'Latest update: %s - %s shift' % (
                    _prod_display_date(_ld) if _ld else 'unknown date',
                    'Night' if _ls == 'night' else 'Morning')
    # If the OTHER shift already saved an update for this date, expose it as a
    # one-click switch chip so the admin never misses the other shift's update
    # (the default view only ever shows one shift at a time).
    _other_shift = 'night' if selected_shift == 'morning' else 'morning'
    other_shift_update = None
    for _d in _shift_daily_rows(product_name, selected_daily, _other_shift):
        if getattr(_d, 'supervisor_name', None):
            other_shift_update = {'shift': _other_shift, 'supervisor': _d.supervisor_name}
            break
    # Production Summary is the read-only, per-shift view of the selected day's
    # production update. Stored shift rows carry the rolled-forward opening, so
    # the morning closing becomes the night opening, and the night closing
    # becomes the next morning's opening.
    res_closing = _ClosingMap()
    try:
        for res in Resource.query.all():
            res_closing.put(res.name, res.closing or 0)
    except Exception:
        res_closing = _ClosingMap()
    summary_rows, summary_old_bags, summary_new_bags = _compute_production_summary(
        product_name, selected_daily, selected_shift, rows, res_closing, today,
        is_third_party=is_third_party)
    summary_total_output = sum(r.get('total_output', 0) for r in summary_rows)
    typed_old = getattr(product, 'old_machine_bags', None)
    typed_new = getattr(product, 'new_machine_bags', None)
    has_stored_shift = bool(daily_rows)
    typed_old_value = _stored_whole_bag_value(typed_old)
    typed_new_value = _stored_whole_bag_value(typed_new)
    stored_old_bags = next((getattr(_d, 'old_bags', None) for _d in daily_rows
                            if getattr(_d, 'old_bags', None) is not None), None)
    stored_new_bags = next((getattr(_d, 'new_bags', None) for _d in daily_rows
                            if getattr(_d, 'new_bags', None) is not None), None)
    if stored_old_bags is not None:
        display_old_bags = int(stored_old_bags)
    elif has_stored_shift:
        display_old_bags = summary_old_bags
    else:
        display_old_bags = typed_old_value if typed_old is not None and typed_old_value is not None else summary_old_bags
    if stored_new_bags is not None:
        display_new_bags = int(stored_new_bags)
    elif has_stored_shift:
        display_new_bags = summary_new_bags
    else:
        display_new_bags = typed_new_value if typed_new is not None and typed_new_value is not None else summary_new_bags
    week_overview = []
    for k in range(8):
        day = (datetime.now() - timedelta(days=k)).strftime('%Y-%m-%d')
        day_rows = MaterialDaily.query.filter_by(product_name=product_name, date=day).all()
        week_overview.append({
            "date": day, "display": _prod_display_date(day),
            "count": len(day_rows),
            "consumed": sum(r.consumed for r in day_rows),
            "closing": sum(r.closing for r in day_rows),
        })
    product_dict = product if isinstance(product, dict) else product.to_dict()
    return render_template('product_materials.html',
        product=product_dict,
        raw_materials=[r.material for r in rows],
        all_materials=all_materials,
        material_stock=[{**r.to_dict(), 'opening': _shift_opening(
            product_name, r.material, selected_daily, selected_shift,
            res_closing.get(str(r.material).strip(), r.opening or 0))} for r in rows],
        today_str=today,
        yesterday_str=yesterday,
        selected_daily=selected_daily,
        selected_daily_display=_prod_display_date(selected_daily),
        selected_shift=selected_shift,
        shift_locked=shift_locked,
        active_shift=active_shift,
        other_shift_update=other_shift_update,
        supervisor_name=supervisor_name,
        supervisor_note=supervisor_note,
        summary_rows=summary_rows,
        summary_total_output=summary_total_output,
        summary_old_machine_bags=display_old_bags,
        summary_new_machine_bags=display_new_bags,
        summary_total_bags=display_old_bags + display_new_bags,
        week_overview=week_overview,
        active_page='inventory_product',
        active_product=product_name
    )


@app.route('/api/inventory-product/<path:product_name>')
def inventory_product_api(product_name):
    product = Product.query.filter_by(name=product_name).first()
    is_third_party = False
    if product:
        rows = ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).all()
    else:
        # 3rd-party products have no Product row - serve them from the formula
        # (same rows the page renders) so the 5s live poll does not 404.
        third_item = ThirdPartyItem.query.filter_by(name=product_name).first()
        if not third_item:
            return jsonify({'error': 'not found'}), 404
        is_third_party = True
        product = {
            'id': 'tp-' + str(third_item.id), 'name': third_item.name,
            'category': '3rd Party Product', 'price': third_item.price or 0,
            'stock': third_item.quantity or 0, 'image': '', 'status': 'In Stock'
        }
        rows = _third_party_production_rows(third_item, product_name)
    res_closing = _ClosingMap()
    try:
        for res in Resource.query.all():
            res_closing.put(res.name, res.closing or 0)
    except Exception:
        res_closing = _ClosingMap()
    today = datetime.now().strftime('%Y-%m-%d')
    sup_date = (request.args.get('date') or today).strip() or today
    try:
        datetime.strptime(sup_date, "%Y-%m-%d")
    except ValueError:
        sup_date = today
    if sup_date > today:
        sup_date = today
    selected_shift = _normalize_shift(request.args.get('shift') or _active_shift_value())
    summary_rows, summary_old_bags, summary_new_bags = _compute_production_summary(
        product_name, sup_date, selected_shift, rows, res_closing, today,
        is_third_party=is_third_party)
    shift_dailies = _shift_daily_rows(product_name, sup_date, selected_shift)
    has_stored_shift = bool(shift_dailies)
    typed_old = getattr(product, 'old_machine_bags', None)
    typed_new = getattr(product, 'new_machine_bags', None)
    typed_old_value = _stored_whole_bag_value(typed_old)
    typed_new_value = _stored_whole_bag_value(typed_new)
    stored_old_bags = next((getattr(_d, 'old_bags', None) for _d in shift_dailies
                            if getattr(_d, 'old_bags', None) is not None), None)
    stored_new_bags = next((getattr(_d, 'new_bags', None) for _d in shift_dailies
                            if getattr(_d, 'new_bags', None) is not None), None)
    if stored_old_bags is not None:
        display_old = int(stored_old_bags)
    elif has_stored_shift:
        display_old = summary_old_bags
    else:
        display_old = typed_old_value if typed_old is not None and typed_old_value is not None else summary_old_bags
    if stored_new_bags is not None:
        display_new = int(stored_new_bags)
    elif has_stored_shift:
        display_new = summary_new_bags
    else:
        display_new = typed_new_value if typed_new is not None and typed_new_value is not None else summary_new_bags
    supervisor_name = ''
    for _d in shift_dailies:
        if getattr(_d, 'supervisor_name', None):
            supervisor_name = _d.supervisor_name or ''
            break
    supervisor_note = ''
    if not supervisor_name:
        # Item 8: same fallback the server render uses, so the 5s poll cannot
        # blank a supervisor that is only stored on an earlier date/other shift.
        _ln, _ld, _ls = _latest_supervisor(product_name)
        if _ln:
            supervisor_name = _ln
            if _ld or _ls:
                supervisor_note = 'Latest update: %s - %s shift' % (
                    _prod_display_date(_ld) if _ld else 'unknown date',
                    'Night' if _ls == 'night' else 'Morning')
    return jsonify({
        'summary_old_machine_bags': display_old,
        'summary_new_machine_bags': display_new,
        'summary_total_bags': display_old + display_new,
        'supervisor_name': supervisor_name,
        'supervisor_note': supervisor_note,
        'supervisor_date': sup_date,
        'summary_rows': summary_rows,
        'today_str': today,
        'server_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    })


@app.route('/medicines')
def medicines():
    products = Product.query.order_by(Product.id).all()
    return render_template('finished_goods.html',
        inventory_title='Medicines',
        products=[p.to_dict() for p in products],
        active_page='medicines'
    )


@app.route('/module/<module_name>')
def module_placeholder(module_name):
    replacements = {
        'purchase-records': 'purchase_records',
        'settings': 'settings',
        'logout': 'logout',
    }
    replacement = replacements.get(module_name)
    if replacement:
        return redirect(url_for(replacement))
    module_details = {
        'purchase-records': ('Purchase Records', 'Purchase record screens are reserved for the backend integration.'),
        'settings': ('Settings', 'Settings and user permissions are reserved for the backend integration.'),
        'logout': ('Logout', 'Authentication is not connected in this demo build, so no session was ended.'),
    }
    title, description = module_details.get(module_name, ('Demo section', 'This demo section is not configured.'))
    return render_template('module_placeholder.html',
        module_title=title, module_description=description, active_page=module_name)


@app.route('/production')
def production():
    today = datetime.now().strftime('%Y-%m-%d')
    productions = ProductionEntry.query.order_by(ProductionEntry.date.desc(), ProductionEntry.id.desc()).all()
    prods = Product.query.order_by(Product.id).all()
    prod_dicts = [p.to_dict() for p in prods]
    prod_names = [p["name"] for p in prod_dicts]
    selected_date = request.args.get('date', today).strip() or today
    if not _prod_valid_date(selected_date, default_today=False):
        selected_date = today
    consumption_logs = [l.to_dict() for l in ConsumptionLog.query.order_by(
        ConsumptionLog.date.desc(), ConsumptionLog.id.desc()).limit(200).all()]
    overall_rows = []
    old_count = 0
    new_count = 0
    for pd in prod_dicts:
        entries = [p for p in productions if p.product == pd["name"]]
        old_e = [p for p in entries if "old" in (p.machine or "").lower()]
        new_e = [p for p in entries if "new" in (p.machine or "").lower()]
        consumed = sum(p.consumed or 0 for p in entries)
        overall_rows.append({
            "name": pd["name"], "opening": pd["stock"], "consumed": consumed,
            "old_batches": len(old_e),
            "new_batches": len(new_e),
            "batches": len(entries),
            "output": sum(p.quantity - (p.consumed or 0) for p in entries),
        })
        old_count += len(old_e)
        new_count += len(new_e)
    old_entries = [p.to_dict() for p in productions if "old" in (p.machine or "").lower()]
    new_entries = [p.to_dict() for p in productions if "new" in (p.machine or "").lower()]
    machine_entries = [e.to_dict() for e in MachineProductionEntry.query.order_by(
        MachineProductionEntry.id.desc()).all()]
    old_machine_entries = [e for e in machine_entries if "old" in (e["machine"] or "").lower()]
    new_machine_entries = [e for e in machine_entries if "new" in (e["machine"] or "").lower()]
    return render_template('production.html',
        consumption_logs=consumption_logs,
        consumption_total=sum(l["quantity"] for l in consumption_logs),
        overall_rows=overall_rows,
        old_batches_total=old_count,
        new_batches_total=new_count,
        old_entries=old_entries,
        new_entries=new_entries,
        old_machine_entries=old_machine_entries,
        new_machine_entries=new_machine_entries,
        productions=[p.to_dict() for p in productions],
        products=prod_dicts,
        product_names=prod_names,
        now_date=today,
        selected_date=selected_date,
        selected_display=_prod_display_date(selected_date),
        history_days=_prod_history_days(productions),
        history_dates=sorted(set(p.date for p in productions), reverse=True),
        monthly_rows=_prod_monthly_rows(productions, prod_names),
        today_production=sum(p.quantity for p in productions if p.date == today),
        month_production=sum(p.quantity for p in productions if p.date[:7] == today[:7]),
        products_produced=len(set(p.product for p in productions)),
        pending_entries=sum(1 for p in productions if p.status == 'In Progress'),
        active_page='production'
    )


@app.route('/purchase-records')
def purchase_records():
    status_filter = request.args.get('status', 'all')
    if status_filter not in {'all', 'Paid', 'Partial', 'Unpaid'}:
        status_filter = 'all'
    query = Purchase.query.order_by(Purchase.id.desc())
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    purchases = query.all()
    vendor_list = [{"name": v.vendor_name, "phone": v.vendor_phone or ""}
                   for v in Vendor.query.order_by(Vendor.vendor_name).all()]
    return render_template('purchase_records.html',
        purchases=[p.to_dict() for p in purchases], vendor_list=vendor_list,
        status_filter=status_filter, active_page='purchase_maintenance')


@app.route('/driver-details')
def admin_driver_details():
    drivers = DriverDetails.query.order_by(DriverDetails.id.desc()).all()
    return render_template('driver_details_admin.html',
        drivers=[d.to_dict() for d in drivers],
        active_page='admin_driver_details')


@app.route('/driver-details/slip/<path:filename>')
def admin_driver_slip(filename):
    return _send_stored_file('driver_slips', filename, DRIVER_SLIP_DIR)


@app.route('/sales-invoices/<path:filename>')
def sales_invoice(filename):
    return _send_stored_file('sales_slips', filename, SALES_SLIP_DIR)


@app.route('/raw-materials-sales')
def raw_materials_sales():
    sales = RawMaterialSale.query.order_by(RawMaterialSale.id.desc()).all()
    materials = Resource.query.filter(Resource.group == 'Raw Materials').order_by(Resource.name).all()
    return render_template('raw_materials_sales.html',
                           sales=[s.to_dict() for s in sales],
                           materials=[m.to_dict() for m in materials],
                           active_page='raw_materials_sales')


# ---------- Change #56 (2026-10-09): multi-product Add Sales (read side) ----------
def _ps_items(product, quantity, items_json):
    """[(product, qty)] for a STORED product sale. `items` is the JSON list
    [{"product": name, "qty": bags}] saved by the user portal; empty/NULL =
    legacy single-product row, whose whole sale is product + quantity."""
    raw = str(items_json or '').strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, list) and parsed:
            order, agg = [], {}
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('product', '') or '').strip()
                try:
                    qty = int(float(str(entry.get('qty', entry.get('quantity'))).strip().replace(',', '')))
                except (TypeError, ValueError):
                    qty = 0
                if not name or qty <= 0:
                    continue
                if name not in agg:
                    order.append(name)
                    agg[name] = 0
                agg[name] += qty
            if order:
                return [(name, agg[name]) for name in order]
    name = str(product or '').strip()
    if not name:
        return []
    try:
        qty = int(quantity or 0)
    except (TypeError, ValueError):
        qty = 0
    return [(name, qty)]


def _ps_amount_fallback(items, price_by_name):
    """Change #52 amount_to_pay fallback (qty x price) summed over every item."""
    return int(round(sum(qty * price_by_name.get(name, 0) for name, qty in items)))


def _ps_render_amounts(d, price_by_name):
    """Change #60: Total Amount cell = the typed total_amount (qty x price
    fallback for legacy/blank rows), the Globle Discount display (amount wins
    over percentage), and the NEW Amount to be paid = Total Amount - discount
    (kept >= 0). Sets `total_amount_value`, `discount_display` and
    `amount_to_pay` on `d` (the old `amount_to_pay` key now carries the
    discounted value - with no discount it is unchanged)."""
    _ta = d.get('total_amount')
    total = (int(_ta) if _ta is not None else
             _ps_amount_fallback(_ps_items(d.get('product'), d.get('quantity'),
                                           d.get('items')), price_by_name))
    amt = d.get('discount_amount')
    pct = d.get('discount_percent')
    if amt is not None:
        discount = float(amt)
        d['discount_display'] = '₹%g' % discount
    elif pct is not None:
        discount = total * float(pct) / 100.0
        d['discount_display'] = '%g%%' % float(pct)
    else:
        discount = 0.0
        d['discount_display'] = ''
    pay = round(total - discount, 2)
    d['total_amount_value'] = total
    d['amount_to_pay'] = int(pay) if float(pay).is_integer() else pay
    return d


@app.route('/product-sales')
def product_sales():
    sales = ProductSale.query.order_by(ProductSale.id.desc()).all()
    products = Product.query.order_by(Product.name).all()
    product_rows = []
    price_by_name = {}
    for prod in products:
        row = prod.to_dict()
        row['status'] = compute_product_status(prod.stock or 0)
        price_by_name[row.get('name')] = float(row.get('price') or 0)
        product_rows.append(row)
    sales_rows = []
    for s in sales:
        d = s.to_dict()
        # Item 2 (2026-10-05): "Amount to be paid (Rs)" was Quantity x price.
        # Change #52 (2026-10-07): the user types it in Add Sales (stored
        # total_amount, NULL = never typed); qty x price is the fallback for
        # legacy rows and rows where it was left empty.
        # Change #60: that typed value is now the **Total Amount** column and
        # `amount_to_pay` carries the discounted **Amount to be paid**
        # (Total - Globle Discount, computed at render time).
        d = _ps_render_amounts(d, price_by_name)
        sales_rows.append(d)
    return render_template('product_sales.html',
                           sales=sales_rows,
                           products=product_rows,
                           active_page='product_sales')


@app.route('/remarks')
def remarks():
    # Change #55 (2026-10-09): VIEW-ONLY. Remarks are entered on the user
    # portal; the admin has no POST/DELETE routes here (tests assert 404/405).
    rows = SalesRemark.query.order_by(SalesRemark.id.desc()).all()
    return render_template('remarks.html',
                           remarks=[r.to_dict() for r in rows],
                           active_page='remarks')


@app.route('/production-stock')
def production_stock():
    products = Product.query.order_by(Product.name).all()
    third_items = ThirdPartyItem.query.order_by(ThirdPartyItem.name).all()
    product_rows = []
    for prod in products:
        row = prod.to_dict()
        row['status'] = compute_product_status(prod.stock or 0)
        product_rows.append(row)
    third_rows = []
    for item in third_items:
        third_rows.append({
            'id': item.id, 'name': item.name, 'category': item.category or '',
            'stock': int(item.quantity or 0), 'unit': item.unit or 'kg',
            'price': item.price or 0,
            'status': compute_resource_status(int(item.quantity or 0)),
            'last_update': item.last_update or item.created or '',
        })
    return render_template('production_stock.html',
                           products=product_rows, third_items=third_rows,
                           active_page='production_stock')


@app.route('/admin-config')

@app.route('/reports')
def reports():
    # Reports period/year starts from fresh launch: September 2026.
    # Only months/years from Sep 2026 up to current month are selectable.
    # Data is stored in its month; next month previous months/years appear automatically.
    FRESH_YEAR, FRESH_MONTH = 2026, 9
    now = datetime.now()
    cur_year, cur_month = now.year, now.month
    # Build valid year/month set from fresh start to current
    valid_years = list(range(FRESH_YEAR, cur_year + 1))

    def valid_months_for_year(y):
        if y < FRESH_YEAR or y > cur_year:
            return []
        if y == FRESH_YEAR and cur_year == FRESH_YEAR:
            return list(range(FRESH_MONTH, cur_month + 1))
        if y == FRESH_YEAR:
            return list(range(FRESH_MONTH, 13))
        if y == cur_year:
            return list(range(1, cur_month + 1))
        return list(range(1, 13))

    selected_month = request.args.get('month', now.strftime('%m'))
    selected_year = request.args.get('year', now.strftime('%Y'))
    # Validate year
    try:
        sy = int(selected_year)
    except:
        sy = cur_year
    if sy not in valid_years:
        sy = cur_year
        selected_year = str(sy)
    # Validate month within valid months for that year
    try:
        sm = int(selected_month)
    except:
        sm = cur_month if sy == cur_year else (FRESH_MONTH if sy == FRESH_YEAR else 1)
    valid_m = valid_months_for_year(sy)
    if sm not in valid_m:
        # default to latest valid month for that year, or current month
        sm = valid_m[-1] if valid_m else cur_month
        selected_month = f'{sm:02d}'
    else:
        selected_month = f'{sm:02d}'
        selected_year = str(sy)

    report_start = request.args.get('start', '')
    report_end = request.args.get('end', '')

    products = Product.query.order_by(Product.id).all()

    def inv_status(stock):
        if stock <= 6:
            return 'Critical'
        if stock <= 30:
            return 'Low'
        return 'Healthy'

    inventory_all = []
    for p in products:
        batches = ProductionEntry.query.filter_by(product=p.name).count()
        inventory_all.append({
            "material": p.name, "category": p.category, "on_hand": p.stock,
            "unit": "unit", "batches": batches, "minimum": 30,
            "status": inv_status(p.stock), "value": p.stock * (p.price or 0),
        })
    inventory_rows = inventory_all

    total_stock_value = sum(r['value'] for r in inventory_rows)
    total_materials = len(inventory_rows)
    total_batches = sum(r['batches'] for r in inventory_rows)
    need_attention = sum(1 for r in inventory_rows if r['status'] in ('Critical', 'Low'))
    largest_holdings = sorted(inventory_rows, key=lambda r: r['on_hand'], reverse=True)

    # Filter purchases and production by selected month/year (stored data is per month)
    def _purchase_in_selected(p):
        try:
            # date_display like "24 May 2026" or "24 Sep 2026"
            dt = datetime.strptime(p.date_display.strip(), "%d %b %Y")
            return dt.strftime("%m") == selected_month and dt.strftime("%Y") == selected_year
        except:
            # fallback: try to match year/month substrings
            return selected_year in p.date_display and datetime(2000, int(selected_month), 1).strftime("%b") in p.date_display

    purchases_all = Purchase.query.order_by(Purchase.id.desc()).all()
    purchases = [p for p in purchases_all if _purchase_in_selected(p)] if purchases_all else []
    purchase_rows = [{
        'invoice': p.v_number, 'supplier': p.vendor_name, 'date': p.date_display,
        'product': p.product, 'quantity': p.quantity, 'amount': p.amount_paid,
        'remaining': p.remaining, 'payment_mode': p.payment_mode, 'status': p.status,
    } for p in purchases]
    purchase_total = sum(p['amount'] for p in purchase_rows)
    outstanding_total = sum(p['remaining'] for p in purchase_rows)

    resources = Resource.query.order_by(Resource.id).all()
    analysis_all = [{
        "material": r.name, "category": r.group, "on_hand": r.closing,
        "unit": r.unit, "minimum": r.min_stock, "status": r.status,
        "last_update": r.last_update,
    } for r in resources]
    materials = analysis_all
    alerts = [r for r in analysis_all if r['status'] in ('Low Stock', 'Critical')]

    # Production report: filter by selected month/year (date is YYYY-MM-DD)
    batches = []
    for prod in ProductionEntry.query.order_by(ProductionEntry.date.desc(), ProductionEntry.id.desc()).all():
        try:
            # prod.date like "2026-09-24"
            if prod.date and len(prod.date) >= 7:
                y, m = prod.date.split("-")[0], prod.date.split("-")[1]
                if y != selected_year or m != selected_month:
                    continue
        except:
            pass
        base = prod.to_dict()
        for shift in ('Morning', 'Night'):
            batches.append({
                'product': base['product'], 'date': base['date'],
                'shift': shift, 'quantity': base['quantity']
            })

    # Report months: only months from fresh start (Sep 2026) to now, per selected year
    report_months = [
        {'value': f'{i:02d}', 'label': datetime(2000, i, 1).strftime('%B')}
        for i in valid_m
    ]
    report_years = [
        {'value': str(y), 'label': str(y)}
        for y in valid_years
    ]

    return render_template('reports.html',
        inventory_rows=inventory_rows, largest_holdings=largest_holdings,
        total_stock_value=total_stock_value, total_materials=total_materials,
        total_batches=total_batches, need_attention=need_attention,
        purchase_rows=purchase_rows, purchase_total=purchase_total,
        outstanding_total=outstanding_total, materials=materials, alerts=alerts,
        batches=batches, report_months=report_months, selected_month=selected_month,
        report_years=report_years, selected_year=selected_year,
        report_start=report_start, report_end=report_end, active_page='reports'
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    record = _get_settings()
    saved = False
    if request.method == 'POST':
        record.company_name = request.form.get('company_name', '').strip() or record.company_name
        record.location = request.form.get('location', '').strip() or record.location
        record.currency_symbol = request.form.get('currency_symbol', '').strip() or '₹'
        record.allocation_rule = request.form.get('allocation_rule', 'FIFO')
        record.low_stock_notifications = 'low_stock_notifications' in request.form
        record.daily_digest = 'daily_digest' in request.form
        record.maintenance_mode = 'maintenance_mode' in request.form
        record.maintenance_message = request.form.get('maintenance_message', '').strip()
        if 'admin_contact_phone' in request.form:
            digits = re.sub(r'\D', '', request.form.get('admin_contact_phone', '') or '')
            if not digits:
                record.admin_contact_phone = ''
            elif len(digits) == 10:
                record.admin_contact_phone = digits
            # 1-9 or 11+ digits: keep the previous value (client caps at 10)
        record.backup_enabled = 'backup_enabled' in request.form
        if 'backup_local_dir' in request.form:
            record.backup_local_dir = request.form.get('backup_local_dir', '').strip()[:500]
        if 'backup_drive_dir' in request.form:
            record.backup_drive_dir = request.form.get('backup_drive_dir', '').strip()[:500]
        if 'backup_keep_days' in request.form:
            try:
                keep_days = int(request.form.get('backup_keep_days') or 30)
            except (TypeError, ValueError):
                keep_days = 30
            record.backup_keep_days = max(1, min(keep_days, 3650))
        db.session.commit()
        saved = True
    return render_template('settings.html', settings=record.to_dict(), saved=saved,
                           active_page='settings',
                           default_backup_dir=backup_service.default_backup_dir())


# ---------- Authentication ----------
_AUTH_OPEN_ENDPOINTS = {'login', 'static', 'api_health', 'api_auth_state',
                        'api_forgot_send', 'api_forgot_verify'}


@app.before_request
def _require_admin_login():
    """Block the whole admin panel until an admin account is logged in."""
    endpoint = request.endpoint or ''
    if endpoint in _AUTH_OPEN_ENDPOINTS or endpoint.startswith('static_'):
        return None
    if session.get('savi_admin_id'):
        # Change #42: absolute session lifetime (12h) - a session cookie that
        # outlives the working day is a liability on shared/lost machines.
        # Sessions stamped before this change (no savi_auth_at) just start
        # their clock now instead of being kicked out.
        auth_at = session.get('savi_auth_at')
        if auth_at is None:
            session['savi_auth_at'] = time.time()
            return None
        if time.time() - float(auth_at) > 43200:
            session.clear()
        else:
            return None
    if request.path.startswith('/api/'):
        return jsonify({"success": False, "error": "Authentication required"}), 401
    return redirect(url_for('login'))


@app.context_processor
def inject_admin_profile():
    info = {
        "current_user_name": session.get('savi_admin_name', ''),
        "current_user_role": session.get('savi_admin_role', ''),
        "current_user_login": "",
        "current_user_phone": "",
        "current_user_created": "",
        "profile_users": [],
        "active_user": {"id": 0, "full_name": "", "login_id": "", "phone": "", "shift": "", "at": ""},
    }
    uid = session.get('savi_admin_id')
    if uid:
        try:
            row = db.session.get(User, uid)
            if row:
                info.update({
                    "current_user_name": row.full_name,
                    "current_user_role": row.role,
                    "current_user_login": row.login_id,
                    "current_user_phone": row.phone or "",
                    "current_user_created": row.created or "",
                })
            info["profile_users"] = [u.to_dict() for u in User.query.order_by(User.id).all()]
            info["active_user"] = _active_user_info()
        except Exception:
            pass
    return info


ADMIN_BLOCK_MESSAGE = ('Access blocked. Only 2 admin members are allowed to log in. '
                       'If you are an admin, call or connect with a person you know and log in '
                       'with the Login ID and Password you were given.')

# ---------- Change #42: login brute-force throttle ----------
# In-memory sliding window (no schema change): any restart/deploys clears it,
# which only ever favours the honest user. Battery suites run one process per
# suite, so the counters can never bleed between test files either.
_LOGIN_FAILS = {}   # "<ip>|<login_id>" -> [failure epochs]
_LOGIN_LOCKS = {}   # "<ip>|<login_id>" (or "<ip>|*") -> lock-until epoch
LOGIN_FAIL_LIMIT = 10        # failed logins per (ip, login_id) per window
LOGIN_FAIL_WINDOW = 600      # 10-minute sliding window, seconds
LOGIN_IP_FAIL_LIMIT = 60     # all ids from one ip per window (stops id stuffing)
LOGIN_LOCK_SECONDS = 600     # lock duration once tripped


def _login_client_ip():
    """Best-effort client address for the throttle. Behind Vercel's proxy the
    first X-Forwarded-For entry is the visitor; anywhere else (local/LAN) the
    socket address is the truth and cannot be spoofed with a fake header."""
    if os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV'):
        xff = request.headers.get('X-Forwarded-For', '')
        first = xff.split(',')[0].strip() if xff else ''
        if first:
            return first
    return request.remote_addr or 'unknown'


def _login_prune(hits, now):
    cutoff = now - LOGIN_FAIL_WINDOW
    return [t for t in hits if t > cutoff]


def _login_lock_remaining(ip, login_id):
    """Seconds left on the lock for this key (per-id, then per-ip), 0 when free."""
    now = time.time()
    for key in ('%s|%s' % (ip, login_id), '%s|*' % ip):
        until = _LOGIN_LOCKS.get(key)
        if until is None:
            continue
        if until > now:
            return until - now
        _LOGIN_LOCKS.pop(key, None)
    return 0


def _login_record_failure(ip, login_id):
    """Count one failed login and trip the per-key / per-IP locks when the
    sliding window fills up."""
    now = time.time()
    key = '%s|%s' % (ip, login_id)
    hits = _login_prune(_LOGIN_FAILS.get(key, []), now) + [now]
    _LOGIN_FAILS[key] = hits
    ip_key = '%s|*' % ip
    ip_hits = _login_prune(_LOGIN_FAILS.get(ip_key, []), now) + [now]
    _LOGIN_FAILS[ip_key] = ip_hits
    if len(hits) >= LOGIN_FAIL_LIMIT:
        _LOGIN_LOCKS[key] = now + LOGIN_LOCK_SECONDS
    if len(ip_hits) >= LOGIN_IP_FAIL_LIMIT:
        _LOGIN_LOCKS[ip_key] = now + LOGIN_LOCK_SECONDS


def _login_clear_failures(ip, login_id):
    """A successful login wipes that key's history (fat-fingered retries are
    normal; only sustained failure ever locks). The per-IP bucket is left to
    expire by itself so one valid account cannot reset another id's clock."""
    _LOGIN_FAILS.pop('%s|%s' % (ip, login_id), None)


def _login_throttled_response(login_id):
    """Generic 429 for a locked key - never reveals whether the account exists."""
    left = _login_lock_remaining(_login_client_ip(), login_id)
    mins = max(1, int(left + 59) // 60)
    msg = ('Too many failed attempts. Please wait about %d minute(s) and try '
           'again, or contact the site owner.') % mins
    return _no_store(make_response(render_template(
        'login.html', error=msg, notice='', contact_phone=_admin_contact_phone(),
        is_user=False), 429))


def _no_store(resp):
    """Keep login/OTP pages out of the back/forward cache (Back button fix)."""
    if not hasattr(resp, 'headers'):
        resp = make_response(resp)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


DEFAULT_ADMIN_CONTACT_PHONE = '9980509508'


def _admin_contact_phone():
    """Phone shown on the login page's Forgot-Password fallback (Call /
    WhatsApp) when email OTP is not configured. Admin Settings overrides
    the default from Change #21."""
    try:
        row = Setting.query.first()
        phone = (getattr(row, 'admin_contact_phone', '') or '').strip()
    except Exception:
        phone = ''
    return phone or DEFAULT_ADMIN_CONTACT_PHONE


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('savi_admin_id'):
        return _no_store(redirect(url_for('dashboard')))
    error = ''
    if request.method == 'POST':
        login_id = request.form.get('login_id', '').strip()
        password = request.form.get('password', '')
        _ip = _login_client_ip()
        if _login_lock_remaining(_ip, login_id) > 0:
            return _login_throttled_response(login_id)
        if not re.fullmatch(r'\d{6}', login_id):
            session.clear()
            _login_record_failure(_ip, login_id)
            error = 'Login ID must be exactly 6 digits (numbers only).'
        else:
            user = User.query.filter_by(login_id=login_id).first()
            allowed_ids = [r.id for r in User.query.filter_by(role='admin').order_by(User.id).limit(2)]
            if (not user or user.role != 'admin' or user.id not in allowed_ids
                    or not check_password_hash(user.password, password)):
                session.clear()
                _login_record_failure(_ip, login_id)
                error = ADMIN_BLOCK_MESSAGE
            else:
                _login_clear_failures(_ip, login_id)
                # Change #42: clear any pre-login cookie state (session fixation)
                # and stamp the absolute session lifetime clock.
                session.clear()
                session['savi_admin_id'] = user.id
                session['savi_admin_name'] = user.full_name
                session['savi_admin_role'] = user.role
                session['savi_auth_at'] = time.time()
                return _no_store(redirect(url_for('dashboard')))
    notice = ('Password changed. Sign in with your new password.'
              if request.args.get('reset') == '1' else '')
    return _no_store(render_template('login.html', error=error, notice=notice,
                                     contact_phone=_admin_contact_phone(),
                                     is_user=False))


@app.route('/api/auth-state')
def api_auth_state():
    """Public: is this browser already logged in? Used by the login page's
    Back-button handler to bounce straight back to the dashboard."""
    return _ok({"logged_in": bool(session.get('savi_admin_id'))})


# ---------- Forgot Password (email OTP) ----------
_RESET_TTL_MIN = 10


def _smtp_config():
    user = os.environ.get('SMTP_USER', '').strip()
    password = os.environ.get('SMTP_PASSWORD', '')
    host = os.environ.get('SMTP_HOST', '').strip()
    if not host:
        # Fall back to Gmail's SMTP server when a Gmail account + app password
        # are configured but no explicit host was given.
        if user and password:
            host = 'smtp.gmail.com'
        else:
            return None
    try:
        port = int(os.environ.get('SMTP_PORT', '587').strip() or 587)
    except ValueError:
        port = 587
    return {
        'host': host, 'port': port, 'user': user,
        'password': password,
        'from': os.environ.get('MAIL_FROM', '').strip() or user,
    }


DEFAULT_ADMIN_RESET_EMAIL = 'goutham.kh7@gmail.com'


def _reset_destination():
    """Where the admin password-reset OTP is emailed. Defaults to the owner's
    recovery inbox; override with the ADMIN_RESET_EMAIL env var."""
    return (os.environ.get('ADMIN_RESET_EMAIL', '').strip()
            or DEFAULT_ADMIN_RESET_EMAIL)


def _reset_email_ready():
    return bool(_smtp_config() and _reset_destination())


def _hash_reset_code(code):
    key = str(app.secret_key or 'savi-reset')
    return hashlib.sha256((key + '|' + str(code)).encode('utf-8')).hexdigest()


def _send_reset_email(to_addr, code):
    cfg = _smtp_config()
    if not cfg:
        raise RuntimeError('smtp-not-configured')
    if not to_addr:
        raise RuntimeError('no-destination')
    msg = EmailMessage()
    msg['Subject'] = 'SAVI Nutrition - Password Reset Code'
    msg['From'] = cfg['from']
    msg['To'] = to_addr
    msg.set_content(
        'Your SAVI Nutrition admin password reset code is: %s\n\n'
        'It is valid for %d minutes. If you did not request this, '
        'you can ignore this email.' % (code, _RESET_TTL_MIN))
    if cfg['port'] == 465:
        with smtplib.SMTP_SSL(cfg['host'], cfg['port'], timeout=20,
                              context=ssl.create_default_context()) as s:
            if cfg['user']:
                s.login(cfg['user'], cfg['password'])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg['host'], cfg['port'], timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if cfg['user']:
                s.login(cfg['user'], cfg['password'])
            s.send_message(msg)


_ForgotGeneric = {"success": True,
                  "message": "If this admin ID exists, a 6-digit code has been "
                             "sent to its recovery email."}


@app.route('/api/auth/forgot/send', methods=['POST'])
def api_forgot_send():
    data = request.get_json(silent=True) or {}
    login_id = str(data.get('login_id', '')).strip()
    if not _reset_email_ready():
        return jsonify({"success": False,
                        "email_not_configured": True,
                        "error": "Email service is not configured on the server. "
                                 "Ask the site owner to set the SMTP settings."}), 503
    admin_ids = [r.login_id for r in User.query.filter_by(role='admin')
                 .order_by(User.id).limit(2)]
    if login_id not in admin_ids:
        return _ok(_ForgotGeneric)
    cutoff = datetime.now() - timedelta(minutes=_RESET_TTL_MIN)
    recent = PasswordReset.query.filter(
        PasswordReset.login_id == login_id,
        PasswordReset.created_at >= cutoff).count()
    if recent >= 3:
        return _ok(_ForgotGeneric)
    code = '%06d' % secrets.randbelow(1000000)
    row = PasswordReset(login_id=login_id, code_hash=_hash_reset_code(code),
                        created_at=datetime.now(), attempts=0, used=False)
    db.session.add(row)
    db.session.commit()
    try:
        _send_reset_email(_reset_destination(), code)
    except Exception:
        try:
            db.session.delete(row)
            db.session.commit()
        except Exception:
            db.session.rollback()
        return jsonify({"success": False,
                        "email_not_configured": True,
                        "error": "Could not send the reset email. Please try "
                                 "again later."}), 503
    return _ok(_ForgotGeneric)


@app.route('/api/auth/forgot/verify', methods=['POST'])
def api_forgot_verify():
    data = request.get_json(silent=True) or {}
    login_id = str(data.get('login_id', '')).strip()
    code = str(data.get('code', '')).strip()
    new_password = str(data.get('new_password', ''))
    if len(new_password) < 6:
        return _fail("New password must be at least 6 characters")
    row = (PasswordReset.query.filter_by(login_id=login_id, used=False)
           .order_by(PasswordReset.id.desc()).first())
    if not row:
        return _fail("Invalid or expired code")
    if row.created_at < datetime.now() - timedelta(minutes=_RESET_TTL_MIN):
        row.used = True
        db.session.commit()
        return _fail("Code expired - request a new one")
    row.attempts = (row.attempts or 0) + 1
    if row.attempts > 5:
        row.used = True
        db.session.commit()
        return _fail("Too many attempts - request a new code")
    if not hmac.compare_digest(str(row.code_hash or ''),
                               _hash_reset_code(code)):
        db.session.commit()
        return _fail("Incorrect code")
    user = User.query.filter_by(login_id=login_id).first()
    allowed_ids = [r.id for r in User.query.filter_by(role='admin')
                   .order_by(User.id).limit(2)]
    if not user or user.role != 'admin' or user.id not in allowed_ids:
        db.session.commit()
        return _fail("Invalid or expired code")
    user.password = generate_password_hash(new_password)
    user.password_plain = new_password
    row.used = True
    db.session.commit()
    return _ok({"success": True})


@app.route('/users', methods=['GET', 'POST'])
def users():
    error = ''
    created = request.args.get('created')
    if request.method == 'POST':
        full_name = (request.form.get('full_name') or '').strip()
        login_id = (request.form.get('login_id') or '').strip()
        password = request.form.get('password') or ''
        # No shift at creation (2026-09-28): the user picks Morning/Night at
        # every login and that choice updates users.shift. Older pages that
        # still post a shift select keep working.
        shift = (request.form.get('shift') or '').strip().lower()
        if shift not in ('morning', 'night'):
            shift = ''
        if not full_name or not login_id:
            error = 'Name and Login ID are required.'
        elif not re.fullmatch(r'\d{6}', login_id):
            error = 'Login ID must be exactly 6 digits (numbers only).'
        elif not re.fullmatch(r'\d{6}', password):
            error = 'Password must be exactly 6 digits (numbers only).'
        elif User.query.filter_by(login_id=login_id).first():
            error = 'Login ID "%s" is already registered.' % login_id
        else:
            db.session.add(User(
                full_name=full_name, login_id=login_id, phone='',
                password=generate_password_hash(password),
                password_plain=password, shift=shift, role='user',
                created=datetime.now().strftime('%d %b %Y, %I:%M %p')))
            db.session.commit()
            return redirect(url_for('users', created=1))

    def _access_row(u):
        # The User Access tables show each password behind an eye toggle.
        # Added here only — User.to_dict() stays clean so no JSON/API route
        # (profile sidebar, change monitor, …) ever exposes password_plain.
        row = u.to_dict()
        row["password_plain"] = u.password_plain or ""
        return row

    admin_rows = User.query.filter_by(role='admin').order_by(User.id).all()
    portal_rows = User.query.filter_by(role='user').order_by(User.id).all()
    return render_template('users.html',
                           users=[_access_row(u) for u in admin_rows],
                           portal_users=[_access_row(u) for u in portal_rows],
                           error=error, created=created, active_page='users')


@app.route('/users/<int:user_id>/delete', methods=['POST'])
def users_delete(user_id):
    row = db.session.get(User, user_id)
    if row and row.role == 'user':
        settings = _get_settings()
        if int(getattr(settings, 'active_user_id', 0) or 0) == int(row.id):
            settings.active_user_id = 0
            settings.active_user_name = ''
            settings.active_user_login = ''
            settings.active_user_phone = ''
            settings.active_user_shift = ''
            settings.active_user_at = ''
        db.session.delete(row)
        db.session.commit()
        return redirect(url_for('users', deleted=1))
    return redirect(url_for('users'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/api/health')
def api_health():
    storage = storage_status()
    ready = storage not in {'unconfigured', 'misconfigured'}
    payload = {
        "database": db.engine.dialect.name,
        "storage": storage,
        "products": Product.query.count(),
        "resources": Resource.query.count(),
        "purchases": Purchase.query.count(),
        "productions": ProductionEntry.query.count(),
    }
    return jsonify({"success": ready, **payload}), 200 if ready else 503


@app.route('/api/change-monitor')
def api_change_monitor():
    """Version token bumped by user-portal writes so the admin UI can prompt a refresh."""
    return _ok({"version": _change_version_value()})


# ---------- Maintenance mode ----------
@app.route('/api/maintenance-status')
def api_maintenance_status():
    settings = _get_settings()
    return _ok({"maintenance_mode": bool(settings.maintenance_mode),
                "maintenance_message": settings.maintenance_message or ""})


@app.route('/maintenance')
def maintenance_page():
    settings = _get_settings()
    if not settings.maintenance_mode:
        return redirect(url_for('dashboard'))
    return render_template('maintenance.html',
        settings=settings.to_dict(), active_page='maintenance')


# ---------- Products API ----------
@app.route('/api/products', methods=['GET'])
def api_products_list():
    return _ok({"products": [p.to_dict() for p in Product.query.order_by(Product.id).all()]})


@app.route('/api/products', methods=['POST'])
def api_products_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    if not name:
        return _fail("Product name is required")
    if Product.query.filter_by(name=name).first():
        return _fail("Product already exists", 409)
    stock = _parse_int(data.get('stock', 0))
    if stock < 0:
        return _fail("Stock cannot be negative")
    product = Product(
        name=name, category=str(data.get('category', 'Finished Goods')),
        price=_parse_int(data.get('price', 0)),
        stock=stock, image=str(data.get('image', '')),
        status=compute_product_status(stock),
        published=True,
    )
    db.session.add(product)
    db.session.flush()
    raw_materials = data.get('materials', [])
    if isinstance(raw_materials, list):
        prefix = ("".join(w[0] for w in name.split() if w).upper()[:3] or "PRD")
        seen = set()
        for idx, rm in enumerate(raw_materials, start=1):
            if not isinstance(rm, dict):
                continue
            mat = str(rm.get('material', '')).strip()
            if not mat or mat in seen:
                continue
            seen.add(mat)
            try:
                opening = int(rm.get('opening', 0) or 0)
            except (TypeError, ValueError):
                db.session.rollback()
                return _fail(f"Opening for '{mat}' must be a number")
            if opening < 0:
                db.session.rollback()
                return _fail(f"Opening for '{mat}' cannot be negative")
            batch = f"{prefix}-{idx:03d}"
            suffix = 2
            while ProductMaterial.query.filter_by(batch=batch).first():
                batch = f"{prefix}-{idx:03d}-{suffix}"
                suffix += 1
            db.session.add(ProductMaterial(
                product_id=product.id, product_name=name, material=mat,
                unit=str(rm.get('unit', 'kg')).strip() or 'kg',
                opening=opening, consumed=0, closing=opening, batch=batch,
                status="Low Stock" if opening < 150 else "Available"))
        _daily_backfill_all(product.id, name)
    # Item 1 (2026-10-01): a NEW product's formula page must start EMPTY - the
    # admin adds items and saves them (auto-created 0-qty rows used to show up
    # as a pre-filled formula the admin never entered). The sync below must
    # then be skipped too: syncing an empty sheet would DELETE the material
    # rows just created from the `materials` payload.
    db.session.commit()
    if product.published and FormulaSheet.query.filter_by(product=name).first():
        _sync_formula_to_product_materials(name)
    return jsonify({"success": True, "product": product.to_dict()}), 201


@app.route('/api/products/set-price', methods=['POST'])
def api_products_set_price():
    """Item 4 (2026-10-01): one price for every product + 3rd party product,
    set from the Product Details page's Price button (admin only)."""
    data = request.get_json(silent=True) or {}
    try:
        price = float(data.get('price'))
    except (TypeError, ValueError):
        return _fail("price must be a number")
    if price < 0:
        return _fail("price cannot be negative")
    updated_products = Product.query.update({Product.price: int(round(price))},
                                            synchronize_session=False)
    updated_third = ThirdPartyItem.query.update({ThirdPartyItem.price: price},
                                                synchronize_session=False)
    db.session.commit()
    return _ok({"products": updated_products, "third_party": updated_third,
                "price": price})


@app.route('/api/products/<int:product_id>', methods=['PUT'])
def api_products_update(product_id):
    product = Product.query.get(product_id)
    if not product:
        return _fail("Product not found", 404)
    data = request.get_json(silent=True) or {}
    if 'name' in data:
        name = str(data['name']).strip()
        if not name:
            return _fail("Product name cannot be empty")
        existing = Product.query.filter_by(name=name).first()
        if existing and existing.id != product.id:
            return _fail("Another product with this name exists", 409)
        old_name = product.name
        product.name = name
        ProductMaterial.query.filter_by(product_name=old_name).update({"product_name": name})
        MaterialDaily.query.filter_by(product_name=old_name).update({"product_name": name})
        FormulaSheet.query.filter_by(product=old_name).update({"product": name})
        ProductionEntry.query.filter_by(product=old_name).update({"product": name})
        MachineProductionEntry.query.filter_by(product_name=old_name).update({"product_name": name})
        if old_name in _pm_hist:
            _pm_hist[name] = _pm_hist.pop(old_name)
    if 'category' in data:
        product.category = str(data['category'])
    if 'price' in data:
        product.price = _parse_int(data['price'], product.price)
    if 'stock' in data:
        stock = _parse_int(data['stock'], product.stock)
        if stock < 0:
            return _fail("Stock cannot be negative")
        product.stock = stock
    if 'image' in data:
        product.image = str(data['image'])
    if 'published' in data:
        product.published = bool(data['published'])
    product.status = compute_product_status(product.stock)
    db.session.commit()
    return _ok({"product": product.to_dict()})


@app.route('/api/products/<int:product_id>/upload-image', methods=['POST'])
def api_products_upload_image(product_id):
    product = Product.query.get(product_id)
    if not product:
        return _fail("Product not found", 404)
    if 'image' not in request.files:
        return _fail("No image file provided", 400)
    file = request.files['image']
    if file.filename == '':
        return _fail("No file selected", 400)
    if not file.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return _fail('Only JPG/PNG/WEBP images are allowed', 400)
    # Hard 2 MB cap. The dashboard photo uploader already crops + compresses
    # every photo client-side, so anything larger here means the client did
    # not run (old page, scripted request) and is rejected instead of
    # silently eating the request quota. The read is capped so an oversized
    # upload is never pulled fully into memory.
    data = _read_capped_upload(file)
    if not data:
        return _fail("The uploaded file is empty.", 400)
    if len(data) > PRODUCT_IMAGE_MAX_BYTES:
        return _fail(
            f"Image is too large (over {PRODUCT_IMAGE_MAX_BYTES // (1024 * 1024)} MB). "
            "Please choose a smaller image.",
            400
        )
    # The stored extension follows the image CONTENT, so the bytes and the
    # filename always agree.
    ext = _sniff_image_ext(data)
    if not ext:
        return _fail('That file is not a valid JPG, PNG or WEBP image.', 400)
    prefix = "".join(w[0] for w in product.name.split() if w).upper()[:3] or "PRD"
    filename = f"{prefix}_{product.id}.{ext}"
    # Serverless (Vercel) has no S3 bucket: keep the photo in the database so
    # the camera upload really works instead of failing with 503.
    if storage_status() == 'unconfigured':
        product.image = filename
        product.image_data = data
        product.image_mime = _image_mime_for_ext(ext)
        db.session.commit()
        return _ok({"product": product.to_dict(), "image": filename, "stored": "database"})
    try:
        save_upload(_BytesFileStorage(filename, data), 'products', filename,
                    {'.jpg', '.jpeg', '.png', '.webp'})
    except ValueError as exc:
        return _fail(str(exc), 400)
    except (FileStorageUnavailable, FileStorageError) as exc:
        return _fail(str(exc), 503)
    product.image = filename
    product.image_data = None
    product.image_mime = None
    db.session.commit()
    return _ok({"product": product.to_dict(), "image": filename})


def _lookup_product_image_blob(filename):
    """Find a dashboard photo that was stored in the database (Vercel)."""
    base = os.path.basename(filename or '')
    if not base:
        return None
    for model in (Product, ThirdPartyItem):
        row = model.query.filter_by(image=base).first()
        if row is not None and getattr(row, 'image_data', None):
            return row
    return None


def _product_image_blob(filename):
    """Dashboard photo stored in the database (Vercel, no S3 bucket)."""
    row = _lookup_product_image_blob(filename)
    if row is None:
        return None
    return bytes(row.image_data), (row.image_mime or 'image/jpeg')


@app.route('/product-images/<path:filename>')
def product_image(filename):
    return _send_stored_file('products', filename,
                             os.path.join(BASE_DIR, 'static', 'img', 'products'),
                             blob_lookup=_product_image_blob)


@app.route('/api/products/<path:product_name>', methods=['GET'])
def api_products_get_by_name(product_name):
    product = Product.query.filter_by(name=product_name).first()
    if not product:
        return _fail("Product not found", 404)
    return _ok({"product": product.to_dict()})


def _sync_formula_to_product_materials(name):
    """Copy the product's published formula sheet into product_materials so the
    user side Production Update page shows exactly the same materials and
    machine quantities that were set on the admin Production Formula page.

    Existing rows keep their opening stock / saved batches; only the materials
    and machine quantities follow the formula. Materials removed from the
    formula are also removed from the user-side material list.
    """
    product = Product.query.filter_by(name=name).first()
    if not product:
        return
    sheet = FormulaSheet.query.filter_by(product=name).order_by(FormulaSheet.id).all()
    keep = set()
    for row in sheet:
        material = (row.material or '').strip()
        if not material:
            continue
        keep.add(material)
        pm = ProductMaterial.query.filter_by(product_name=name, material=material).first()
        if not pm:
            pm = ProductMaterial(product_id=product.id, product_name=name, material=material,
                                 unit='kg', opening=0, consumed=0, closing=0,
                                 old_batches=0, new_batches=0)
            db.session.add(pm)
        pm.old_machine_qty = row.old_machine or 0
        pm.new_machine_qty = row.new_machine or 0
    for pm in ProductMaterial.query.filter_by(product_name=name).all():
        if (pm.material or '').strip() not in keep:
            db.session.delete(pm)
    db.session.commit()


@app.route('/api/products/<path:product_name>/publish', methods=['POST'])
def api_products_publish(product_name):
    product = Product.query.filter_by(name=product_name).first()
    if not product:
        return _fail("Product not found", 404)
    # Item 1 (2026-10-01): publishing is only allowed once a formula has been
    # saved (syncing an empty sheet would wipe the user-side material list).
    if not product.published and not FormulaSheet.query.filter_by(product=product_name).first():
        return _fail("Add items and save the formula before publishing", 400)
    product.published = not product.published
    if product.published:
        _sync_formula_to_product_materials(product.name)
    db.session.commit()
    return _ok({"product": product.to_dict(), "published": product.published})


@app.route('/api/third-party-items/<int:item_id>/publish', methods=['POST'])
def api_third_party_publish(item_id):
    item = ThirdPartyItem.query.get(item_id)
    if not item:
        return _fail("3rd Party item not found", 404)
    item.published = not item.published
    db.session.commit()
    return _ok({"item": item.to_dict(), "published": item.published})


@app.route('/api/products/<int:product_id>', methods=['DELETE'])
def api_products_delete(product_id):
    product = Product.query.get(product_id)
    if not product:
        return _fail("Product not found", 404)
    name = product.name
    ProductMaterial.query.filter_by(product_id=product.id).delete()
    ProductMaterial.query.filter_by(product_name=name).delete()
    MaterialDaily.query.filter_by(product_name=name).delete()
    FormulaSheet.query.filter_by(product=name).delete()
    ProductionEntry.query.filter_by(product=name).delete()
    MachineProductionEntry.query.filter_by(product_name=name).delete()
    ConsumptionLog.query.filter_by(product_name=name).delete()
    _pm_hist.pop(name, None)
    db.session.delete(product)
    db.session.commit()
    return _ok()


# ---------- Resources API ----------
@app.route('/api/resources', methods=['GET'])
def api_resources_list():
    group = request.args.get('group', 'all')
    query = Resource.query
    if group != 'all':
        query = query.filter_by(group=group)
    return _ok({"resources": [r.to_dict() for r in query.order_by(Resource.id).all()]})


@app.route('/api/resources', methods=['POST'])
def api_resources_create():
    data = request.get_json(silent=True) or {}
    group = str(data.get('group', 'Raw Materials')).strip() or 'Raw Materials'
    name = str(data.get('name', '')).strip()
    if not name:
        return _fail("Resource name is required")
    problem = _resource_name_problem(name)
    if problem:
        return _fail(problem)
    opening = _parse_int(data.get('opening', 0))
    consumed = _parse_int(data.get('consumed', 0))
    min_stock = _parse_int(data.get('min_stock', 250))
    if min(opening, consumed, min_stock) < 0:
        return _fail("Stock values cannot be negative")
    for label, value in (("Stock", opening), ("Consumed", consumed),
                         ("Minimum stock", min_stock)):
        if (len(str(abs(int(value)))) > RESOURCE_FIELD_MAX_CHARS
                or _stock_field_too_large(value)):
            return _fail(
                "%s value is excessive - it must be at most %d digits (%d)."
                % (label, RESOURCE_FIELD_MAX_CHARS, RESOURCE_MAX_VALUE))
    category = str(data.get('category', '')).strip()
    existing = Resource.query.filter_by(group=group, name=name).first()
    if existing:
        # Picked an existing item in the Add Resource dialog: keep the stored
        # stock unless a DIFFERENT number was submitted (the dialog prefills
        # the current value, so a save-without-edits changes nothing).
        if 'opening' in data and opening != (existing.closing or 0):
            existing.opening = opening
            existing.consumed = 0
            existing.purchased = 0
            existing.sold = 0
        if min_stock >= 0 and 'min_stock' in data:
            existing.min_stock = min_stock
        if category:
            existing.item_category = category
        existing.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
        existing.recompute()
        db.session.commit()
        return jsonify({"success": True, "resource": existing.to_dict(),
                        "updated": True}), 200
    resource = Resource(
        group=group, name=name,
        item_category=category,
        unit=str(data.get('unit', 'kg')) or 'kg',
        opening=opening, consumed=consumed,
        closing=max(opening - consumed, 0), min_stock=min_stock,
        last_update=datetime.now().strftime("%d %b %Y, %I:%M %p"),
    )
    resource.recompute()
    db.session.add(resource)
    db.session.commit()
    return jsonify({"success": True, "resource": resource.to_dict()}), 201


@app.route('/api/resources/<int:resource_id>', methods=['PUT'])
def api_resources_update(resource_id):
    resource = Resource.query.get(resource_id)
    if not resource:
        return _fail("Resource not found", 404)
    orig_opening = resource.opening
    orig_closing = resource.closing
    orig_last_update = resource.last_update
    data = request.get_json(silent=True) or {}
    if 'name' in data:
        name = str(data['name']).strip()
        if not name:
            return _fail("Resource name cannot be empty")
        problem = _resource_name_problem(name)
        if problem:
            return _fail(problem)
        resource.name = name
    for field in ('group', 'item_category', 'category', 'unit'):
        if field in data and data[field] is not None:
            value = str(data[field])
            if field == 'group':
                resource.group = value
            elif field in ('item_category', 'category'):
                resource.item_category = value
            elif field == 'unit':
                resource.unit = value or resource.unit
    for field in ('opening', 'consumed', 'min_stock', 'purchased', 'sold'):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if value < 0:
                return _fail(f"{field} cannot be negative")
            if _stock_field_too_large(value):
                return _fail(
                    f"{field} value is excessive - it must be at most "
                    f"{RESOURCE_FIELD_MAX_CHARS} digits ({RESOURCE_MAX_VALUE}).")
            setattr(resource, field, value)
    if 'opening' in data:
        # An UNCHANGED save is not a stock take. The dialogs prefill the box
        # with the current closing, so re-saving without typing anything used
        # to zero the counters and (with a consumed>0 row) collapse the closing
        # to 0. Only a DIFFERENT number re-counts the shelf.
        if (int(data['opening']) == (orig_closing or 0)
                and 'purchased' not in data and 'sold' not in data
                and 'consumed' not in data):
            resource.opening = orig_opening
            resource.last_update = orig_last_update
            return _ok({"resource": resource.to_dict(), "unchanged": True})
        # Real stock take: the purchase/sale counters start over unless the
        # caller set them explicitly in the same request (production
        # consumption is NOT cleared here - it belongs to the Production
        # Update flow, and stock_formula_test pins that behaviour).
        if 'purchased' not in data:
            resource.purchased = 0
        if 'sold' not in data:
            resource.sold = 0
    resource.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
    resource.recompute()
    db.session.commit()
    return _ok({"resource": resource.to_dict()})


@app.route('/api/resources/<int:resource_id>', methods=['DELETE'])
def api_resources_delete(resource_id):
    resource = Resource.query.get(resource_id)
    if not resource:
        return _fail("Resource not found", 404)
    db.session.delete(resource)
    db.session.commit()
    return _ok()


@app.route('/api/resources/<int:resource_id>/consume', methods=['POST'])
def api_resources_consume(resource_id):
    resource = Resource.query.get(resource_id)
    if not resource:
        return _fail("Resource not found", 404)
    data = request.get_json(silent=True) or {}
    try:
        qty = int(data.get('quantity', 0))
    except (TypeError, ValueError):
        return _fail("quantity must be a number")
    if qty <= 0:
        return _fail("quantity must be positive")
    if resource.closing - qty < 0:
        return _fail("Not enough closing stock", 409)
    resource.consumed += qty
    resource.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
    resource.recompute()
    db.session.commit()
    return _ok({"resource": resource.to_dict()})


@app.route('/api/resources/bulk-stock', methods=['POST'])
def api_resources_bulk_stock():
    """Save & publish edited stocks for a whole group. Only admin can edit after password. Syncs to user via shared DB."""
    data = request.get_json(silent=True) or {}
    group = str(data.get('group', '')).strip()
    items = data.get('items', [])
    if not group or not isinstance(items, list):
        return _fail("group and items required")
    updated = 0
    skipped = 0
    for it in items:
        name = str(it.get('name', '')).strip()
        if not name:
            continue
        problem = _resource_name_problem(name)
        if problem:
            return _fail(problem)
        try:
            stock = int(it.get('stock', 0))
        except (TypeError, ValueError):
            return _fail(f"stock for {name} must be a number")
        if stock < 0:
            return _fail(f"stock for {name} cannot be negative")
        if len(str(stock)) > RESOURCE_FIELD_MAX_CHARS or _stock_field_too_large(stock):
            return _fail(
                "Stock value for %s is excessive - it must be at most %d digits (%d)."
                % (name, RESOURCE_FIELD_MAX_CHARS, RESOURCE_MAX_VALUE))
        res = Resource.query.filter_by(group=group, name=name).first()
        if not res:
            res = Resource(group=group, name=name, item_category="", unit="kg", opening=stock, consumed=0, closing=stock, min_stock=250, last_update=datetime.now().strftime("%d %b %Y, %I:%M %p"))
            res.recompute()
            db.session.add(res)
            updated += 1
            continue
        if stock == (res.closing or 0):
            # Saving the row without changing the number is NOT a stock take.
            # Leave the row (and its purchase/consumption history) exactly as it
            # is - this is what used to wipe a 50,000 kg row down to 0.
            skipped += 1
            continue
        res.closing = stock
        res.opening = stock
        res.consumed = 0
        res.purchased = 0
        res.sold = 0
        res.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
        res.recompute()
        updated += 1
    db.session.commit()
    return _ok({"updated": updated, "unchanged": skipped})


# ---------- Raw materials API (persistent) ----------
@app.route('/api/raw-materials', methods=['GET'])
def api_materials_list():
    return _ok({"materials": [m.to_dict() for m in RawMaterial.query.order_by(RawMaterial.id).all()]})


@app.route('/api/raw-materials', methods=['POST'])
def add_material():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    unit = str(data.get('unit', 'kg')).strip() or 'kg'
    opening = _parse_int(data.get('opening', 0))
    min_stock = _parse_int(data.get('min_stock', 50))
    if not name or opening < 0 or min_stock < 0:
        return _fail("Name is required and stock values cannot be negative")
    if RawMaterial.query.filter_by(name=name).first():
        return _fail("Material already exists", 409)
    mat = RawMaterial(name=name, unit=unit, opening=opening, consumed=0,
                      closing=opening, min_stock=min_stock)
    mat.recompute()
    db.session.add(mat)
    db.session.commit()
    return jsonify({"success": True, "material": mat.to_dict()}), 201


@app.route('/api/raw-materials/<int:material_id>', methods=['PUT'])
def update_material(material_id):
    mat = RawMaterial.query.get(material_id)
    if not mat:
        return _fail("Material not found", 404)
    data = request.get_json(silent=True) or {}
    if 'name' in data:
        name = str(data['name']).strip()
        if not name:
            return _fail("Material name cannot be empty")
        mat.name = name
    if 'unit' in data:
        mat.unit = str(data['unit']).strip() or mat.unit
    for field in ('opening', 'consumed', 'closing', 'min_stock'):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if value < 0:
                return _fail(f"{field} cannot be negative")
            setattr(mat, field, value)
    mat.status = compute_raw_status(mat.closing, mat.min_stock)
    db.session.commit()
    return _ok({"material": mat.to_dict()})


@app.route('/api/raw-materials/<int:material_id>', methods=['DELETE'])
def api_materials_delete(material_id):
    mat = RawMaterial.query.get(material_id)
    if not mat:
        return _fail("Material not found", 404)
    db.session.delete(mat)
    db.session.commit()
    return _ok()


# ---------- Product raw-materials API (inventory detail pages) ----------
def _pm_status(closing: int) -> str:
    return "Low Stock" if closing < 150 else "Available"


# Server-side Undo/Redo history per product. Lives in the running server
# process so the buttons work regardless of browser storage or tab.
_PM_HIST_LIMIT = 50
_pm_hist = {}


def _pm_hist_for(product_name):
    return _pm_hist.setdefault(product_name, {"undo": [], "redo": []})


def _pm_snapshot(row):
    return {"product_name": row.product_name, "material": row.material,
            "unit": row.unit, "opening": row.opening,
            "consumed": row.consumed, "batch": row.batch or "",
            "old_batches": row.old_batches or 0,
            "new_batches": row.new_batches or 0}


def _pm_record(product_name, entry):
    h = _pm_hist_for(product_name)
    h["undo"].append(entry)
    h["redo"].clear()
    del h["undo"][:-_PM_HIST_LIMIT]


def _pm_counts(product_name):
    h = _pm_hist_for(product_name)
    return {"undo_count": len(h["undo"]), "redo_count": len(h["redo"])}


def _pm_create_row(product_name, vals, row_id=None):
    """Create a ProductMaterial row from snapshot values. Returns row.

    When row_id is given (undo/redo restore), the row is re-inserted with
    its original id so it returns to its exact original position in the
    table. Falls back to a fresh id if that id is already taken.
    """
    from sqlalchemy.exc import IntegrityError
    product = Product.query.filter_by(name=product_name).first()
    if not product:
        return None
    opening = _parse_int(vals.get("opening", 0))
    consumed = _parse_int(vals.get("consumed", 0))
    closing = max(opening - consumed, 0)

    def build(rid):
        kwargs = dict(
            product_id=product.id, product_name=product_name,
            material=str(vals.get("material", "")).strip(),
            unit=str(vals.get("unit", "kg")) or "kg",
            opening=opening, consumed=consumed, closing=closing,
            batch=str(vals.get("batch", "") or ""),
            status=_pm_status(closing),
            old_batches=_parse_int(vals.get("old_batches", 0)),
            new_batches=_parse_int(vals.get("new_batches", 0)),
        )
        if rid is not None:
            kwargs["id"] = rid
        return ProductMaterial(**kwargs)

    if row_id is not None and ProductMaterial.query.get(row_id) is None:
        try:
            row = build(row_id)
            db.session.add(row)
            db.session.flush()
            return row
        except IntegrityError:
            db.session.rollback()
    row = build(None)
    db.session.add(row)
    return row


def _pm_apply(row, vals):
    row.material = str(vals.get("material", row.material)).strip() or row.material
    row.unit = str(vals.get("unit", row.unit)) or row.unit
    row.batch = str(vals.get("batch", row.batch or "") or "")
    row.opening = _parse_int(vals.get("opening", row.opening))
    row.consumed = _parse_int(vals.get("consumed", row.consumed))
    row.closing = max(row.opening - row.consumed, 0)
    row.status = _pm_status(row.closing)
    if "old_batches" in vals:
        row.old_batches = _parse_int(vals.get("old_batches", row.old_batches))
    if "new_batches" in vals:
        row.new_batches = _parse_int(vals.get("new_batches", row.new_batches))


def _pm_sync_daily(row):
    today = datetime.now().strftime("%Y-%m-%d")
    daily = MaterialDaily.query.filter_by(
        product_name=row.product_name, material=row.material, date=today).first()
    if daily:
        daily.consumed = row.consumed
        daily.closing = row.closing


@app.route('/api/product-materials', methods=['GET'])
def api_pm_list():
    product_name = request.args.get('product', '')
    query = ProductMaterial.query
    if product_name:
        query = query.filter_by(product_name=product_name)
    return _ok({"materials": [r.to_dict() for r in query.order_by(ProductMaterial.id).all()]})


@app.route('/api/product-materials', methods=['POST'])
def api_pm_create():
    data = request.get_json(silent=True) or {}
    product_name = str(data.get('product_name', data.get('product', ''))).strip()
    material = str(data.get('material', '')).strip()
    if not product_name or not material:
        return _fail("product_name and material are required")
    product = Product.query.filter_by(name=product_name).first()
    if not product:
        return _fail("Product not found", 404)
    opening = _parse_int(data.get('opening', 0))
    consumed = _parse_int(data.get('consumed', 0))
    if opening < 0 or consumed < 0:
        return _fail("Stock values cannot be negative")
    closing = max(opening - consumed, 0)
    row = ProductMaterial(
        product_id=product.id, product_name=product_name, material=material,
        unit=str(data.get('unit', 'kg')) or 'kg',
        opening=opening, consumed=consumed, closing=closing,
        batch=str(data.get('batch', '')).strip(),
        status=_pm_status(closing),
        old_batches=_parse_int(data.get('old_batches', 0)),
        new_batches=_parse_int(data.get('new_batches', 0)),
    )
    db.session.add(row)
    db.session.commit()
    _daily_backfill_row(row, datetime.now().strftime("%Y-%m-%d"), days=7)
    db.session.commit()
    _pm_record(product_name, {"t": "add", "id": row.id, "snap": row.to_dict()})
    return jsonify({"success": True, "material": row.to_dict(),
                    **_pm_counts(product_name)}), 201


@app.route('/api/product-materials/daily', methods=['GET'])
def api_pm_daily():
    product_name = request.args.get('product', '').strip()
    datestr = request.args.get('date', '').strip()
    if not product_name or not datestr:
        return _fail("product and date are required")
    try:
        datetime.strptime(datestr, "%Y-%m-%d")
    except ValueError:
        return _fail("Invalid date (use YYYY-MM-DD)")
    rows = MaterialDaily.query.filter_by(
        product_name=product_name, date=datestr).order_by(MaterialDaily.id).all()
    dicts = [r.to_dict() for r in rows]
    return _ok({"date": datestr, "rows": dicts,
                "total_consumed": sum(r.consumed for r in rows),
                "total_closing": sum(r.closing for r in rows)})


@app.route('/api/product-materials/<int:row_id>', methods=['PUT'])
def api_pm_update(row_id):
    row = ProductMaterial.query.get(row_id)
    if not row:
        return _fail("Material not found", 404)
    before = _pm_snapshot(row)
    data = request.get_json(silent=True) or {}
    if 'material' in data:
        material = str(data['material']).strip()
        if not material:
            return _fail("Material name cannot be empty")
        row.material = material
    if 'unit' in data:
        row.unit = str(data['unit']) or row.unit
    if 'batch' in data:
        row.batch = str(data['batch'])
    for field in ('opening', 'consumed'):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if value < 0:
                return _fail(f"{field} cannot be negative")
            setattr(row, field, value)
    for field in ('old_batches', 'new_batches'):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if value < 0:
                return _fail(f"{field} cannot be negative")
            setattr(row, field, value)
    row.closing = max(row.opening - row.consumed, 0)
    row.status = _pm_status(row.closing)
    db.session.commit()
    _pm_record(row.product_name, {"t": "edit", "id": row.id,
                                  "before": before, "after": _pm_snapshot(row)})
    return _ok({"material": row.to_dict(), **_pm_counts(row.product_name)})


@app.route('/api/product-materials/<int:row_id>/consume', methods=['POST'])
def api_pm_consume(row_id):
    row = ProductMaterial.query.get(row_id)
    if not row:
        return _fail("Material not found", 404)
    data = request.get_json(silent=True) or {}
    try:
        qty = int(data.get('quantity', 0))
    except (TypeError, ValueError):
        return _fail("quantity must be a number")
    if qty <= 0:
        return _fail("quantity must be positive")
    if row.closing - qty < 0:
        return _fail("Not enough closing stock", 409)
    before = _pm_snapshot(row)
    row.consumed += qty
    row.closing = max(row.opening - row.consumed, 0)
    row.status = _pm_status(row.closing)
    today = datetime.now().strftime("%Y-%m-%d")
    daily = MaterialDaily.query.filter_by(
        product_name=row.product_name, material=row.material, date=today).first()
    if daily:
        daily.consumed = row.consumed
        daily.closing = row.closing
    db.session.add(ConsumptionLog(date=today, product_name=row.product_name,
                                  material=row.material, quantity=qty,
                                  actor=str(data.get('actor', 'Admin')).strip() or 'Admin'))
    db.session.commit()
    _pm_record(row.product_name, {"t": "edit", "id": row.id,
                                  "before": before, "after": _pm_snapshot(row)})
    return _ok({"material": row.to_dict(), **_pm_counts(row.product_name)})


@app.route('/api/product-materials/bulk', methods=['PUT'])
def api_pm_bulk():
    """Update many rows at once (Update All / Add Consumed).

    Body: {product_name, date?, shift?, items: [{id, opening?, consumed?,
    batch?, old_batches?, new_batches?, qty_per_batch?, shift_opening?}]}.
    Closing = opening - consumed is recalculated per row; the daily stock
    for the given date/shift (defaults to today) is synced when consumed
    changes — created if missing using the supplied shift opening so the
    "Add Consumed" stock reduction shows consistently on both portals.
    Recorded as ONE undo step.
    """
    data = request.get_json(silent=True) or {}
    product_name = str(data.get('product_name', data.get('product', ''))).strip()
    items = data.get('items', [])
    if not product_name or not isinstance(items, list) or not items:
        return _fail("product_name and a non-empty items list are required")
    rows = {r.id: r for r in ProductMaterial.query.filter_by(product_name=product_name).all()}
    if not rows:
        return _fail("Product not found", 404)
    changes = []
    today = datetime.now().strftime("%Y-%m-%d")
    day = str(data.get('date', today) or today).strip()
    shift = str(data.get('shift', '') or '').strip()
    actor = str(data.get('actor', 'Admin')).strip() or 'Admin'
    for it in items:
        if not isinstance(it, dict) or 'id' not in it:
            continue
        try:
            rid = int(it['id'])
        except (TypeError, ValueError):
            return _fail("id must be a number")
        row = rows.get(rid)
        if not row:
            db.session.rollback()
            return _fail(f"Row {rid} not found in {product_name}", 404)
        vals = {}
        for field in ('opening', 'consumed', 'old_batches', 'new_batches'):
            if field in it and it[field] != '' and it[field] is not None:
                try:
                    value = int(it[field])
                except (TypeError, ValueError):
                    db.session.rollback()
                    return _fail(f"{row.material}: {field} must be a number")
                if value < 0:
                    db.session.rollback()
                    return _fail(f"{row.material}: {field} cannot be negative")
                vals[field] = value
        if 'batch' in it and str(it.get('batch', '')).strip() != str(row.batch or ''):
            vals['batch'] = str(it['batch']).strip()
        shift_opening = None
        if it.get('shift_opening') is not None and str(it.get('shift_opening')).strip() != '':
            try:
                shift_opening = int(it['shift_opening'])
            except (TypeError, ValueError):
                db.session.rollback()
                return _fail(f"{row.material}: shift_opening must be a number")
            if shift_opening < 0:
                db.session.rollback()
                return _fail(f"{row.material}: shift_opening cannot be negative")
        if not vals:
            continue
        opening = vals.get('opening', row.opening)
        consumed = vals.get('consumed', row.consumed)
        allow = max(opening, shift_opening or 0)
        if consumed > allow:
            db.session.rollback()
            return _fail(f"{row.material}: consumed ({consumed}) exceeds available opening stock ({allow})", 409)
        after = {**_pm_snapshot(row), **vals, "closing": max(opening - consumed, 0)}
        if any(_pm_snapshot(row).get(k) != after.get(k) for k in _pm_snapshot(row)):
            changes.append({"id": rid, "before": _pm_snapshot(row), "after": after,
                            "consumed_changed": consumed != row.consumed,
                            "shift_opening": shift_opening})
    if not changes:
        return _ok({"updated": [], **_pm_counts(product_name)})
    updated = []
    for ch in changes:
        row = rows[ch["id"]]
        old_consumed = row.consumed
        _pm_apply(row, ch["after"])
        if ch["consumed_changed"]:
            extra = max((row.consumed or 0) - int(old_consumed or 0), 0)
            if extra:
                live_res = _resource_by_name(str(row.material))
                if live_res is not None:
                    # Production usage belongs in Resource.consumed so the stock
                    # formula keeps it across purchases and stock edits.
                    live_res.consumed = (live_res.consumed or 0) + extra
                    live_res.recompute()
            # M3: record the exact Resource delta so undo/redo can reverse it -
            # previously undo only restored the ProductMaterial row and left the
            # bumped Resource.consumed permanently skewed.
            ch["res_extra"] = extra
            so = ch.get("shift_opening")
            q = MaterialDaily.query.filter_by(
                product_name=product_name, material=row.material, date=day)
            if shift:
                q = q.filter(MaterialDaily.shift == shift)
            daily = q.first()
            if daily:
                daily.consumed = row.consumed
                daily.closing = max((daily.opening or 0) - row.consumed, 0)
            elif so is not None:
                daily_closing = max(so - row.consumed, 0)
                db.session.add(MaterialDaily(
                    product_name=product_name, material=row.material, date=day,
                    shift=shift or 'morning', opening=so, consumed=row.consumed,
                    closing=daily_closing,
                    batch='%s/%s' % (row.old_batches or 0, row.new_batches or 0)))
            db.session.add(ConsumptionLog(date=day, product_name=product_name,
                                          material=row.material,
                                          quantity=row.consumed - old_consumed,
                                          actor=actor))
        updated.append(row.to_dict())
    db.session.commit()
    _pm_record(product_name, {"t": "bulk", "changes": changes})
    return _ok({"updated": updated, **_pm_counts(product_name)})


@app.route('/api/product-materials/remove-all', methods=['POST'])
def api_pm_remove_all():
    data = request.get_json(silent=True) or {}
    product_name = str(data.get('product_name', data.get('product', ''))).strip()
    if not product_name:
        return _fail("product_name is required")
    rows = ProductMaterial.query.filter_by(product_name=product_name).all()
    if not rows:
        return _ok({"removed": 0, **_pm_counts(product_name)})
    snapshots = [_pm_snapshot(row) for row in rows]
    for row in rows:
        db.session.delete(row)
    db.session.commit()
    _pm_record(product_name, {"t": "bulkdel", "items": snapshots})
    return _ok({"removed": len(rows), **_pm_counts(product_name)})


@app.route('/api/product-materials/<int:row_id>', methods=['DELETE'])
def api_pm_delete(row_id):
    row = ProductMaterial.query.get(row_id)
    if not row:
        return _fail("Material not found", 404)
    snap = _pm_snapshot(row)
    product_name = row.product_name
    deleted_id = row.id
    db.session.delete(row)
    db.session.commit()
    _pm_record(product_name, {"t": "del", "id": deleted_id, "snap": snap})
    return _ok(**_pm_counts(product_name))


@app.route('/api/product-materials/history', methods=['GET'])
def api_pm_history():
    product_name = request.args.get('product', '')
    return _ok(_pm_counts(product_name))


@app.route('/api/product-materials/undo', methods=['POST'])
def api_pm_undo():
    data = request.get_json(silent=True) or {}
    product_name = str(data.get('product_name', data.get('product', ''))).strip()
    h = _pm_hist_for(product_name)
    if not h["undo"]:
        return _fail("Nothing to undo")
    e = h["undo"].pop()
    if e["t"] == "add":
        row = ProductMaterial.query.get(e["id"])
        if row:
            db.session.delete(row)
        h["redo"].append({"t": "add", "id": e["id"], "snap": e["snap"]})
    elif e["t"] == "edit":
        row = ProductMaterial.query.get(e["id"])
        if not row:
            db.session.rollback()
            h["undo"].append(e)
            return _fail("That row no longer exists, cannot undo")
        _pm_apply(row, e["before"])
        _pm_sync_daily(row)
        h["redo"].append(e)
    elif e["t"] == "del":
        created = _pm_create_row(e["snap"]["product_name"], e["snap"], row_id=e.get("id"))
        if not created:
            db.session.rollback()
            h["undo"].append(e)
            return _fail("Product no longer exists, cannot undo")
        db.session.flush()
        h["redo"].append({"t": "del", "id": created.id, "snap": e["snap"]})
    elif e["t"] == "bulkdel":
        restored = []
        for snap in e["items"]:
            created = _pm_create_row(snap["product_name"], snap, row_id=snap.get("id"))
            if created:
                restored.append(created.id)
        h["redo"].append(e)
    elif e["t"] == "bulk":
        for ch in e["changes"]:
            row = ProductMaterial.query.get(ch["id"])
            if not row:
                db.session.rollback()
                h["undo"].append(e)
                return _fail("A row no longer exists, cannot undo")
            _pm_apply(row, ch["before"])
            # M3: reverse the Resource.consumed bump the bulk write applied,
            # otherwise undo leaves the live stock permanently decremented.
            res_extra = ch.get("res_extra", 0)
            if res_extra:
                live_res = _resource_by_name(str(row.material))
                if live_res is not None:
                    live_res.consumed = max((live_res.consumed or 0) - int(res_extra), 0)
                    live_res.recompute()
            _pm_sync_daily(row)
        h["redo"].append(e)
    db.session.commit()
    return _ok(_pm_counts(product_name))


@app.route('/api/product-materials/redo', methods=['POST'])
def api_pm_redo():
    data = request.get_json(silent=True) or {}
    product_name = str(data.get('product_name', data.get('product', ''))).strip()
    h = _pm_hist_for(product_name)
    if not h["redo"]:
        return _fail("Nothing to redo")
    e = h["redo"].pop()
    if e["t"] == "add":
        created = _pm_create_row(e["snap"]["product_name"], e["snap"], row_id=e.get("id"))
        if not created:
            db.session.rollback()
            h["redo"].append(e)
            return _fail("Product no longer exists, cannot redo")
        db.session.flush()
        h["undo"].append({"t": "add", "id": created.id, "snap": created.to_dict()})
    elif e["t"] == "edit":
        row = ProductMaterial.query.get(e["id"])
        if not row:
            db.session.rollback()
            h["redo"].append(e)
            return _fail("That row no longer exists, cannot redo")
        _pm_apply(row, e["after"])
        _pm_sync_daily(row)
        h["undo"].append(e)
    elif e["t"] == "del":
        row = ProductMaterial.query.get(e["id"])
        if row:
            db.session.delete(row)
        h["undo"].append({"t": "del", "id": e["id"], "snap": e["snap"]})
    elif e["t"] == "bulkdel":
        for snap in e["items"]:
            row = ProductMaterial.query.get(snap.get("id"))
            if row:
                db.session.delete(row)
        h["undo"].append(e)
    elif e["t"] == "bulk":
        for ch in e["changes"]:
            row = ProductMaterial.query.get(ch["id"])
            if not row:
                db.session.rollback()
                h["redo"].append(e)
                return _fail("A row no longer exists, cannot redo")
            _pm_apply(row, ch["after"])
            # M3: re-apply the same Resource.consumed delta the original bulk
            # write did - recorded as res_extra so redo is exactly reversible.
            res_extra = ch.get("res_extra", 0)
            if res_extra:
                live_res = _resource_by_name(str(row.material))
                if live_res is not None:
                    live_res.consumed = (live_res.consumed or 0) + int(res_extra)
                    live_res.recompute()
            _pm_sync_daily(row)
        h["undo"].append(e)
    db.session.commit()
    return _ok(_pm_counts(product_name))


# ---------- Production API (persistent + stock effect) ----------
def _prod_valid_date(value, default_today=True):
    """Return a YYYY-MM-DD string or None if invalid / in the future."""
    raw = str(value or "").strip()
    if not raw and default_today:
        return datetime.now().strftime("%Y-%m-%d")
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    if parsed.date() > datetime.now().date():
        return None
    return parsed.strftime("%Y-%m-%d")


def _prod_display_date(datestr):
    try:
        return datetime.strptime(datestr, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return datestr


def _prod_history_days(entries):
    days = {}
    for e in entries:
        days.setdefault(e.date, []).append(e)
    result = []
    for day in sorted(days.keys(), reverse=True):
        rows = sorted(days[day], key=lambda x: x.id)
        result.append({
            "date": day,
            "display": _prod_display_date(day),
            "entries": [r.to_dict() for r in rows],
            "total": sum(r.quantity for r in rows),
        })
    return result


def _prod_monthly_rows(entries, product_names):
    months = {}
    for e in entries:
        key = e.date[:7]
        bucket = months.setdefault(key, {"per_product": {n: 0 for n in product_names}, "days": set()})
        if e.product in bucket["per_product"]:
            bucket["per_product"][e.product] += e.quantity
        else:
            bucket["per_product"][e.product] = e.quantity
        bucket["days"].add(e.date)
    result = []
    for key in sorted(months.keys(), reverse=True):
        bucket = months[key]
        try:
            label = datetime.strptime(key, "%Y-%m").strftime("%B %Y")
        except ValueError:
            label = key
        total = sum(bucket["per_product"].values())
        result.append({"key": key, "label": label,
                       "per_product": bucket["per_product"],
                       "total": total, "days": len(bucket["days"])})
    return result


@app.route('/api/production', methods=['GET'])
def api_production_list():
    rows = ProductionEntry.query.order_by(ProductionEntry.date.desc(), ProductionEntry.id.desc()).all()
    return _ok({"productions": [r.to_dict() for r in rows]})


@app.route('/api/production/history', methods=['GET'])
def api_production_history():
    day = request.args.get('date', '').strip()
    query = ProductionEntry.query.order_by(ProductionEntry.date.desc(), ProductionEntry.id)
    if day:
        if not _prod_valid_date(day, default_today=False):
            return _fail("Invalid date (use YYYY-MM-DD, not in the future)")
        query = query.filter_by(date=day)
    return _ok({"days": _prod_history_days(query.all())})


@app.route('/api/production/monthly', methods=['GET'])
def api_production_monthly():
    entries = ProductionEntry.query.order_by(ProductionEntry.date.desc()).all()
    names = [p.name for p in Product.query.order_by(Product.id).all()]
    return _ok({"months": _prod_monthly_rows(entries, names), "products": names})


@app.route('/api/production', methods=['POST'])
def add_production():
    data = request.get_json(silent=True) or {}
    product = str(data.get('product', '')).strip()
    batch = str(data.get('batch', '')).strip()
    notes = str(data.get('notes', '')).strip()
    status = str(data.get('status', 'Completed')).strip() or 'Completed'
    quantity = _parse_int(data.get('quantity', 0))
    if not product or not batch or quantity <= 0:
        return _fail("Product, batch and a positive quantity are required")
    if ProductionEntry.query.filter_by(batch=batch).first():
        return _fail("Batch already exists", 409)
    machine = str(data.get('machine', 'Old Machine')).strip() or 'Old Machine'
    if machine not in ('Old Machine', 'New Machine'):
        return _fail("machine must be Old Machine or New Machine")
    entry = ProductionEntry(product=product, date=datetime.now().strftime('%Y-%m-%d'),
                            quantity=quantity, batch=batch, status=status, notes=notes,
                            machine=machine, consumed=0)
    db.session.add(entry)
    if status == 'Completed':
        prod = Product.query.filter_by(name=product).first()
        if prod:
            prod.stock += quantity
            prod.status = compute_product_status(prod.stock)
    db.session.add(StockMovement(date=datetime.now().strftime('%d %b %Y'), material=product,
                                 movement='Production In', quantity=quantity,
                                 unit='unit', reference=batch))
    db.session.commit()
    return jsonify({"success": True, "production": entry.to_dict()}), 201


@app.route('/api/production/bulk', methods=['POST'])
def api_production_bulk():
    """Save one day's upload for many products at once (daily production).

    Body: {date: 'YYYY-MM-DD' (default today), items: [{product, quantity}],
           notes: optional, status: optional (default Completed)}.
    Every stored line also lands in production history and the monthly rollup.
    """
    data = request.get_json(silent=True) or {}
    day = _prod_valid_date(data.get('date', ''), default_today=True)
    if not day:
        return _fail("Invalid production date (use YYYY-MM-DD, not in the future)")
    items = data.get('items', [])
    if not isinstance(items, list) or not items:
        return _fail("items must be a non-empty list")
    notes = str(data.get('notes', '')).strip()
    status = str(data.get('status', 'Completed')).strip() or 'Completed'
    machine = str(data.get('machine', 'Old Machine')).strip() or 'Old Machine'
    if machine not in ('Old Machine', 'New Machine'):
        return _fail("machine must be Old Machine or New Machine")
    created = []
    for item in items:
        if not isinstance(item, dict):
            continue
        product = str(item.get('product', '')).strip()
        quantity = _parse_int(item.get('quantity', 0))
        if not product or quantity <= 0:
            continue
        prod = Product.query.filter_by(name=product).first()
        if not prod:
            db.session.rollback()
            return _fail(f"Unknown product: {product}", 404)
        batch = _prod_next_batch(_prod_batch_prefix(product), day)
        entry = ProductionEntry(product=product, date=day, quantity=quantity,
                                batch=batch, status=status, notes=notes,
                                machine=machine, consumed=0)
        db.session.add(entry)
        if status == 'Completed':
            prod.stock += quantity
            prod.status = compute_product_status(prod.stock)
        db.session.add(StockMovement(date=_prod_display_date(day), material=product,
                                     movement='Production In', quantity=quantity,
                                     unit='unit', reference=batch))
        created.append(entry)
    if not created:
        return _fail("Nothing to save — enter a quantity above zero for at least one product")
    db.session.commit()
    return jsonify({"success": True, "date": day,
                    "productions": [e.to_dict() for e in created],
                    "day_total": sum(e.quantity for e in created)}), 201


@app.route('/api/production/<int:entry_id>', methods=['PUT'])
def api_production_update(entry_id):
    entry = ProductionEntry.query.get(entry_id)
    if not entry:
        return _fail("Production entry not found", 404)
    data = request.get_json(silent=True) or {}
    if 'quantity' in data:
        quantity = _parse_int(data['quantity'], entry.quantity)
        if quantity <= 0:
            return _fail("Quantity must be positive")
        entry.quantity = quantity
    if 'batch' in data:
        batch = str(data['batch']).strip()
        if not batch:
            return _fail("Batch cannot be empty")
        existing = ProductionEntry.query.filter_by(batch=batch).first()
        if existing and existing.id != entry.id:
            return _fail("Batch already exists", 409)
        entry.batch = batch
    if 'consumed' in data:
        try:
            consumed = int(data['consumed'])
        except (TypeError, ValueError):
            return _fail("consumed must be a number")
        if consumed < 0:
            return _fail("consumed cannot be negative")
        if consumed > entry.quantity:
            return _fail(f"consumed cannot exceed quantity ({entry.quantity})", 409)
        entry.consumed = consumed
    if 'status' in data:
        entry.status = str(data['status'])
    if 'notes' in data:
        entry.notes = str(data['notes'])
    db.session.commit()
    return _ok({"production": entry.to_dict()})


@app.route('/api/production/<int:entry_id>', methods=['DELETE'])
def api_production_delete(entry_id):
    entry = ProductionEntry.query.get(entry_id)
    if not entry:
        return _fail("Production entry not found", 404)
    db.session.delete(entry)
    db.session.commit()
    return _ok()


# ---------- Purchases API ----------
@app.route('/purchase-slips/<path:filename>')
def purchase_slip(filename):
    return _send_stored_file('purchases', filename, PURCHASE_UPLOAD_DIR)


@app.route('/api/purchases', methods=['GET'])
def api_purchases_list():
    status = request.args.get('status', 'all')
    query = Purchase.query.order_by(Purchase.id.desc())
    if status in ('Paid', 'Partial', 'Unpaid'):
        query = query.filter_by(status=status)
    return _ok({"purchases": [p.to_dict() for p in query.all()]})


def _admin_password_is_verified(password):
    """True only when the password matches the currently logged-in admin account."""
    uid = session.get('savi_admin_id')
    if not uid:
        return False
    row = db.session.get(User, uid)
    if not row or row.role != 'admin':
        return False
    try:
        return check_password_hash(row.password or '', password or '')
    except Exception:
        return False


@app.route('/api/stock-update/verify', methods=['POST'])
def api_stock_update_verify_password():
    data = request.get_json(silent=True) or {}
    if _admin_password_is_verified(data.get('password', '')):
        return _ok({"verified": True})
    return _fail("Incorrect admin password. Stock update was not allowed.", 401)


@app.route('/api/purchases/<int:purchase_id>/stock-update', methods=['POST'])
def api_purchase_stock_update(purchase_id):
    """Admin-only stock update for a purchase record. Re-verifies the admin
    password so a stale session can never change stock without authority."""
    data = request.get_json(silent=True) or {}
    if not _admin_password_is_verified(data.get('password', '')):
        return _fail("Incorrect admin password. Stock was not changed.", 401)
    purchase = db.session.get(Purchase, purchase_id)
    if not purchase:
        return _fail("Purchase record not found.", 404)
    quantity = str(data.get('quantity', '')).strip()
    if not quantity:
        return _fail("Quantity is required.")
    purchase.quantity = quantity
    db.session.commit()
    return _ok({"purchase": purchase.to_dict()})


@app.route('/api/purchases/bulk-stock', methods=['POST'])
def api_purchases_bulk_stock():
    """Bulk Add Stock for Purchase Maintenance — password SAVI STOCK, only Stock editable.
    Body: {items: [{id, quantity}]}. Updates Purchase.quantity for all items. Visible instantly in user portal via shared DB."""
    data = request.get_json(silent=True) or {}
    items = data.get('items', [])
    if not isinstance(items, list) or not items:
        return _fail("items required")
    # Optional: verify SAVI STOCK is not needed server-side (gate is client), but allow bulk without extra password for publish flow
    updated = 0
    for it in items:
        pid = it.get('id')
        qty = str(it.get('quantity', '')).strip()
        if not pid or qty == "":
            continue
        try:
            pid_int = int(pid)
        except:
            continue
        purchase = db.session.get(Purchase, pid_int)
        if not purchase:
            continue
        purchase.quantity = qty
        updated += 1
    if updated:
        db.session.commit()
    return _ok({"updated": updated})


@app.route('/api/purchases', methods=['POST'])
def api_purchases_create():
    return _fail("Purchase management is read-only on the admin panel. Use the user side to create purchases.", 403)
    data = request.get_json(silent=True) or {}
    vendor = str(data.get('vendor_name', '')).strip()
    vendor_phone = str(data.get('vendor_phone', '')).strip()
    v_number = str(data.get('v_number', '')).strip() or _next_v_number()
    if not vendor or not vendor_phone:
        return _fail("vendor_name and vendor_phone are required")
    if Purchase.query.filter_by(v_number=v_number).first():
        return _fail("Record number already exists", 409)
    amount = _parse_int(data.get('amount_paid', 0))
    remaining = _parse_int(data.get('remaining', 0))
    if amount < 0 or remaining < 0:
        return _fail("Amounts cannot be negative")
    status = str(data.get('status', '')).strip()
    if not status:
        status = 'Paid' if remaining == 0 else ('Unpaid' if amount == 0 else 'Partial')
    purchase = Purchase(
        vendor_name=vendor, vendor_phone=vendor_phone, address=str(data.get('address', '')),
        v_number=v_number, date_display=str(data.get('date_display', datetime.now().strftime('%d %b %Y'))),
        product=str(data.get('product', '')).strip(), quantity=str(data.get('quantity', '')).strip(),
        amount_paid=amount, remaining=remaining,
        payment_mode=str(data.get('payment_mode', 'Cash')), status=status,
    )
    db.session.add(purchase)

    # A purchase immediately increases the matching Resource Maintenance stock.
    # Quantity may be entered as "3000", "3000 kg", "3,000 Kg", etc.
    qty_text = purchase.quantity.replace(',', '')
    qty_match = re.search(r"[-+]?\d+(?:\.\d+)?", qty_text)
    purchase_qty = int(float(qty_match.group(0))) if qty_match else 0
    resource = None
    if purchase.product:
        # Match the Resource Maintenance item robustly. Exact/case-insensitive
        # matching is preferred, with a small typo-tolerant fallback so entries
        # such as "maiz" still update the existing "Maize" row.
        product_name = purchase.product.strip()
        resource = Resource.query.filter(Resource.name.ilike(product_name)).first()
        candidates = Resource.query.all()
        if not resource:
            normalized = re.sub(r"\s+", " ", product_name).strip().casefold()
            for candidate in candidates:
                candidate_norm = re.sub(r"\s+", " ", str(candidate.name or "")).strip().casefold()
                if candidate_norm == normalized:
                    resource = candidate
                    break
        if not resource and product_name:
            from difflib import SequenceMatcher
            compact = re.sub(r"[^a-z0-9]", "", product_name.casefold())
            best = None
            best_score = 0.0
            for candidate in candidates:
                candidate_compact = re.sub(r"[^a-z0-9]", "", str(candidate.name or "").casefold())
                if not candidate_compact:
                    continue
                score = SequenceMatcher(None, compact, candidate_compact).ratio()
                if score > best_score:
                    best_score, best = score, candidate
            if best_score >= 0.78:
                resource = best
    if resource and purchase_qty > 0:
        # Purchases go into their own counter, never into opening, so the
        # stock formula stays honest: closing = opening + purchased - consumed - sold.
        resource.purchased = (resource.purchased or 0) + purchase_qty
        resource.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
        resource.recompute()

    db.session.add(StockMovement(date=datetime.now().strftime('%d %b %Y'),
                                 material=purchase.product or vendor,
                                 movement='Purchase In', quantity=purchase_qty,
                                 unit='kg' if purchase_qty else 'lot', reference=v_number))
    db.session.commit()
    return jsonify({"success": True, "purchase": purchase.to_dict(),
                    "resource_updated": bool(resource and purchase_qty > 0),
                    "resource": resource.to_dict() if resource and purchase_qty > 0 else None}), 201


@app.route('/api/purchases/<int:purchase_id>', methods=['PUT'])
def api_purchases_update(purchase_id):
    return _fail("Purchase management is read-only on the admin panel. Use the user side to update purchases.", 403)
    purchase = Purchase.query.get(purchase_id)
    if not purchase:
        return _fail("Purchase not found", 404)
    data = request.get_json(silent=True) or {}
    for field in ('vendor_name', 'vendor_phone', 'address', 'product', 'quantity', 'payment_mode', 'status', 'date_display'):
        if field in data and data[field] is not None:
            setattr(purchase, field, str(data[field]))
    for field in ('amount_paid', 'remaining'):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if value < 0:
                return _fail(f"{field} cannot be negative")
            setattr(purchase, field, value)
    db.session.commit()
    return _ok({"purchase": purchase.to_dict()})


@app.route('/api/purchases/<int:purchase_id>', methods=['DELETE'])
def api_purchases_delete(purchase_id):
    return _fail("Purchase management is read-only on the admin panel. Use the user side to delete purchases.", 403)
    purchase = Purchase.query.get(purchase_id)
    if not purchase:
        return _fail("Purchase not found", 404)
    db.session.delete(purchase)
    db.session.commit()
    return _ok()


_MEDICINE_KEYWORDS = ['liv', 'toxi', 'mycin', 'probiotic', 'vitamin', 'medicine',
                      'tonic', 'binder', 'hepta', 'clostat', 'aleta', 'myco',
                      'relimax', 'meti', 'kemtrace', 'lexi', 'addarome']
_CHEMICAL_KEYWORDS = ['dcp', 'phosphate', 'calcium', 'salt', 'bentonite', 'soda',
                      'carbonate', 'choline', 'mineral', 'limestone', 'grit',
                      'chemical', 'dicalcium']


def _vendor_group(product_name):
    """Classify a vendor's product into Raw Materials / Medicines / Chemicals."""
    name = (product_name or '').strip().lower()
    if not name:
        return 'Raw Materials'
    med_names = [r.name.lower() for r in Resource.query.filter_by(group='Medicines').all()]
    chem_names = [r.name.lower() for r in Resource.query.filter_by(group='Chemicals').all()]
    for rn in med_names:
        if rn in name or name in rn:
            return 'Medicines'
    for rn in chem_names:
        if rn in name or name in rn:
            return 'Chemicals'
    if any(k in name for k in _MEDICINE_KEYWORDS):
        return 'Medicines'
    if any(k in name for k in _CHEMICAL_KEYWORDS):
        return 'Chemicals'
    return 'Raw Materials'


@app.context_processor
def inject_nav_products():
    try:
        names = [p.name for p in Product.query.order_by(Product.id).all()]
        # The six built-in products always remain visible in Production Update.
        standard_names = {
            'Savi Gold Plus', 'Promin Gold', 'Promin Plus',
            'Promin', 'Savi Gold', 'Savi Supreme'
        }
        # New products created from Production Formula become visible in
        # Production Update only after a formula has been saved for them.
        production_names = []
        for p in Product.query.order_by(Product.id).all():
            if p.name in standard_names:
                production_names.append(p.name)
                continue
            saved_formula = FormulaSheet.query.filter(
                FormulaSheet.product == p.name,
                or_(FormulaSheet.old_machine > 0, FormulaSheet.new_machine > 0)
            ).first()
            if saved_formula:
                production_names.append(p.name)

        # 3rd-party products become visible in Production Update and Navigation.
        third_names = [t.name for t in ThirdPartyItem.query.order_by(ThirdPartyItem.name).all()]
    except Exception:
        names, production_names, third_names = [], [], []
    return {
        "nav_products": names,
        "nav_product_ids": {p.name: p.id for p in Product.query.order_by(Product.id).all()},
        "nav_production_products": production_names,
        "nav_third_party": third_names,
        "nav_third_party_ids": {t.name: t.id for t in ThirdPartyItem.query.order_by(ThirdPartyItem.name).all()}
    }



def _formula_product_exists(name):
    return bool(Product.query.filter_by(name=name).first() or ThirdPartyItem.query.filter_by(name=name).first())


@app.route('/formulas')
def formulas():
    products = [p.name for p in Product.query.order_by(Product.id).all()]
    selected_list = request.args.get('list', '').strip()
    third_party = ThirdPartyItem.query.order_by(ThirdPartyItem.id.desc()).all()
    third_names = {t.name for t in third_party}
    if selected_list not in products and selected_list not in third_names and selected_list != '3rd Party Product':
        selected_list = products[0] if products else (next(iter(third_names)) if third_names else 'Raw Materials')
    # Server-render the Publish button's initial state so a slow/failed
    # loadPublishState() fetch can never show the wrong state (a blind click
    # on a stale "Publish" label would silently unpublish the product).
    if selected_list in third_names:
        publish_initial = any(bool(t.published) for t in third_party if t.name == selected_list)
    else:
        _sel_product = Product.query.filter_by(name=selected_list).first()
        publish_initial = bool(_sel_product and _sel_product.published)
    # Only show materials that have actually been added to this product formula.
    # The category dropdowns are used to add new materials.
    sheet_rows = [r.to_dict() for r in FormulaSheet.query.filter_by(product=selected_list).order_by(FormulaSheet.id).all()]
    last_updated = max((s["updated"] for s in sheet_rows if s.get("updated")), default="")
    # Live stock status per dropdown material (Critical -> red, Low Stock -> orange).
    formula_statuses = {}
    _res_by_group_name = {(r.group, r.name): r for r in Resource.query.all()}
    for _cat, _mats in FORMULA_CATEGORIES.items():
        for _m in _mats:
            _row = _res_by_group_name.get((_cat, _m))
            if _row is not None:
                formula_statuses[_m] = compute_resource_status(_row.closing, _row.min_stock or 250)
    return render_template('formulas.html',
        products=products,
        sections=list(FORMULA_CATEGORIES.keys()),
        formula_categories=FORMULA_CATEGORIES,
        formula_statuses=formula_statuses,
        active_section='All',
        third_party=[t.to_dict() for t in third_party],
        third_names=list(third_names),
        selected_list=selected_list,
        publish_initial=publish_initial,
        sheet_rows=sheet_rows,
        last_updated=last_updated,
        sheet_total_old=round(sum(s["old_machine"] for s in sheet_rows), 2),
        sheet_total_new=round(sum(s["new_machine"] for s in sheet_rows), 2),
        active_page='formulas')


# ---------- Formula sheet API (inline edit + double + update all) ----------
@app.route('/api/formula-sheet/item', methods=['POST'])
def api_formula_sheet_add_item():
    data = request.get_json(silent=True) or {}
    product = str(data.get('product', '')).strip()
    category = str(data.get('category', '')).strip()
    material = str(data.get('material', '')).strip()
    if not product or not category or not material:
        return _fail("product, category and material are required")
    if product != '3rd Party Product' and not _formula_product_exists(product):
        return _fail("Unknown product", 404)
    if category not in FORMULA_CATEGORIES or material not in FORMULA_CATEGORIES[category]:
        return _fail("Invalid category or material", 400)
    crit = Resource.query.filter_by(group=category, name=material).first()
    if crit is not None and compute_resource_status(crit.closing or 0, crit.min_stock or 250) == 'Critical':
        return _fail("This item is critical in stock so you can not add to formula", 400)
    existing = FormulaSheet.query.filter_by(product=product, category=category, material=material).first()
    if existing:
        return _ok({"row": existing.to_dict(), "already_exists": True})
    row = FormulaSheet(product=product, category=category, material=material, old_machine=0, new_machine=0,
                       updated=datetime.now().strftime('%d %b %Y'))
    db.session.add(row)
    db.session.commit()
    return _ok({"row": row.to_dict(), "already_exists": False})


@app.route('/api/formula-sheet/item', methods=['DELETE'])
def api_formula_sheet_delete_item():
    data = request.get_json(silent=True) or {}
    product = str(data.get('product', '')).strip()
    category = str(data.get('category', '')).strip()
    material = str(data.get('material', '')).strip()
    row = FormulaSheet.query.filter_by(product=product, category=category, material=material).first()
    if not row:
        return _fail("Formula item not found", 404)
    db.session.delete(row)
    db.session.commit()
    return _ok()


@app.route('/api/formula-sheet/clear', methods=['POST'])
def api_formula_sheet_clear():
    data = request.get_json(silent=True) or {}
    product = str(data.get('product', '')).strip()
    if not product:
        return _fail("product is required")
    if product != '3rd Party Product' and not _formula_product_exists(product):
        return _fail("Unknown product", 404)
    deleted = FormulaSheet.query.filter_by(product=product).delete()
    db.session.commit()
    return _ok({"deleted": deleted})


@app.route('/api/formula-sheet', methods=['GET'])
def api_formula_sheet():
    product = request.args.get('product', '').strip()
    section = request.args.get('section', 'Raw Materials').strip() or 'Raw Materials'
    if section not in FORMULA_CATEGORIES:
        return _fail("Unknown section")
    if product != '3rd Party Product' and not _formula_product_exists(product):
        return _fail("Unknown product", 404)
    rows = FormulaSheet.query.filter_by(product=product, category=section).order_by(FormulaSheet.id).all()
    dicts = [r.to_dict() for r in rows]
    material_map = {d["material"]: d for d in dicts}
    full_rows = []
    for m in FORMULA_CATEGORIES[section]:
        if m in material_map:
            full_rows.append(material_map[m])
        else:
            full_rows.append({"material": m, "old_machine": 0, "new_machine": 0})
    return _ok({"product": product, "section": section, "rows": full_rows,
                "total_old": round(sum(d["old_machine"] for d in full_rows), 2),
                "total_new": round(sum(d["new_machine"] for d in full_rows), 2)})


@app.route('/api/formula-sheet', methods=['PUT'])
def api_formula_sheet_update():
    data = request.get_json(silent=True) or {}
    product = str(data.get('product', '')).strip()
    if not product:
        return _fail("product and a valid section are required")
    if product != '3rd Party Product' and not _formula_product_exists(product):
        return _fail("Unknown product", 404)
    section = str(data.get('section', 'Raw Materials')).strip() or 'Raw Materials'

    def _parse_num(value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        return value

    # Replace mode (the formula page's Save button): the payload IS the
    # product's complete formula. Every existing row (all categories) is
    # deleted first so categories that lost all their items disappear too,
    # and quantity is mandatory for every item before anything is stored.
    if data.get('replace') and isinstance(data.get('rows'), list):
        new_rows = []
        seen = set()
        for item in data['rows']:
            if not isinstance(item, dict):
                continue
            material = str(item.get('material', '')).strip()
            category = str(item.get('category', '')).strip()
            if not material or not category:
                return _fail("Each item needs a material and a category")
            if category not in FORMULA_CATEGORIES or material not in FORMULA_CATEGORIES[category]:
                return _fail("Invalid category or material", 400)
            if (category, material) in seen:
                return _fail("Duplicate item in formula: " + material, 400)
            seen.add((category, material))
            old_val = _parse_num(item.get('old_machine', 0))
            new_val = _parse_num(item.get('new_machine', 0))
            if old_val is None or new_val is None:
                return _fail("old_machine and new_machine must be non-negative numbers")
            if old_val <= 0 or new_val <= 0:
                return _fail("Enter quantity for all items before saving", 400)
            new_rows.append((category, material, old_val, new_val))
        FormulaSheet.query.filter_by(product=product).delete()
        stamp = datetime.now().strftime('%d %b %Y')
        for category, material, old_val, new_val in new_rows:
            db.session.add(FormulaSheet(product=product, category=category, material=material,
                                         old_machine=old_val, new_machine=new_val, updated=stamp))
        db.session.commit()
        rows = FormulaSheet.query.filter_by(product=product).all()
        # Change #47 item 1 (2026-10-07): SAVE must push the sheet to the user
        # side too. Syncing used to happen ONLY on the publish toggle - the
        # Save button skips that call while the product is already published
        # (blind toggle would UNpublish it), so an admin who edited an already
        # published formula saw nothing change on the user's Production Update
        # page. A non-empty sheet syncs; an empty one never wipes PM rows.
        if rows:
            _sync_formula_to_product_materials(product)
        return _ok({"product": product, "saved": len(rows),
                    "total_old": round(sum(r.old_machine or 0 for r in rows), 2),
                    "total_new": round(sum(r.new_machine or 0 for r in rows), 2)})

    if section not in FORMULA_CATEGORIES:
        return _fail("product and a valid section are required")

    def _apply(material, old_val, new_val):
        row = FormulaSheet.query.filter_by(product=product, category=section, material=material).first()
        if not row:
            row = FormulaSheet(product=product, category=section, material=material)
            db.session.add(row)
        row.old_machine = old_val
        row.new_machine = new_val
        row.updated = datetime.now().strftime('%d %b %Y')

    if isinstance(data.get('rows'), list):
        for item in data['rows']:
            if not isinstance(item, dict):
                continue
            material = str(item.get('material', '')).strip()
            if not material:
                continue
            old_val = _parse_num(item.get('old_machine', 0))
            new_val = _parse_num(item.get('new_machine', 0))
            if old_val is None or new_val is None:
                return _fail("old_machine and new_machine must be non-negative numbers")
            _apply(material, old_val, new_val)
        db.session.commit()
    else:
        material = str(data.get('material', '')).strip()
        if not material:
            return _fail("material is required")
        old_val = _parse_num(data.get('old_machine', 0))
        new_val = _parse_num(data.get('new_machine', 0))
        if old_val is None or new_val is None:
            return _fail("old_machine and new_machine must be non-negative numbers")
        _apply(material, old_val, new_val)
        db.session.commit()

    # Change #47 item 1: same sync as the replace path above - a section edit
    # must reach the user's Production Update page immediately too.
    if FormulaSheet.query.filter_by(product=product).first():
        _sync_formula_to_product_materials(product)

    rows = FormulaSheet.query.filter_by(product=product, category=section).order_by(FormulaSheet.id).all()
    material_map = {}
    for row in rows:
        material_map[row.material] = row.to_dict()
    full_rows = []
    for m in FORMULA_CATEGORIES[section]:
        if m in material_map:
            full_rows.append(material_map[m])
        else:
            full_rows.append({"material": m, "old_machine": 0, "new_machine": 0})
    return _ok({"product": product, "section": section, "rows": full_rows,
                "total_old": round(sum(d["old_machine"] for d in full_rows), 2),
                "total_new": round(sum(d["new_machine"] for d in full_rows), 2)})


# ---------- Formulas API ----------
@app.route('/api/formulas', methods=['GET'])
def api_formulas_list():
    return _ok({"formulas": [f.to_dict() for f in Formula.query.order_by(Formula.id.desc()).all()]})


@app.route('/api/formulas', methods=['POST'])
def api_formulas_create():
    import json as _json
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    if not name:
        return _fail("Formula name is required")
    if Formula.query.filter_by(name=name).first():
        return _fail("Formula already exists", 409)
    raw_lines = data.get('ingredients', [])
    if not isinstance(raw_lines, list):
        return _fail("ingredients must be a list")
    lines = []
    for ln in raw_lines:
        if not isinstance(ln, dict):
            continue
        material = str(ln.get('material', '')).strip()
        category = str(ln.get('category', '')).strip()
        try:
            old_q = float(ln.get('old_machine', 0) or 0)
            new_q = float(ln.get('new_machine', 0) or 0)
        except (TypeError, ValueError):
            return _fail(f"Machine quantities for '{material or '?'}' must be numbers")
        # F4: float("nan") and float("1e999") parse without error but are not
        # valid quantities. Reject non-finite values before the <=0 check
        # (nan <= 0 is False, so they would otherwise slip through and be
        # stored, later crashing arithmetic and exports downstream).
        if (old_q != old_q or old_q in (float('inf'), float('-inf'))
                or new_q != new_q or new_q in (float('inf'), float('-inf'))):
            return _fail(f"Machine quantities for '{material or '?'}' must be finite numbers")
        if not material or (old_q <= 0 and new_q <= 0):
            continue
        lines.append({"category": category, "material": material,
                      "old_machine": old_q, "new_machine": new_q})
    if not lines:
        return _fail("Add at least one ingredient with a machine quantity above zero")
    record = Formula(
        name=name, product=str(data.get('product', '')).strip(),
        ingredients=_json.dumps(lines),
        notes=str(data.get('notes', '')).strip(),
        created=datetime.now().strftime('%d %b %Y'),
    )
    product_name = str(data.get('product', '')).strip()
    for ln in lines:
        existing = FormulaSheet.query.filter_by(product=product_name, category=ln.get('category',''), material=ln.get('material','')).first()
        if not existing:
            db.session.add(FormulaSheet(
                product=product_name, category=ln.get('category',''),
                material=ln.get('material',''),
                old_machine=ln.get('old_machine',0), new_machine=ln.get('new_machine',0),
                updated=datetime.now().strftime('%d %b %Y')
            ))
    db.session.add(record)
    db.session.commit()
    return jsonify({"success": True, "formula": record.to_dict()}), 201


@app.route('/api/formulas/<int:formula_id>', methods=['DELETE'])
def api_formulas_delete(formula_id):
    record = Formula.query.get(formula_id)
    if not record:
        return _fail("Formula not found", 404)
    db.session.delete(record)
    db.session.commit()
    return _ok()


# ---------- Third-party items API ----------
@app.route('/api/third-party-items', methods=['GET'])
def api_third_party_list():
    name_filter = request.args.get('name', '').strip()
    query = ThirdPartyItem.query.order_by(ThirdPartyItem.id.desc())
    if name_filter:
        query = query.filter(ThirdPartyItem.name.ilike(name_filter))
    return _ok({"items": [t.to_dict() for t in query.all()]})


@app.route('/api/third-party-items', methods=['POST'])
def api_third_party_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    if not name:
        return _fail("Item name is required")
    if ThirdPartyItem.query.filter_by(name=name).first():
        return _fail("A 3rd party product with this name already exists")
    try:
        quantity = float(data.get('quantity', 0) or 0)
        price = float(data.get('price', 0) or 0)
    except (TypeError, ValueError):
        return _fail("Quantity and price must be numbers")
    if quantity < 0 or price < 0:
        return _fail("Quantity and price cannot be negative")
    import json as _json
    mats = []
    for rm in (data.get('materials', []) or []):
        if not isinstance(rm, dict):
            continue
        mat = str(rm.get('material', '')).strip()
        if not mat:
            continue
        try:
            opening = float(rm.get('opening', 0) or 0)
        except (TypeError, ValueError):
            return _fail(f"Opening for '{mat}' must be a number")
        mats.append({"material": mat, "opening": opening})
    record = ThirdPartyItem(
        name=name,
        category=str(data.get('category', 'Raw Materials')).strip() or 'Raw Materials',
        quantity=quantity, unit=str(data.get('unit', 'kg')).strip() or 'kg',
        materials=_json.dumps(mats), price=price,
        notes=str(data.get('notes', '')).strip(),
        created=datetime.now().strftime('%d %b %Y'),
        published=True,
    )
    db.session.add(record)
    # Item 1 (2026-10-05): a NEW 3rd-party product must open with an EMPTY
    # formula sheet. This block used to auto-insert a 0-qty FormulaSheet row
    # for EVERY material in EVERY category, so the new product immediately
    # showed the whole material list instead of staying blank until the admin
    # adds items (same rule Change #27 applied to api_products_create).
    db.session.commit()
    return jsonify({"success": True, "item": record.to_dict()}), 201


@app.route('/api/third-party-items/<int:item_id>', methods=['PUT'])
def api_third_party_update(item_id):
    """Item 9 (2026-10-05): per-item edit so Product Details can store a price
    for ONE 3rd-party row. Only price is accepted for now - the list endpoint
    and the create route carry nothing else worth editing inline."""
    record = ThirdPartyItem.query.get(item_id)
    if not record:
        return _fail("Item not found", 404)
    data = request.get_json(silent=True) or {}
    if 'price' in data:
        try:
            price = float(data['price'])
        except (TypeError, ValueError):
            return _fail("Price must be a number")
        if price < 0:
            return _fail("Price cannot be negative")
        record.price = price
    db.session.commit()
    return _ok({"item": record.to_dict()})


@app.route('/api/third-party-items/<int:item_id>', methods=['DELETE'])
def api_third_party_delete(item_id):
    record = ThirdPartyItem.query.get(item_id)
    if not record:
        return _fail("Item not found", 404)
    # Item 2 (2026-10-05): the dashboard card now offers this delete, so it has
    # to clean up the same related rows api_products_delete does - otherwise a
    # deleted 3rd-party product leaves its formula sheet and production history
    # behind (and a later item with the same name resurrects them).
    name = record.name
    ProductMaterial.query.filter_by(product_name=name).delete()
    MaterialDaily.query.filter_by(product_name=name).delete()
    FormulaSheet.query.filter_by(product=name).delete()
    ProductionEntry.query.filter_by(product=name).delete()
    MachineProductionEntry.query.filter_by(product_name=name).delete()
    ConsumptionLog.query.filter_by(product_name=name).delete()
    _pm_hist.pop(name, None)
    db.session.delete(record)
    db.session.commit()
    return _ok()


@app.route('/api/third-party-items/<int:item_id>/upload-image', methods=['POST'])
def api_third_party_upload_image(item_id):
    item = ThirdPartyItem.query.get(item_id)
    if not item:
        return _fail("3rd Party item not found", 404)
    if 'image' not in request.files:
        return _fail("No image file provided", 400)
    file = request.files['image']
    if file.filename == '':
        return _fail("No file selected", 400)
    if not file.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
        return _fail('Only JPG/PNG/WEBP images are allowed', 400)
    # Same hard 2 MB cap + content-sniffed extension as the product endpoint.
    data = _read_capped_upload(file)
    if not data:
        return _fail("The uploaded file is empty.", 400)
    if len(data) > PRODUCT_IMAGE_MAX_BYTES:
        return _fail(
            f"Image is too large (over {PRODUCT_IMAGE_MAX_BYTES // (1024 * 1024)} MB). "
            "Please choose a smaller image.",
            400
        )
    ext = _sniff_image_ext(data)
    if not ext:
        return _fail('That file is not a valid JPG, PNG or WEBP image.', 400)
    prefix = "".join(w[0] for w in item.name.split() if w).upper()[:3] or "TP"
    filename = f"{prefix}_tp{item.id}.{ext}"
    if storage_status() == 'unconfigured':
        item.image = filename
        item.image_data = data
        item.image_mime = _image_mime_for_ext(ext)
        db.session.commit()
        return _ok({"item": item.to_dict(), "image": filename, "stored": "database"})
    try:
        save_upload(_BytesFileStorage(filename, data), 'products', filename,
                    {'.jpg', '.jpeg', '.png', '.webp'})
    except ValueError as exc:
        return _fail(str(exc), 400)
    except (FileStorageUnavailable, FileStorageError) as exc:
        return _fail(str(exc), 503)
    item.image = filename
    item.image_data = None
    item.image_mime = None
    db.session.commit()
    return _ok({"item": item.to_dict(), "image": filename})


def vendor_product_groups(v):
    """Return [{'group', 'products': [...]}] for display of a vendor's products."""
    raw = (v.get('products') if isinstance(v, dict) else getattr(v, 'products', '')) or ''
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return [{"group": g, "products": [str(p) for p in items]}
                    for g, items in parsed.items() if items]
        if isinstance(parsed, list):
            return [{"group": "Products", "products": [str(p) for p in parsed]}]
    name = (v.get('product') if isinstance(v, dict) else getattr(v, 'product', '')) or ''
    items = [p.strip() for p in name.split(',') if p.strip()]
    if not items:
        return []
    if isinstance(v, dict):
        cat = v.get('category', '')
    else:
        cat = getattr(v, 'category', '')
    key = cat if cat in ("Raw Materials", "Medicines", "Chemicals") else "Products"
    return [{"group": key, "products": items}]


app.jinja_env.globals['vendor_product_groups'] = vendor_product_groups


@app.route('/vendors')
def vendors():
    status_filter = request.args.get('status', 'all')
    if status_filter not in {'all', 'Paid', 'Partial', 'Unpaid'}:
        status_filter = 'all'
    category_filter = request.args.get('category', 'all')
    if category_filter not in VENDOR_CATEGORIES:
        category_filter = 'all'
    query = Vendor.query.order_by(Vendor.id.desc())
    if status_filter != 'all':
        query = query.filter_by(status=status_filter)
    if category_filter != 'all':
        query = query.filter_by(category=category_filter)
    rows = []
    for v in query.all():
        d = v.to_dict()
        d['group'] = _vendor_group(v.product)
        rows.append(d)
    all_vendor_names = sorted(
        v[0] for v in db.session.query(Vendor.vendor_name).distinct().all())
    return render_template('vendors.html',
        vendors=rows, all_vendor_names=all_vendor_names,
        status_filter=status_filter, category_filter=category_filter,
        active_page='vendors')


# ---------- Vendors API (Master Data) ----------
@app.route('/api/vendors', methods=['GET'])
def api_vendors_list():
    status = request.args.get('status', 'all')
    category = request.args.get('category', 'all')
    query = Vendor.query.order_by(Vendor.id.desc())
    if status in ('Paid', 'Partial', 'Unpaid'):
        query = query.filter_by(status=status)
    if category in VENDOR_CATEGORIES:
        query = query.filter_by(category=category)
    return _ok({"vendors": [v.to_dict() for v in query.all()]})


@app.route('/api/vendors', methods=['POST'])
def api_vendors_create():
    return _fail("Vendor management is read-only on the admin panel. Use the user side to create vendors.", 403)
    data = request.get_json(silent=True) or {}
    vendor = str(data.get('vendor_name', '')).strip()
    phone = str(data.get('vendor_phone', '')).strip()
    if not vendor:
        return _fail("vendor_name is required")
    gstin = _valid_gstin(data.get('gstin', ''))
    if not gstin:
        return _fail("GSTIN must be exactly 15 alphanumeric characters")
    amount = _parse_int(data.get('amount_paid', 0))
    remaining = _parse_int(data.get('remaining', 0))
    if amount < 0 or remaining < 0:
        return _fail("Amounts cannot be negative")
    status = str(data.get('status', '')).strip()
    if not status:
        status = 'Paid' if remaining == 0 else ('Unpaid' if amount == 0 else 'Partial')
    category = str(data.get('category', '')).strip() or _vendor_category_for(data.get('product', ''))
    if category not in VENDOR_CATEGORIES:
        category = 'Raw Materials'
    record = Vendor(
        vendor_name=vendor, vendor_phone=phone, gstin=gstin, address=str(data.get('address', '')),
        date_display=str(data.get('date_display', datetime.now().strftime('%d %b %Y'))),
        category=category,
        product=str(data.get('product', '')), quantity=str(data.get('quantity', '')),
        amount_paid=amount, remaining=remaining,
        payment_mode=str(data.get('payment_mode', 'Cash')), status=status,
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({"success": True, "vendor": record.to_dict()}), 201


@app.route('/api/vendors/<int:vendor_id>', methods=['PUT'])
def api_vendors_update(vendor_id):
    return _fail("Vendor management is read-only on the admin panel. Use the user side to update vendors.", 403)
    record = Vendor.query.get(vendor_id)
    if not record:
        return _fail("Vendor not found", 404)
    data = request.get_json(silent=True) or {}
    for field in ('vendor_name', 'vendor_phone', 'address', 'product', 'quantity',
                  'payment_mode', 'status', 'date_display'):
        if field in data and data[field] is not None:
            setattr(record, field, str(data[field]))
    if 'category' in data and str(data['category']).strip() in VENDOR_CATEGORIES:
        record.category = str(data['category']).strip()
    if 'gstin' in data and str(data['gstin']).strip() != '':
        gstin = _valid_gstin(data['gstin'])
        if not gstin:
            return _fail("GSTIN must be exactly 15 alphanumeric characters")
        record.gstin = gstin
    for field in ('amount_paid', 'remaining'):
        if field in data:
            try:
                value = int(data[field])
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if value < 0:
                return _fail(f"{field} cannot be negative")
            setattr(record, field, value)
    db.session.commit()
    return _ok({"vendor": record.to_dict()})


@app.route('/api/vendors/<int:vendor_id>', methods=['DELETE'])
def api_vendors_delete(vendor_id):
    return _fail("Vendor management is read-only on the admin panel. Use the user side to delete vendors.", 403)
    record = Vendor.query.get(vendor_id)
    if not record:
        return _fail("Vendor not found", 404)
    db.session.delete(record)
    db.session.commit()
    return _ok()


# ---------- Settings API ----------
@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    return _ok({"settings": _get_settings().to_dict()})


@app.route('/api/settings', methods=['PUT'])
def api_settings_update():
    record = _get_settings()
    data = request.get_json(silent=True) or {}
    for field in ('company_name', 'location', 'currency_symbol', 'allocation_rule'):
        if field in data and str(data[field]).strip():
            setattr(record, field, str(data[field]).strip())
    for field in ('low_stock_notifications', 'daily_digest'):
        if field in data:
            setattr(record, field, bool(data[field]))
    db.session.commit()
    return _ok({"settings": record.to_dict()})


# ---------- Backup / Export / Notifications ----------
def _backup_payload():
    settings = _get_settings()
    products = [p.to_dict() for p in Product.query.order_by(Product.id).all()]
    product_materials = [{
        **m.to_dict(), "product_name": m.product_name,
    } for m in ProductMaterial.query.order_by(ProductMaterial.id).all()]
    resources = [{
        **r.to_dict(), "group": r.group, "item_category": r.item_category,
    } for r in Resource.query.order_by(Resource.id).all()]
    raw_materials = [m.to_dict() for m in RawMaterial.query.order_by(RawMaterial.id).all()]
    production = [p.to_dict() for p in ProductionEntry.query.order_by(
        ProductionEntry.date.desc(), ProductionEntry.id.desc()).all()]
    purchases = [{
        **p.to_dict(), "amount_paid": p.amount_paid, "remaining": p.remaining,
    } for p in Purchase.query.order_by(Purchase.id.desc()).all()]
    movements = [m.to_dict() for m in StockMovement.query.order_by(StockMovement.id.desc()).all()]
    daily_history = [d.to_dict() for d in MaterialDaily.query.order_by(
        MaterialDaily.date.desc(), MaterialDaily.id).all()]
    vendors = [{
        **v.to_dict(), "amount_paid": v.amount_paid, "remaining": v.remaining,
    } for v in Vendor.query.order_by(Vendor.id).all()]
    formulas = [f.to_dict() for f in Formula.query.order_by(Formula.id).all()]
    third_party_items = [t.to_dict() for t in ThirdPartyItem.query.order_by(ThirdPartyItem.id).all()]
    consumption_logs = [l.to_dict() for l in ConsumptionLog.query.order_by(ConsumptionLog.id).all()]

    stock_value = sum(p["price"] * p["stock"] for p in products)
    outstanding = sum(p["remaining"] for p in purchases)
    alerts = [
        {"material": r["name"], "on_hand": r["closing"], "minimum": r["min_stock"], "status": r["status"]}
        for r in resources if r["status"] in ("Low Stock", "Critical")
    ]
    for p in products:
        if p["status"] in ("Low Stock", "Critical"):
            alerts.append({"material": p["name"], "on_hand": p["stock"], "minimum": 2000, "status": p["status"]})

    summary = {
        "Total products": len(products),
        "Total resources": len(resources),
        "Total stock value (Rs)": f"{stock_value:,}",
        "Low stock products": sum(1 for p in products if p["status"] in ("Low Stock", "Critical")),
        "Items needing attention": len(alerts),
        "Production entries": len(production),
        "Purchase records": len(purchases),
        "Outstanding amount (Rs)": f"{outstanding:,}",
    }
    return {
        "company": settings.company_name,
        "generated_at": backup_label(),
        "summary": summary,
        "products": products,
        "product_materials": product_materials,
        "resources": resources,
        "raw_materials": raw_materials,
        "production": production,
        "purchases": purchases,
        "movements": movements,
        "vendors": vendors,
        "formulas": formulas,
        "third_party_items": third_party_items,
        "consumption_logs": consumption_logs,
        "daily_history": daily_history,
        "alerts": alerts,
        "settings": settings.to_dict(),
    }


def _run_daily_backup(slot='manual'):
    """Create the daily Excel/PDF/JSON/ZIP backup, copy it to the Google Drive
    synced folder, and record the result on the settings row.

    slot = 'manual' | 'startup' | '00:00' | '18:00'
    """
    record = _get_settings()
    today = date.today().isoformat()
    last_slot = record.backup_last_slot or ''

    if slot != 'manual' and not record.backup_enabled:
        return {"skipped": True, "message": "Automatic backup is off."}
    if slot == 'startup' and last_slot.startswith(today):
        return {"skipped": True, "message": "Backup already saved today."}
    if slot in ('00:00', '18:00') and last_slot == today + '|' + slot:
        return {"skipped": True, "message": "Backup already saved for this slot."}

    started = backup_label()
    try:
        payload = _backup_payload()
        local_dir = backup_service.resolve_backup_dir(record.backup_local_dir)
        result = backup_service.write_daily_backup(
            payload, local_dir, slot, record.backup_keep_days or backup_service.DEFAULT_KEEP_DAYS)

        messages = ["Saved %d files to %s" % (len(result["files"]), result["folder"])]
        drive = None
        drive_dir = (record.backup_drive_dir or '').strip()
        if drive_dir:
            drive_check = backup_service.check_drive_folder(drive_dir)
            if not drive_check["ok"]:
                messages.append(drive_check["message"] + " Local copy was saved.")
            else:
                try:
                    drive = backup_service.copy_to_drive(result, drive_dir)
                    if drive["verified"]:
                        messages.append("Copied to Google Drive folder: %s" % drive["folder"])
                        backup_service.prune_old_backups(drive_dir, record.backup_keep_days
                                                         or backup_service.DEFAULT_KEEP_DAYS)
                    else:
                        messages.append("Google Drive copy incomplete: %s (local copy saved)" % (
                            ", ".join(drive["failed"]) or "unknown error"))
                except Exception as drive_exc:
                    messages.append("Google Drive copy failed: %s (local copy saved)" % drive_exc)
        else:
            messages.append("Google Drive folder not set (local copy only).")

        status = " | ".join(messages)
        record.backup_last_run = started
        record.backup_last_slot = today + '|' + slot
        record.backup_last_status = status[:500]
        db.session.commit()
        return {
            "skipped": False,
            "slot": slot,
            "local": result,
            "drive": drive,
            "last_run": started,
            "message": status,
        }
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            record = _get_settings()
            record.backup_last_run = started
            record.backup_last_status = ("Backup failed: %s" % exc)[:500]
            db.session.commit()
        except Exception:
            db.session.rollback()
        raise


def start_backup_scheduler():
    """Start the 12:00 AM / 6:00 PM backup thread (once per process)."""
    def _runner(slot):
        with app.app_context():
            return _run_daily_backup(slot)
    return backup_service.start_scheduler(_runner)


@app.route('/api/backup/run', methods=['POST'])
def api_backup_run():
    try:
        data = request.form if request.form else (request.get_json(silent=True) or {})
        record = _get_settings()
        changed = False
        if 'backup_local_dir' in data:
            record.backup_local_dir = str(data.get('backup_local_dir') or '').strip()[:500]
            changed = True
        if 'backup_drive_dir' in data:
            record.backup_drive_dir = str(data.get('backup_drive_dir') or '').strip()[:500]
            changed = True
        if 'backup_keep_days' in data:
            try:
                keep_days = int(data.get('backup_keep_days') or 30)
            except (TypeError, ValueError):
                keep_days = 30
            record.backup_keep_days = max(1, min(keep_days, 3650))
            changed = True
        if 'backup_enabled' in data:
            record.backup_enabled = str(data.get('backup_enabled')).strip().lower() in (
                '1', 'true', 'on', 'yes')
            changed = True
        if changed:
            db.session.commit()
        result = _run_daily_backup('manual')
        return _ok({"result": result})
    except Exception as exc:
        return _fail("Backup failed: %s" % exc, 500)


@app.route('/api/backup/status', methods=['GET'])
def api_backup_status():
    record = _get_settings()
    return _ok({
        "default_dir": backup_service.default_backup_dir(),
        "backup": {
            "enabled": bool(record.backup_enabled) if record.backup_enabled is not None else True,
            "local_dir": record.backup_local_dir or "",
            "drive_dir": record.backup_drive_dir or "",
            "keep_days": record.backup_keep_days or backup_service.DEFAULT_KEEP_DAYS,
            "last_run": record.backup_last_run or "",
            "last_status": record.backup_last_status or "",
        },
    })


@app.route('/api/backup', methods=['GET'])
def api_backup():
    import io
    from flask import send_file
    payload = _backup_payload()
    filename, data = build_backup_zip(payload, backup_stamp())
    return send_file(io.BytesIO(data), mimetype="application/zip",
                     as_attachment=True, download_name=filename)


@app.route('/api/export/excel', methods=['GET'])
def api_export_excel():
    import io
    from flask import send_file
    payload = _backup_payload()
    data = build_excel_bytes(payload)
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"savi_export_{backup_stamp()}.xlsx")


@app.route('/api/export/pdf', methods=['GET'])
def api_export_pdf():
    import io
    from flask import send_file
    payload = _backup_payload()
    data = build_pdf_bytes(payload)
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=True,
                     download_name=f"savi_report_{backup_stamp()}.pdf")


@app.route('/api/production-formula/export', methods=['GET'])
def api_export_production_formula():
    import io
    from flask import send_file, request
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    product_name = request.args.get('product_name', '').strip()
    if not product_name:
        return jsonify({"error": "product_name required"}), 400

    product = Product.query.filter_by(name=product_name).first()
    if not product:
        return jsonify({"error": "Product not found"}), 404

    rows = FormulaSheet.query.filter_by(product=product_name).order_by(FormulaSheet.id).all()
    materials = []
    for row in rows:
        materials.append({
            "material": row.material,
            "category": row.category,
            "old_machine": row.old_machine or 0,
            "new_machine": row.new_machine or 0,
        })

    wb = Workbook()
    ws = wb.active
    ws.title = "Production Formula"

    # Header
    ws["A1"] = "Savi Nutrition"
    ws["A1"].font = Font(bold=True, size=16, color=GREEN)
    ws["A2"] = f"Production Formula: {product_name}"
    ws["A2"].font = Font(size=11, color="66756C")
    ws["A3"] = f"Generated on {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
    ws["A3"].font = Font(size=11, color="66756C")

    # Table headers
    headers = ["S.No", "Category", "Material", "Old Machine Qty (kg)", "New Machine Qty (kg)"]
    header_fill = PatternFill("solid", fgColor=GREEN)
    header_font = Font(bold=True, color="FFFFFF", size=11)
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=5, column=col, value=title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER

    # Data rows
    for idx, m in enumerate(materials, start=1):
        row_data = [idx, m["category"], m["material"], m["old_machine"], m["new_machine"]]
        for c, value in enumerate(row_data, start=1):
            cell = ws.cell(row=5 + idx, column=c, value=value)
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")
            if c >= 4:
                cell.alignment = Alignment(horizontal="right", vertical="center")
        if idx % 2 == 0:
            for c in range(1, len(headers) + 1):
                ws.cell(row=5 + idx, column=c).fill = PatternFill("solid", fgColor="F4FAF6")

    ws.freeze_panes = "A6"
    ws.auto_filter.ref = f"A5:E{5 + len(materials)}"

    # Column widths
    widths = {1: 8, 2: 20, 3: 30, 4: 22, 5: 22}
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = backup_stamp()
    safe_name = product_name.replace(" ", "_")
    return send_file(buf,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name=f"production_formula_{safe_name}_{stamp}.xlsx")


def _parse_ddmm(value):
    """Parse a dd-mm-yyyy (or %d %b %Y display) date string. Returns a date or None."""
    if not value:
        return None
    s = str(value).strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _credit_alert_raw(vendor, credit_days, credit_due_date, remaining, status):
    """Return an alert dict when a credit payment is due within 2 days or overdue."""
    try:
        days = int(credit_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return None
    try:
        rem = int(str(remaining or 0).replace(',', '').strip())
    except (TypeError, ValueError):
        rem = 0
    if rem <= 0 or status == 'Paid':
        return None
    due = _parse_ddmm(credit_due_date)
    if not due:
        return None
    days_left = (due - date.today()).days
    if days_left > 2:
        return None
    return {"vendor": vendor, "credit_days": days, "due_date": credit_due_date,
            "days_left": days_left, "overdue": days_left < 0}


def credit_alert(p):
    """Jinja helper: credit alert for a Purchase ORM object or dict (used to highlight rows)."""
    if isinstance(p, dict):
        return _credit_alert_raw(
            p.get('vendor_name', ''), p.get('credit_days', 0), p.get('credit_due_date', '') or '',
            p.get('remaining', 0), p.get('status', '') or '')
    return _credit_alert_raw(
        p.vendor_name, getattr(p, 'credit_days', 0), getattr(p, 'credit_due_date', '') or '',
        getattr(p, 'remaining', 0) or 0, getattr(p, 'status', '') or '')


app.jinja_env.globals['credit_alert'] = credit_alert


def _notification_notes(payload):
    """Build the live notification list. Every note carries a stable `id` key
    so the admin can select and delete individual notifications."""
    notes = []
    for r in payload["purchases"]:
        alert = _credit_alert_raw(r["vendor_name"], r.get("credit_days", 0),
                                  r.get("credit_due_date", ""), r.get("remaining", 0),
                                  r.get("status", ""))
        if not alert:
            continue
        due_on = alert["due_date"]
        nid = f"credit|{r['vendor_name']}"
        if alert["overdue"]:
            notes.append({"id": nid, "icon": "fa-exclamation-circle", "tone": "danger",
                          "title": "Credit overdue",
                          "text": f"{alert['vendor']} is overdue by {-alert['days_left']} day(s) — Rs {r.get('remaining', 0):,} pending.",
                          "time": f"Due {due_on}"})
        elif alert["days_left"] == 0:
            notes.append({"id": nid, "icon": "fa-hourglass-half", "tone": "warn",
                          "title": "Credit due today",
                          "text": f"{alert['vendor']} payment is due today — Rs {r.get('remaining', 0):,} pending.",
                          "time": f"Due {due_on}"})
        else:
            notes.append({"id": nid, "icon": "fa-hourglass-half", "tone": "warn",
                          "title": "Credit due soon",
                          "text": f"{alert['vendor']} is due in {alert['days_left']} day(s) — Rs {r.get('remaining', 0):,} pending.",
                          "time": f"Due {due_on}"})
    for p in payload["products"]:
        if p["status"] in ("Low Stock", "Critical"):
            notes.append({"id": f"lowstock|{p['name']}", "icon": "fa-exclamation-triangle", "tone": "warn",
                          "title": "Low stock", "text": f"{p['name']} is low ({p['stock']} left).",
                          "time": "Needs attention"})
    for r in payload["resources"]:
        if r["status"] == "Critical":
            notes.append({"id": f"critical|{r['name']}", "icon": "fa-times-circle", "tone": "danger",
                          "title": "Critical stock", "text": f"{r['name']} is critical ({r['closing']} left).",
                          "time": r["last_update"]})
    for p in payload["production"]:
        if p["status"] == "In Progress":
            notes.append({"id": f"production|{p['product']}|{p['batch']}", "icon": "fa-cogs", "tone": "info",
                          "title": "Production running", "text": f"{p['product']} batch {p['batch']} in progress.",
                          "time": p["date"]})
    outstanding = sum(p["remaining"] for p in payload["purchases"])
    if outstanding > 0:
        notes.append({"id": "outstanding|payments", "icon": "fa-file-invoice", "tone": "info",
                      "title": "Outstanding payments",
                      "text": f"Rs {outstanding:,} pending across purchase records.",
                      "time": "Accounts"})
    return notes


def _dismissed_notification_keys():
    try:
        return {row[0] for row in db.session.query(NotificationDismiss.nkey).all()}
    except Exception:
        return set()


@app.route('/api/notifications', methods=['GET'])
def api_notifications():
    payload = _backup_payload()
    dismissed = _dismissed_notification_keys()
    active = [n for n in _notification_notes(payload) if n["id"] not in dismissed][:15]
    return _ok({"notifications": active, "count": len(active)})


@app.route('/api/notifications/delete', methods=['POST'])
def api_notifications_delete():
    data = request.get_json(silent=True) or {}
    keys = data.get('keys') or data.get('ids') or []
    if not isinstance(keys, list):
        return _fail("keys must be a list")
    added = 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    for k in keys:
        k = str(k).strip()
        if not k:
            continue
        if not NotificationDismiss.query.filter_by(nkey=k).first():
            db.session.add(NotificationDismiss(nkey=k, created=now))
            added += 1
    db.session.commit()
    return _ok({"dismissed": added})


@app.route('/api/notifications/clear', methods=['POST'])
def api_notifications_clear():
    payload = _backup_payload()
    dismissed = _dismissed_notification_keys()
    notes = [n for n in _notification_notes(payload) if n["id"] not in dismissed]
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    for n in notes:
        db.session.add(NotificationDismiss(nkey=n["id"], created=now))
    db.session.commit()
    return _ok({"cleared": len(notes)})


@app.route('/api/messages/send', methods=['POST'])
def api_send_message():
    data = request.get_json(silent=True) or {}
    text = str(data.get('message', '')).strip()
    if not text:
        return _fail("Message cannot be empty")
    msg = Message(text=text, sender="Admin", time=datetime.now().strftime('%Y-%m-%d %H:%M'), seen_by="")
    db.session.add(msg)
    db.session.commit()
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()
    return _ok({"success": True, "messages": [m.to_dict() for m in reversed(msgs)]})


@app.route('/api/messages', methods=['GET'])
def api_get_messages():
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()
    return _ok({"messages": [m.to_dict() for m in reversed(msgs)]})


@app.route('/api/messages/seen', methods=['POST'])
def api_seen_message():
    data = request.get_json(silent=True) or {}
    label = str(data.get('label', 'User')).strip() or 'User'
    try:
        msg_id = int(data.get('id'))
    except (TypeError, ValueError):
        return _fail("id is required")
    msg = db.session.get(Message, msg_id)
    if not msg:
        return _fail("Message not found")
    seen_list = [s.strip() for s in msg.seen_by.split(",") if s.strip()] if msg.seen_by else []
    if label not in seen_list:
        seen_list.append(label)
        msg.seen_by = ",".join(seen_list)
        db.session.commit()
    return _ok({"success": True})


@app.route('/api/messages/<int:message_id>', methods=['DELETE'])
def api_delete_message(message_id):
    """Item 1 (2026-10-05): per-message delete for the admin message list. Only
    removes the single row - /api/messages/clear wipes the whole table."""
    msg = db.session.get(Message, message_id)
    if not msg:
        return _fail("Message not found")
    db.session.delete(msg)
    db.session.commit()
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()
    return _ok({"success": True, "messages": [m.to_dict() for m in reversed(msgs)]})


@app.route('/api/messages/clear', methods=['POST'])
def api_clear_messages():
    from sqlalchemy import text as _text
    try:
        db.session.execute(_text("DELETE FROM messages"))
        db.session.commit()
    except Exception:
        pass
    return _ok({"success": True})


@app.route('/api/messages/status', methods=['GET'])
def api_message_status():
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()

    def _seen(m, label):
        return label in [s.strip() for s in (m.seen_by or '').split(',') if s.strip()]

    # admin's outgoing messages the user has not read yet, plus the user's
    # incoming messages the admin has not acknowledged yet.
    unread = [m for m in msgs
              if ((m.sender or 'Admin') == 'Admin' and not _seen(m, 'User'))
              or ((m.sender or 'Admin') == 'User' and not _seen(m, 'Admin'))]
    return _ok({"unread_count": len(unread), "messages": [m.to_dict() for m in reversed(msgs)]})


# ---------- Presence (multi-user wait/alert) ----------
# Tracks which tabs are on which page. If a second user opens the same
# page while another is active there, they get a wait/alert notification.
import time as _time
_presence = {}
_PRESENCE_TTL = 60


@app.route('/api/presence', methods=['POST'])
def api_presence():
    data = request.get_json(silent=True) or {}
    tab = str(data.get('tab', '')).strip()[:64]
    page = str(data.get('page', '')).strip()[:160] or '/'
    label = str(data.get('label', '')).strip()[:40] or 'Another user'
    if not tab:
        return _fail("tab is required")
    now = _time.time()
    _presence[tab] = {"page": page, "label": label, "seen": now}
    stale = [t for t, v in _presence.items() if now - v["seen"] > _PRESENCE_TTL]
    for t in stale:
        del _presence[t]
    others = [{"label": v["label"], "seconds_ago": int(now - v["seen"])}
              for t, v in _presence.items()
              if t != tab and v["page"] == page]
    return _ok({"others": others, "you": label})


if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=5000)
