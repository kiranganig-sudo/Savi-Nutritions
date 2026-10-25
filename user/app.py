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
import mimetypes
from datetime import datetime, date, timedelta
from io import BytesIO

from flask import Flask, jsonify, redirect, render_template, request, send_file, send_from_directory, session, url_for, make_response
from werkzeug.exceptions import NotFound
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import inspect, or_

try:
    from .models import (
        db, Product, ProductMaterial, MaterialDaily, Resource, RawMaterial,
        ProductionEntry, Purchase, Vendor, StockMovement, Setting, MachineProductionEntry,
        Message, FormulaSheet, ThirdPartyItem, DriverDetails, User, compute_product_status, compute_raw_status,
        compute_resource_status, RawMaterialSale, ProductSale, SalesRemark,
    )
    from .seed import seed_database, RESOURCE_CATEGORIES, PRODUCTS
except ImportError:
    from models import (
        db, Product, ProductMaterial, MaterialDaily, Resource, RawMaterial,
        ProductionEntry, Purchase, Vendor, StockMovement, Setting, MachineProductionEntry,
        Message, FormulaSheet, ThirdPartyItem, DriverDetails, User, compute_product_status, compute_raw_status,
        compute_resource_status, RawMaterialSale, ProductSale, SalesRemark,
    )
    from seed import seed_database, RESOURCE_CATEGORIES, PRODUCTS

try:
    from .export_utils import (
        backup_label, backup_stamp, build_backup_zip, build_excel_bytes,
        build_pdf_bytes, THIN_BORDER, GREEN, LIGHT_GREEN,
    )
except ImportError:
    from export_utils import (
        backup_label, backup_stamp, build_backup_zip, build_excel_bytes,
        build_pdf_bytes, THIN_BORDER, GREEN, LIGHT_GREEN,
    )

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
from savi_file_storage import FileStorageError, FileStorageUnavailable, StoredFileNotFound, _max_file_bytes, delete_upload, load_upload, save_upload, storage_status

DB_PATH = os.path.join(BASE_DIR, '..', 'admin', 'savi.db')
DB_PATH = os.path.abspath(DB_PATH)

# Input restriction for Resource Maintenance item names and stock numbers:
# both are capped at 40 characters (numbers for stock, letters + digits for
# the item name). Anything longer is rejected with an "excessive value"
# message instead of being silently truncated. Kept identical to the admin
# app so both portals enforce the same limits.
RESOURCE_FIELD_MAX_CHARS = 40

# ...and the largest value a stock column can actually hold (signed 64-bit,
# the type both SQLite and MySQL use for INTEGER). A longer number is refused
# with the same "excessive value" message instead of crashing the save.
RESOURCE_MAX_VALUE = 9223372036854775807

# Item names take letters AND numbers (the user asked for both) plus the few
# punctuation marks real item names already use on the live data - e.g.
# "Biotin 2%", "Clostat-365", "Copra Cake (AK)". Mirrors RESOURCE_NAME_PATTERN
# in admin/app.py; keep the two in step.
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

app = Flask(__name__)
# --- Database: MySQL via DATABASE_URL or fallback to SQLite ---
# Both admin and user MUST point to SAME MySQL database when using MySQL.
# Example: mysql+pymysql://savi_user:StrongPass@localhost:3306/savi_nutrition?charset=utf8mb4
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
_secret = os.environ.get("SECRET_KEY", "")
# Admin and user SHOULD use different secrets in production; fallback for dev only
if not _secret:
    if os.environ.get("FLASK_ENV") == "production" or os.environ.get("DATABASE_URL"):
        raise RuntimeError("SECRET_KEY env var is required in production")
    _secret = "savi-nutrition-user-secret-dev-only"
app.config["SECRET_KEY"] = _secret
app.config["SESSION_COOKIE_NAME"] = "savi_user_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SECURE_COOKIE", "0") == "1"
app.config["JSON_SORT_KEYS"] = False

# Fallback "contact the admin" number shown on the login page and portal header
# when admin Settings has no number configured yet.
DEFAULT_ADMIN_CONTACT_PHONE = "9980509508"
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
ALLOWED_SLIP_EXTENSIONS = {'.jpg', '.jpeg', '.png'}
DRIVER_UPLOAD_DIR = os.path.join(ROOT_DIR, 'uploads', 'driver_slips')
SALES_UPLOAD_DIR = os.path.join(ROOT_DIR, 'uploads', 'sales_slips')
if not (os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV')):
    os.makedirs(PURCHASE_UPLOAD_DIR, exist_ok=True)
    os.makedirs(DRIVER_UPLOAD_DIR, exist_ok=True)
    os.makedirs(SALES_UPLOAD_DIR, exist_ok=True)
ALLOWED_DRIVER_SLIP_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.pdf'}
ALLOWED_SALES_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.pdf'}

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
    """Raised inside the startup block when the schema is already up to date."""


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
    """Record _SCHEMA_VERSION after the startup block completed."""
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
    `_prepare_slip` keeps the uploaded bytes in purchases.slip_data /
    driver_details.slip_data. Returns (bytes, mime) or None.
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


class SelectedMaterialRow:
    def __init__(self, resource):
        self.id = 'selected-%s' % resource.id
        self.material = resource.name
        self.unit = resource.unit or 'kg'
        self.opening = resource.closing or 0
        self.consumed = 0
        self.closing = resource.closing or 0
        self.batch = ''
        self.status = resource.status or 'Good'
        self.old_machine_qty = 0
        self.new_machine_qty = 0
        self.old_batches = 0
        self.new_batches = 0
        self.total_bags = 0

    def to_dict(self):
        return {
            'id': self.id, 'material': self.material, 'unit': self.unit,
            'opening': self.opening, 'consumed': self.consumed, 'closing': self.closing,
            'batch': self.batch, 'status': self.status,
            'old_machine_qty': self.old_machine_qty, 'new_machine_qty': self.new_machine_qty,
            'old_batches': self.old_batches, 'new_batches': self.new_batches,
            'total_bags': self.total_bags,
        }


def _production_resource_catalog():
    groups = ('Raw Materials', 'Medicines', 'Chemicals')
    resources = Resource.query.filter(Resource.group.in_(groups)).order_by(Resource.group, Resource.name).all()
    catalog = {group: [] for group in groups}
    for resource in resources:
        catalog.setdefault(resource.group, []).append({
            'name': resource.name,
            'unit': resource.unit or 'kg',
            'status': resource.status or 'Good',
            'closing': resource.closing or 0,
        })
    return catalog


def _migrate_product_material_batch():
    """Add batch column to existing savi.db files and backfill values."""
    from sqlalchemy import text
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


def _migrate_product_machine_quantities():
    """Add machine quantity columns and retain existing production values.

    Change #63: the demo backfill that used to stamp fabricated 10+i/100+i
    machine quantities into every formula row is removed - a fresh start must
    not invent production values. Only the idempotent column ADDs remain.
    """
    from sqlalchemy import text
    cols = _table_columns("product_materials")
    if "old_machine_qty" not in cols:
        db.session.execute(text("ALTER TABLE product_materials ADD COLUMN old_machine_qty FLOAT DEFAULT 0"))
    if "new_machine_qty" not in cols:
        db.session.execute(text("ALTER TABLE product_materials ADD COLUMN new_machine_qty FLOAT DEFAULT 0"))
    db.session.commit()
    return


def _migrate_purchase_phone():
    """Add vendor_phone column to existing savi.db files.

    Change #63: the demo phone-number backfill that used to run here is removed -
    a fresh start must not fabricate sample phone numbers into real rows. Only
    the column ADD (idempotent) remains.
    """
    from sqlalchemy import text
    cols = _table_columns("purchases")
    if "vendor_phone" not in cols:
        db.session.execute(text("ALTER TABLE purchases ADD COLUMN vendor_phone VARCHAR(30) DEFAULT ''"))
        db.session.commit()
    return


def _migrate_formula_sheet_product():
    """Add product column to existing formula_sheet rows (product-scoped sheets).

    Change #63: this migration existed only in the admin app, so a user-app
    boot against an old DB (no product column) would crash on any formula
    query. Mirrors the admin definition.
    """
    from sqlalchemy import text
    cols = _table_columns("formula_sheet")
    if "product" not in cols:
        db.session.execute(text("ALTER TABLE formula_sheet ADD COLUMN product VARCHAR(120) DEFAULT ''"))
        db.session.commit()


def _migrate_third_party_published():
    """Add published column to existing third_party_items table.

    Change #63: this migration existed only in the admin app, so a user-app
    boot against an old DB (no published column) would crash on any 3rd-party
    query. Mirrors the admin definition.
    """
    from sqlalchemy import text
    cols = _table_columns("third_party_items")
    if "published" not in cols:
        # Items that existed before the publish feature stay visible by default.
        db.session.execute(text("ALTER TABLE third_party_items ADD COLUMN published BOOLEAN DEFAULT 0"))
        db.session.execute(text("UPDATE third_party_items SET published = 1"))
        db.session.commit()


def _sync_resource_catalog():
    """Keep Resource Maintenance exactly aligned with the three configured catalogs."""
    changed = False
    # Change #63: the old group-delete loop removed any Resource whose group
    # was not in RESOURCE_CATEGORIES on every boot, which destroyed custom
    # groups users had created. Same fix as the admin app: only the three
    # catalog groups are enforced; custom groups are preserved.
    # allowed_groups = set(RESOURCE_CATEGORIES.keys())
    # for row in Resource.query.all():
    #     if row.group not in allowed_groups:
    #         db.session.delete(row)
    #         changed = True
    # db.session.flush()

    for group, desired in RESOURCE_CATEGORIES.items():
        desired_names = [name for name, _ in desired]
        # Preserve stock/history when known legacy names are replaced by requested names.
        aliases = {
            ("Raw Materials", "Hallar Polish"): "Hullar Polish",
            ("Medicines", "Aletta"): "Aleta",
            ("Medicines", "Heptacare"): "Heptacare (Toxin Binder)",
            ("Medicines", "Biocahal BAD3E"): "Biochal BAD3E",
            ("Chemicals", "DCP"): "DCP (Dicalcium Phosphate)",
        }
        for (old_group, old_name), new_name in aliases.items():
            if old_group != group or new_name not in desired_names:
                continue
            old = Resource.query.filter_by(group=old_group, name=old_name).first()
            new = Resource.query.filter_by(group=group, name=new_name).first()
            if old and not new:
                old.name = new_name
                changed = True

        # Change #63: a resource a user added under a valid group is NOT demo
        # data - do not delete it on boot just because its name is absent from
        # the static catalog. (Previously this loop destroyed every custom
        # resource each startup.)
        # for row in Resource.query.filter_by(group=group).all():
        #     if row.name not in desired_names:
        #         db.session.delete(row)
        #         changed = True
        db.session.flush()
        existing = {r.name: r for r in Resource.query.filter_by(group=group).all()}
        for i, (name, item_category) in enumerate(desired, 1):
            row = existing.get(name)
            if not row:
                # Change #63: missing catalog resource is created EMPTY (0 kg),
                # never at a fabricated 500+i*25 demo stock.
                stock = 0
                row = Resource(group=group, name=name, item_category=item_category, unit="kg",
                               opening=stock, consumed=0, closing=stock, min_stock=250,
                               status=compute_resource_status(stock, 250), last_update="")
                db.session.add(row)
                changed = True
            elif row.item_category != item_category:
                row.item_category = item_category
                changed = True
            row.recompute()
    if changed:
        db.session.commit()
    return changed


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
    Mirror in admin/app.py - admin runs first in combined mode."""
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
    """Add the admin contact phone column to settings (login "contact admin")."""
    from sqlalchemy import text
    cols = _table_columns("settings")
    if "admin_contact_phone" not in cols:
        db.session.execute(text(
            "ALTER TABLE settings ADD COLUMN admin_contact_phone VARCHAR(30) DEFAULT ''"))
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

    The admin app owns the upload route, but the user portal also serves the
    same tables, so both models/apps must carry the blob columns - otherwise
    the user portal's SELECT fails on the extra column the admin app added.
    create_all builds db.LargeBinary as a 64 KB BLOB, so MySQL needs the
    explicit MEDIUMBLOB (the cap is 2 MB per photo).
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


def _migrate_vendor_products():
    """Add the products Text column to vendors (before any full-row Vendor query)."""
    from sqlalchemy import text
    cols = _table_columns("vendors")
    if "products" not in cols:
        db.session.execute(text("ALTER TABLE vendors ADD COLUMN products TEXT"))
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


def _valid_gstin(value):
    """Return uppercased GSTIN if exactly 15 alphanumeric chars, else None."""
    import re
    v = str(value or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{15}", v):
        return v
    return None


def _valid_phone(value):
    """Return phone as digits if exactly 10 digits, else None."""
    import re
    digits = re.sub(r"\D", "", str(value or ""))
    if re.fullmatch(r"\d{10}", digits):
        return digits
    return None


_OLD_PRODUCT_STOCK = {
    "Savi Gold Plus": 150, "Promin Gold": 85, "Promin Plus": 12,
    "Promin": 200, "Savi Gold": 5, "Savi Supreme": 180,
}


def _upgrade_product_demo_stock():
    """Move demo products onto the new stock spread (some >2000, some 1000-2000, some <1000).

    Applies only when a product still carries the old demo stock value, so it is
    a one-time migration and never overwrites stock the user has edited.
    """
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


def _migrate_vendor_category():
    """Add category column to existing savi.db vendors and backfill it."""
    from sqlalchemy import text
    cols = _table_columns("vendors")
    if "category" not in cols:
        db.session.execute(text("ALTER TABLE vendors ADD COLUMN category VARCHAR(40) DEFAULT 'Raw Materials'"))
        db.session.commit()
    if "gstin" not in cols:
        db.session.execute(text("ALTER TABLE vendors ADD COLUMN gstin VARCHAR(15) DEFAULT ''"))
        db.session.commit()
    empties = Vendor.query.filter(
        (Vendor.category.is_(None)) | (Vendor.category == "")).all()
    for v in empties:
        v.category = _vendor_category_for(v.product)
    if empties:
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
    # do not fabricate sample rows (the empty-table guard below would now
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


def _migrate_production_machine_consumed():
    from sqlalchemy import text
    cols = _table_columns("production")
    if "machine" not in cols:
        db.session.execute(text("ALTER TABLE production ADD COLUMN machine VARCHAR(20) DEFAULT 'Old Machine'"))
        db.session.commit()
    if "consumed" not in cols:
        db.session.execute(text("ALTER TABLE production ADD COLUMN consumed INTEGER DEFAULT 0"))
        db.session.commit()

def _init_third_party_table():
    db.create_all()


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


def _migrate_material_daily_supervisor():
    """Ensure supervisor_name + opening + shift columns exist on the shared material_daily table."""
    from sqlalchemy import text
    try:
        cols = _table_columns("material_daily")
        if "supervisor_name" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN supervisor_name VARCHAR(120) DEFAULT ''"))
        if "opening" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN opening INTEGER DEFAULT 0"))
        if "shift" not in cols:
            db.session.execute(text("ALTER TABLE material_daily ADD COLUMN shift VARCHAR(10) DEFAULT 'morning'"))
            db.session.execute(text("UPDATE material_daily SET shift = 'morning' WHERE shift IS NULL OR shift = ''"))
        db.session.commit()
    except Exception:
        db.session.rollback()


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
    """Ensure old_batches/new_batches columns exist on product_materials."""
    from sqlalchemy import text
    try:
        cols = _table_columns("product_materials")
        if "old_batches" not in cols:
            db.session.execute(text("ALTER TABLE product_materials ADD COLUMN old_batches INTEGER DEFAULT 0"))
        if "new_batches" not in cols:
            db.session.execute(text("ALTER TABLE product_materials ADD COLUMN new_batches INTEGER DEFAULT 0"))
        db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_pm_total_bags():
    """Ensure manually-entered total_bags column exists on product_materials."""
    from sqlalchemy import text
    try:
        cols = _table_columns("product_materials")
        if "total_bags" not in cols:
            db.session.execute(text("ALTER TABLE product_materials ADD COLUMN total_bags FLOAT DEFAULT 0"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_product_bags():
    """Ensure manually-entered old/new machine bag totals exist on products."""
    from sqlalchemy import text
    try:
        cols = _table_columns("products")
        changed = False
        if "old_machine_bags" not in cols:
            db.session.execute(text("ALTER TABLE products ADD COLUMN old_machine_bags FLOAT DEFAULT NULL"))
            changed = True
        if "new_machine_bags" not in cols:
            db.session.execute(text("ALTER TABLE products ADD COLUMN new_machine_bags FLOAT DEFAULT NULL"))
            changed = True
        if changed:
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_settings_maintenance():
    """Ensure maintenance_mode/maintenance_message columns exist on settings."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        if "maintenance_mode" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN maintenance_mode BOOLEAN DEFAULT 0"))
        if "maintenance_message" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN maintenance_message TEXT"))
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


def _seed_default_admin():
    return False


def _active_shift_value():
    """Return the currently active shift ('morning' or 'night')."""
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


def _shift_opening(product_name, material, date_str, shift, fallback=0):
    """Opening stock for a shift = closing stock of the previous shift.

    Morning follows the previous date's night shift (or morning shift when no
    night shift was recorded); night follows the same date's morning shift.
    Stored rows with opening<=0 are broken zero snapshots (created before real
    stock existed / from an empty formula opening) and are SKIPPED so the chain
    falls through to the supplied fallback (Resource Maintenance closing, which
    Purchase Maintenance feeds). Legacy shift-NULL rows count as morning.
    When a chain value exists it is returned UNLESS the fallback (live stock
    fed by purchases) is higher - a purchase after the last snapshot must show.
    """
    from datetime import timedelta
    shift = 'night' if str(shift).strip().lower() == 'night' else 'morning'
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


def _migrate_change_version():
    """Add the change_version column that tells the admin when the user portal updated data."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        if "change_version" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN change_version INTEGER DEFAULT 0"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _migrate_admin_change_version():
    """Add the admin_change_version column that tells this portal when the admin updated data."""
    from sqlalchemy import text
    try:
        cols = _table_columns("settings")
        if "admin_change_version" not in cols:
            db.session.execute(text("ALTER TABLE settings ADD COLUMN admin_change_version INTEGER DEFAULT 0"))
            db.session.commit()
    except Exception:
        db.session.rollback()


def _admin_change_version_value():
    """Current admin-update version number (watched by this portal)."""
    settings = _get_settings()
    try:
        return int(getattr(settings, 'admin_change_version', 0) or 0)
    except Exception:
        return 0


def _bump_change_version():
    """Increment the version token the admin polls to prompt a refresh."""
    try:
        settings = _get_settings()
        settings.change_version = int(getattr(settings, 'change_version', 0) or 0) + 1
        db.session.commit()
    except Exception:
        db.session.rollback()


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


def _set_active_user(user):
    """Record which user-portal account is currently logged in (shared with admin)."""
    try:
        settings = _get_settings()
        settings.active_user_id = user.id
        settings.active_user_name = user.full_name or ""
        settings.active_user_login = user.login_id or ""
        settings.active_user_phone = user.phone or ""
        settings.active_user_shift = user.shift or ""
        settings.active_user_at = datetime.now().strftime('%d %b %Y, %I:%M %p')
        db.session.commit()
    except Exception:
        db.session.rollback()


def _clear_active_user():
    """Clear the currently-logged-in user record on logout."""
    try:
        settings = _get_settings()
        settings.active_user_id = 0
        settings.active_user_name = ""
        settings.active_user_login = ""
        settings.active_user_phone = ""
        settings.active_user_shift = ""
        settings.active_user_at = ""
        db.session.commit()
    except Exception:
        db.session.rollback()


@app.after_request
def _flag_user_changes(response):
    """Every successful user-portal write bumps change_version so the admin UI
    knows data was updated here (it never logs the admin out, just prompts a refresh)."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        endpoint = request.endpoint or ''
        if endpoint not in ('login', 'signup', 'static', 'api_health', 'api_messages_seen') \
                and not endpoint.startswith('static_') \
                and response.status_code < 400:
            _bump_change_version()
    return response


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
        _migrate_active_shift()
        _migrate_change_version()
        _migrate_admin_change_version()
        _migrate_active_user()
        _seed_default_admin()
        from sqlalchemy import text
        cols = _table_columns("products")
        if "published" not in cols:
            db.session.execute(text("ALTER TABLE products ADD COLUMN published BOOLEAN DEFAULT 0"))
            db.session.execute(text("UPDATE products SET published = 1"))
            db.session.commit()
        # One-time safety net: if the publish feature was never used on a given
        # table, keep its existing rows visible on the user side. Each table is
        # handled independently so deliberate publish choices are never overridden.
        try:
            pub_products = db.session.execute(text("SELECT COUNT(*) FROM products WHERE published = 1")).scalar() or 0
            if pub_products == 0:
                db.session.execute(text("UPDATE products SET published = 1"))
            pub_thirds = db.session.execute(text("SELECT COUNT(*) FROM third_party_items WHERE published = 1")).scalar() or 0
            if pub_thirds == 0:
                db.session.execute(text("UPDATE third_party_items SET published = 1"))
            db.session.commit()
        except Exception:
            db.session.rollback()
        _migrate_pm_total_bags()
        _migrate_product_bags()
        _migrate_product_machine_quantities()
        _migrate_product_material_batch()
        _migrate_purchase_phone()
        _migrate_formula_sheet_product()
        _migrate_third_party_published()
        if os.environ.get("SAVI_NO_SEED") != "1":
            _sync_resource_catalog()
            seed_database(db)
            _seed_material_daily()
            _seed_production_sample()
            _roll_daily_forward()
            _upgrade_product_demo_stock()
        for prod in Product.query.all():
            st = compute_product_status(prod.stock)
            if prod.status != st:
                prod.status = st
        db.session.commit()
        _migrate_vendor_category()
        _migrate_production_machine_consumed()
        _migrate_settings_maintenance()
        _migrate_pm_batches()
        _migrate_material_daily_supervisor()
        _migrate_material_daily_bags()
        _init_third_party_table()
        if Vendor.query.count() == 0:
            for p in Purchase.query.order_by(Purchase.id).all():
                db.session.add(Vendor(
                    vendor_name=p.vendor_name, address=p.address or "",
                    vendor_phone=p.vendor_phone or "", date_display=p.date_display,
                    category=_vendor_category_for(p.product),
                    product=p.product, quantity=p.quantity, amount_paid=p.amount_paid,
                    remaining=p.remaining, payment_mode=p.payment_mode, status=p.status))
            db.session.commit()
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
    products = Product.query.filter(Product.published.is_(True)).order_by(Product.id).all()
    third_party = ThirdPartyItem.query.filter(ThirdPartyItem.published.is_(True)).order_by(ThirdPartyItem.id).all()
    return render_template('dashboard.html',
        products=[p.to_dict() for p in products],
        third_party=[t.to_dict() for t in third_party],
        resource_groups=_resource_overview_groups(),
        total_products=Product.query.filter(Product.published.is_(True)).count(),
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


@app.route('/resource-maintenance/low-stock')
def resource_maintenance_low_stock():
    resources = Resource.query.filter(Resource.status == 'Critical').order_by(Resource.group, Resource.closing, Resource.id).all()
    return render_template('resource_maintenance.html',
        resource_category='Stock Alerts',
        resources=[r.to_dict() for r in resources],
        categories=list(RESOURCE_CATEGORIES.keys()),
        active_page='resource_maintenance',
        low_stock_only=True
    )


@app.route('/resource-maintenance/<path:category>')
def resource_maintenance(category):
    if category not in RESOURCE_CATEGORIES:
        return redirect(url_for('resource_maintenance', category='Raw Materials'))
    resources = Resource.query.filter_by(group=category).order_by(Resource.id).all()
    return render_template('resource_maintenance.html',
        resource_category=category,
        resources=[r.to_dict() for r in resources],
        categories=list(RESOURCE_CATEGORIES.keys()),
        active_page='resource_maintenance'
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
    products = Product.query.filter(Product.published.is_(True)).order_by(Product.id).all()
    return render_template('finished_goods.html',
        inventory_title='Finished Goods',
        products=[p.to_dict() for p in products],
        active_page='finished_goods'
    )


@app.route('/inventory-product/<path:product_name>')
def inventory_product(product_name):
    from datetime import timedelta
    # Shift is LOCKED to the shift chosen at login (user decisions 2026-09-27/28):
    # the session shift (Morning/Night picked on the login form) always wins -
    # the Morning/Night toggle on the user portal is disabled and ?shift= is
    # ignored; to change shift the user logs out and picks the other one at login.
    session_shift = str(session.get('savi_user_shift', '') or '').strip().lower()
    if session_shift in ('morning', 'night'):
        selected_shift = session_shift
        shift_locked = True
    else:
        # Account without a shift (legacy row) - old order: ?shift=, then global.
        shift_locked = False
        requested_shift = str(request.args.get('shift', '') or '').strip().lower()
        if requested_shift in ('morning', 'night'):
            selected_shift = requested_shift
        else:
            selected_shift = _active_shift_value()
    active_shift = _active_shift_value()
    product = Product.query.filter(Product.name == product_name, Product.published.is_(True)).first()
    is_third_party = False
    third_item = None
    if not product:
        third_item = ThirdPartyItem.query.filter(ThirdPartyItem.name == product_name, ThirdPartyItem.published.is_(True)).first()
        if not third_item:
            return redirect(url_for('finished_goods'))
        is_third_party = True
        product = {
            'id': 'tp-' + str(third_item.id), 'name': third_item.name,
            'category': '3rd Party Product', 'price': third_item.price or 0,
            'stock': third_item.quantity or 0, 'image': '', 'status': 'In Stock'
        }
    # Project Update uses the same material list as Production Formula and
    # shows the saved production values for the product.
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
        rows = []
        for f in formula_rows:
            opening = opening_map.get(f.material, 0)
            old_batches = f.old_machine or 0
            new_batches = f.new_machine or 0
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
                        'old_machine_qty': self.old_machine_qty or 0,
                        'new_machine_qty': self.new_machine_qty or 0,
                    }
            rows.append(ThirdPartyProductionRow(f, opening, old_batches, new_batches))
    resource_catalog = _production_resource_catalog()
    resource_by_name = {
        item['name']: item
        for items in resource_catalog.values()
        for item in items
    }
    selected_names = []
    if not is_third_party:
        existing_by_name = {row.material: row for row in rows}
        requested_names = request.args.getlist('selected')
        for raw_name in requested_names:
            name = str(raw_name or '').strip()
            if name and name not in selected_names and (name in existing_by_name or name in resource_by_name):
                selected_names.append(name)
        if selected_names:
            rows = []
            for name in selected_names:
                if name in existing_by_name:
                    rows.append(existing_by_name[name])
                else:
                    rows.append(SelectedMaterialRow(Resource.query.filter_by(name=name).first()))
    if not selected_names:
        selected_names = [row.material for row in rows]
    today = datetime.now().strftime('%Y-%m-%d')
    yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    selected_daily = request.args.get('date', today).strip() or today
    try:
        datetime.strptime(selected_daily, "%Y-%m-%d")
    except ValueError:
        selected_daily = today
    if selected_daily > today:
        selected_daily = today
    daily_rows = MaterialDaily.query.filter_by(product_name=product_name, date=selected_daily,
                                               shift=selected_shift).all()
    daily_by_material = {str(d.material).strip(): d for d in daily_rows}
    week_overview = []
    for k in range(8):
        day = (datetime.now() - timedelta(days=k)).strftime('%Y-%m-%d')
        day_rows = []
        week_overview.append({
            "date": day, "display": _prod_display_date(day),
            "count": len(day_rows),
            "consumed": sum(r.consumed for r in day_rows),
            "closing": sum(r.closing for r in day_rows),
        })
    # Combine saved Old Machine + New Machine production data for the summary page.
    old_entries = {e.material: e for e in MachineProductionEntry.query.filter_by(product_name=product_name, machine='old').all()}
    new_entries = {e.material: e for e in MachineProductionEntry.query.filter_by(product_name=product_name, machine='new').all()}
    loaded_old_batch = 0
    loaded_new_batch = 0
    if not is_third_party:
        for e in old_entries.values():
            try:
                if str(e.batch or '').strip():
                    loaded_old_batch = max(loaded_old_batch, int(float(e.batch)))
            except (TypeError, ValueError):
                pass
        for e in new_entries.values():
            try:
                if str(e.batch or '').strip():
                    loaded_new_batch = max(loaded_new_batch, int(float(e.batch)))
            except (TypeError, ValueError):
                pass
        if loaded_old_batch == 0 and rows:
            loaded_old_batch = rows[0].old_batches or 0
        if loaded_new_batch == 0 and rows:
            loaded_new_batch = rows[0].new_batches or 0
    else:
        loaded_old_batch = rows[0].old_batches if rows else 0
        loaded_new_batch = rows[0].new_batches if rows else 0
    summary_stock = []
    res_closing = _ClosingMap()
    try:
        for res in Resource.query.all():
            res_closing.put(res.name, res.closing or 0)
    except Exception:
        res_closing = _ClosingMap()
    old_bags_default = 0.0
    new_bags_default = 0.0
    for r in rows:
        material_name = str(r.material).strip()
        opening = _shift_opening(product_name, r.material, selected_daily, selected_shift,
                                 res_closing.get(material_name, r.opening or 0))
        _stored_row = daily_by_material.get(material_name)
        if _stored_row is not None and (_stored_row.opening or 0) > 0:
            # This shift already saved its update: show the opening it actually
            # started with. A fresh recompute would drift after the save has
            # decremented the Resource stock, and zero snapshots are skipped
            # above anyway (broken rows never force the display back to 0).
            opening = _stored_row.opening
        data = r.to_dict()
        # Per-shift list: when THIS shift has a stored row for the date, show its
        # batches/consumed - morning and night stay independent (no row = not updated yet).
        _daily = daily_by_material.get(material_name)
        stored_pair = None
        if _daily is not None and '/' in str(_daily.batch or ''):
            _ob_s, _nb_s = str(_daily.batch).split('/', 1)
            try:
                _ob_f, _nb_f = float(_ob_s or 0), float(_nb_s or 0)
                stored_pair = (int(_ob_f) if _ob_f.is_integer() else _ob_f,
                               int(_nb_f) if _nb_f.is_integer() else _nb_f)
            except ValueError:
                stored_pair = None
        if is_third_party:
            # Item 2 (2026-10-01): the formula's Old/New Machine values are the
            # per-batch QUANTITY the admin uploaded - shown as text in both Qty
            # columns. Batch inputs are seeded from THIS shift's stored pair only
            # (0 when never entered), and consumed = qty x batch for BOTH machines
            # - exactly like standard products.
            _ob_v, _nb_v = stored_pair if stored_pair is not None else (0, 0)
            _q_old = r.old_machine_qty or 0
            _q_new = r.new_machine_qty or 0
            old_part = (_q_old or 0) * _ob_v
            new_part = (_q_new or 0) * _nb_v
            total_consumed = int(round(old_part + new_part))
            total_output = (old_part + new_part) / 50.0
            data['old_batches'] = _ob_v
            data['new_batches'] = _nb_v
            data['old_machine_qty'] = _q_old
            data['new_machine_qty'] = _q_new
        elif _daily is not None:
            _ob_v, _nb_v = stored_pair if stored_pair is not None else (0, 0)
            _qty_old = getattr(r, 'old_machine_qty', None)
            if _qty_old is None:
                _qty_old = getattr(r, 'old_machine', 0) or 0
            _qty_new = getattr(r, 'new_machine_qty', None)
            if _qty_new is None:
                _qty_new = getattr(r, 'new_machine', 0) or 0
            old_part = (_qty_old or 0) * _ob_v
            new_part = (_qty_new or 0) * _nb_v
            total_consumed = int(_daily.consumed or 0)
            total_output = (old_part + new_part) / 50.0
            data['old_batches'] = _ob_v
            data['new_batches'] = _nb_v
        else:
            old_part = 0
            new_part = 0
            total_consumed = 0
            total_output = 0
            data['old_batches'] = 0
            data['new_batches'] = 0
        old_bags_default += old_part / 50.0
        new_bags_default += new_part / 50.0
        closing = max(opening - total_consumed, 0)
        total_bags = getattr(r, 'total_bags', 0) or 0
        resource_info = resource_by_name.get(material_name, {})
        data.update({'opening': opening, 'total_consumed': total_consumed, 'closing': closing,
                     'total_output': total_output, 'total_bags': total_bags,
                     'resource_status': resource_info.get('status', ''),
                     'resource_group': next((group for group, items in resource_catalog.items()
                                            if any(item['name'] == material_name for item in items)), '')})
        summary_stock.append(data)

    summary_total_consumed = sum((row.get('total_consumed') or 0) for row in summary_stock)
    summary_total_output = sum((row.get('total_output') or 0) for row in summary_stock)
    supervisor_name = ''
    for _d in daily_rows:
        if getattr(_d, 'supervisor_name', None):
            supervisor_name = _d.supervisor_name or ''
            break
    total_bags_total = sum((row.get('total_bags') or 0) for row in summary_stock)
    stored_old_bags = next((getattr(_d, 'old_bags', None) for _d in daily_rows
                            if getattr(_d, 'old_bags', None) is not None), None)
    stored_new_bags = next((getattr(_d, 'new_bags', None) for _d in daily_rows
                            if getattr(_d, 'new_bags', None) is not None), None)
    if isinstance(product, dict):
        product_old_machine_bags = int(stored_old_bags) if stored_old_bags is not None else 0
        product_new_machine_bags = int(stored_new_bags) if stored_new_bags is not None else 0
    else:
        # Item 5 (2026-10-05): ONLY this product+date+shift's stored bags may seed
        # the boxes. The old code fell back to products.old_machine_bags - a
        # product-level slot with NO date and NO shift - so a date the user never
        # filled in still displayed the last value ever typed for that product
        # ("I only entered Promin Gold but Promin Plus shows bags").
        # Item 4 (2026-10-05): a date with no stored row must read 0, so the
        # entry boxes only ever show what was actually saved for THIS date.
        product_old_machine_bags = int(round(stored_old_bags)) if stored_old_bags is not None else 0
        product_new_machine_bags = int(round(stored_new_bags)) if stored_new_bags is not None else 0

    return render_template('product_materials.html',
        product=product,
        raw_materials=[r.material for r in rows],
        material_stock=summary_stock,
        resource_groups=resource_catalog,
        selected_materials=selected_names,
        summary_total_consumed=summary_total_consumed,
        summary_total_output=summary_total_output,
        total_bags_total=total_bags_total,
        product_old_machine_bags=product_old_machine_bags,
        product_new_machine_bags=product_new_machine_bags,
        selected_daily=selected_daily,
        selected_daily_display=_prod_display_date(selected_daily),
        # Item 3 (2026-10-05): the template's max="{{ today_str }}" was empty
        # because the user app never supplied it, so the date picker happily
        # offered every future day. Pass it so past-only picking works.
        today_str=today,
        just_saved=(request.args.get('saved') == '1'),
        supervisor_name=supervisor_name,
        daily_rows=[r.to_dict() for r in daily_rows],
        daily_total_consumed=sum(r.consumed for r in daily_rows),
        daily_total_closing=sum(r.closing for r in daily_rows),
        week_overview=week_overview,
        can_edit=not is_third_party,
        show_update_controls=True,
        saved_old_batch=loaded_old_batch,
        saved_new_batch=loaded_new_batch,
        selected_shift=selected_shift,
        shift_locked=shift_locked,
        active_shift=active_shift,
        active_page='inventory_product',
        active_product=product_name,
        active_is_third_party=is_third_party
    )


def _machine_update_rows(product_name, machine):
    rows = ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).all()
    entries = MachineProductionEntry.query.filter_by(product_name=product_name, machine=machine).all()
    by_material = {e.material: e for e in entries}
    result = []
    for row in rows:
        data = row.to_dict()
        entry = by_material.get(row.material)
        if entry and entry.batch:
            saved_batches = entry.batch
        else:
            saved_batches = row.old_batches if machine == 'old' else row.new_batches
        data.update({
            "batch": entry.batch if entry else "",
            "saved_consumed": entry.consumed if entry else "",
            "saved_output": entry.output if entry else "",
            "saved_batches": saved_batches,
        })
        result.append(data)
    return result


@app.route('/production-update/<path:product_name>/old-machine')
def old_machine_production_update(product_name):
    product = Product.query.filter(Product.name == product_name, Product.published.is_(True)).first()
    if not product:
        return redirect(url_for('finished_goods'))
    material_stock = _machine_update_rows(product_name, 'old')
    total_consume = sum((row.get('saved_consumed') or 0) for row in material_stock)
    old_row = ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).first()
    no_of_batches = (old_row.old_batches if old_row else 0) or 0
    return render_template('old_machine_update.html',
        product=product.to_dict(),
        material_stock=material_stock,
        total_consume=round(total_consume, 2) or '',
        no_of_batches=no_of_batches,
        active_page='inventory_product',
        active_product=product_name
    )


@app.route('/production-update/<path:product_name>/new-machine')
def new_machine_production_update(product_name):
    product = Product.query.filter(Product.name == product_name, Product.published.is_(True)).first()
    if not product:
        return redirect(url_for('finished_goods'))
    material_stock = _machine_update_rows(product_name, 'new')
    total_consume = sum((row.get('saved_consumed') or 0) for row in material_stock)
    new_row = ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).first()
    no_of_batches = (new_row.new_batches if new_row else 0) or 0
    return render_template('new_machine_update.html',
        product=product.to_dict(),
        material_stock=material_stock,
        total_consume=round(total_consume, 2) or '',
        no_of_batches=no_of_batches,
        active_page='inventory_product',
        active_product=product_name
    )


@app.route('/production-update/<path:product_name>/<machine>-machine/save', methods=['POST'])
def save_machine_production_update(product_name, machine):
    if machine not in ('old', 'new'):
        return redirect(url_for('finished_goods'))
    product = Product.query.filter(Product.name == product_name, Product.published.is_(True)).first()
    if not product:
        return redirect(url_for('finished_goods'))
    rows = ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).all()
    consumed_vals = request.form.getlist('consumed[]')
    batches_vals = request.form.getlist('no_of_batches[]')
    total_consumed = 0
    total_batches = 0
    for index, row in enumerate(rows):
        qty = row.old_machine_qty if machine == 'old' else row.new_machine_qty
        raw_consumed = consumed_vals[index] if index < len(consumed_vals) else ''
        try: consumed = float(raw_consumed) if str(raw_consumed).strip() != '' else 0
        except ValueError: consumed = 0
        consumed = round(max(consumed, 0), 2)
        raw_batches = batches_vals[index] if index < len(batches_vals) else ''
        try: n_batches = int(float(raw_batches)) if str(raw_batches).strip() != '' else 0
        except ValueError: n_batches = 0
        n_batches = max(n_batches, 0)
        total_consumed += consumed
        total_batches += n_batches
        entry = MachineProductionEntry.query.filter_by(product_name=product_name, material=row.material, machine=machine).first()
        if not entry:
            entry = MachineProductionEntry(product_name=product_name, material=row.material, machine=machine)
            db.session.add(entry)
        entry.batch = str(n_batches) if n_batches else ''
        entry.quantity = qty
        entry.consumed = consumed
        entry.output = round(consumed / 50.0, 2)
        if machine == 'old':
            row.old_batches = n_batches
        else:
            row.new_batches = n_batches
    db.session.commit()
    return redirect(url_for('old_machine_production_update' if machine == 'old' else 'new_machine_production_update', product_name=product_name, saved=1))


@app.route('/production-update/<path:product_name>/save', methods=['POST'])
def save_combined_production_update(product_name):
    product = Product.query.filter(Product.name == product_name, Product.published.is_(True)).first()
    is_third_party = False
    if not product:
        third_item = ThirdPartyItem.query.filter(ThirdPartyItem.name == product_name, ThirdPartyItem.published.is_(True)).first()
        if not third_item:
            return redirect(url_for('finished_goods'))
        is_third_party = True
    rows = (ProductMaterial.query.filter_by(product_name=product_name).order_by(ProductMaterial.id).all()
            if not is_third_party
            else FormulaSheet.query.filter_by(product=product_name).order_by(FormulaSheet.id).all())
    selected_materials = request.form.getlist('selected_materials[]')
    if not is_third_party and selected_materials:
        resource_rows = Resource.query.filter(Resource.group.in_(('Raw Materials', 'Medicines', 'Chemicals'))).all()
        resource_by_name = {str(resource.name).strip(): resource for resource in resource_rows}
        existing_by_name = {str(row.material).strip(): row for row in rows}
        selected_rows = []
        for raw_name in selected_materials:
            name = str(raw_name or '').strip()
            if not name or name in {str(row.material).strip() for row in selected_rows}:
                continue
            if name in existing_by_name:
                selected_rows.append(existing_by_name[name])
            elif name in resource_by_name:
                selected_rows.append(SelectedMaterialRow(resource_by_name[name]))
        if selected_rows:
            rows = selected_rows
    def _num(raw):
        try:
            value = float(raw)
            if value != value or value in (float('inf'), float('-inf')):
                return 0
            return max(int(value) if str(raw).strip() != '' else 0, 0)
        except (TypeError, ValueError):
            return 0
    old_batches_list = request.form.getlist('old_batches[]')
    new_batches_list = request.form.getlist('new_batches[]')
    bags_list = request.form.getlist('total_bags[]')
    today = datetime.now().strftime('%Y-%m-%d')
    prod_date_raw = (request.form.get('production_date') or '').strip() or today
    try:
        prod_date = datetime.strptime(prod_date_raw, '%Y-%m-%d').strftime('%Y-%m-%d')
    except ValueError:
        prod_date = today
    is_today = prod_date == today
    _form_shift = str(request.form.get('shift', '') or '').strip().lower()
    _session_shift = str(session.get('savi_user_shift', '') or '').strip().lower()
    if _session_shift in ('morning', 'night'):
        # Locked to the shift chosen at login - a tampered form value cannot
        # write the other shift's rows.
        current_shift = _session_shift
    else:
        current_shift = _form_shift if _form_shift in ('morning', 'night') else _active_shift_value()
    if prod_date > today:
        # Item 3 (2026-10-05): a future production date is refused outright
        # instead of being silently moved to today - the user was saving values
        # onto a date they never picked and the success dialog hid it.
        return redirect(url_for('inventory_product', product_name=product_name, error=4,
                                date=today, shift=current_shift, selected=selected_materials))
    supervisor_name = (request.form.get('supervisor_name') or '').strip()
    old_bags_raw = (request.form.get('old_bags') or '').strip()
    new_bags_raw = (request.form.get('new_bags') or '').strip()

    def _is_whole(raw):
        """Change #41: the five mandatory Production Update fields must be WHOLE
        numbers >= 0. '3' and '3.0' pass (integral floats), but '12.5', 'abc',
        a negative or a blank is refused instead of being silently rounded -
        the error=1 banner already promised whole numbers."""
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return False
        return value >= 0 and float(value).is_integer()

    if (not supervisor_name or not old_bags_raw or not new_bags_raw
            or not _is_whole(old_bags_raw) or not _is_whole(new_bags_raw)):
        return redirect(url_for('inventory_product', product_name=product_name, error=1, date=prod_date, shift=current_shift, selected=selected_materials))
    # Item 3 (2026-10-01): Old Machine Batch and New Machine Batch are mandatory
    # for EVERY product - a blank/missing/non-whole batch input on any row blocks
    # the save before any write (same style as the Supervisor Name / bags guard
    # above).
    if (len(old_batches_list) < len(rows) or len(new_batches_list) < len(rows)
            or any(not _is_whole(v) for v in old_batches_list[:len(rows)])
            or any(not _is_whole(v) for v in new_batches_list[:len(rows)])):
        return redirect(url_for('inventory_product', product_name=product_name, error=3, date=prod_date, shift=current_shift, selected=selected_materials))
    def _whole_bags(raw):
        try:
            return int(round(max(float(raw), 0)))
        except (TypeError, ValueError):
            return 0
    old_bags_int = _whole_bags(old_bags_raw)
    new_bags_int = _whole_bags(new_bags_raw)
    # Product.stock tracks finished-goods bags (produced - sold). Read the bags
    # THIS shift already has stored BEFORE writing, so the stock change below is
    # only the difference this save introduces (re-saving the same bags must not
    # add them again - that was the old double-count bug).
    prev_bags_total = 0
    if not is_third_party:
        for prev_daily in MaterialDaily.query.filter_by(product_name=product_name,
                                                        date=prod_date,
                                                        shift=current_shift).all():
            if prev_daily.old_bags is not None:
                prev_row_total = int(prev_daily.old_bags or 0) + int(prev_daily.new_bags or 0)
                if prev_row_total > prev_bags_total:
                    prev_bags_total = prev_row_total
    res_closing = _ClosingMap()
    try:
        for res in Resource.query.all():
            res_closing.put(res.name, res.closing or 0)
    except Exception:
        res_closing = _ClosingMap()
    # Pre-validate BEFORE any write: today's consumed must be <= the stock
    # opening for every material (user request 2026-09-29). If any material
    # would go negative, block the whole save with a clear message.
    OVER_MSG = "Number of batches is not entered properly. It is exceeding the stock. Check and update."
    for idx, row in enumerate(rows):
        old_batch = _num(old_batches_list[idx]) if idx < len(old_batches_list) else 0
        new_batch = _num(new_batches_list[idx]) if idx < len(new_batches_list) else 0
        daily = MaterialDaily.query.filter_by(product_name=product_name, material=row.material,
                                              date=prod_date, shift=current_shift).first()
        if is_third_party:
            if idx >= len(old_batches_list):
                old_batch = _num(getattr(row, 'old_machine', 0))
            # Item 2: qty comes from the formula (admin's uploaded quantity) -
            # the over-stock check must use the same qty x batch math the write
            # below uses, for BOTH machines.
            old_qty = getattr(row, 'old_machine', 0) or 0
            new_qty = getattr(row, 'new_machine', 0) or 0
            total_consumed = round((old_qty or 0) * old_batch + (new_qty or 0) * new_batch, 2)
        else:
            old_qty = getattr(row, 'old_machine_qty', None)
            new_qty = getattr(row, 'new_machine_qty', None)
            if old_qty is None:
                old_qty = getattr(row, 'old_machine', 0) or 0
            if new_qty is None:
                new_qty = getattr(row, 'new_machine', 0) or 0
            total_consumed = round((old_qty or 0) * old_batch + (new_qty or 0) * new_batch, 2)
        opening = _shift_opening(product_name, row.material, prod_date, current_shift,
                                 res_closing.get(str(row.material).strip(), getattr(row, 'opening', 0) or 0))
        _daily_pref = MaterialDaily.query.filter_by(
            product_name=product_name, material=row.material,
            date=prod_date, shift=current_shift).first()
        if _daily_pref is not None and (_daily_pref.opening or 0) > 0:
            # F1: a RE-save must be judged against the SAME opening the user
            # already saw and stored, not a freshly recomputed chain that can
            # drift lower (the chain skips <=0 rows and the Resource fallback
            # drops when stock is consumed) - otherwise a legitimate re-save is
            # falsely blocked by the over-stock guard.
            opening = int(_daily_pref.opening)
        if total_consumed > (opening or 0) + 0.001:
            return redirect(url_for('inventory_product', product_name=product_name, error=2,
                                    date=prod_date, shift=current_shift, selected=selected_materials,
                                    over=row.material))
    for idx, row in enumerate(rows):
        old_batch = _num(old_batches_list[idx]) if idx < len(old_batches_list) else 0
        new_batch = _num(new_batches_list[idx]) if idx < len(new_batches_list) else 0
        daily = MaterialDaily.query.filter_by(product_name=product_name, material=row.material,
                                              date=prod_date, shift=current_shift).first()
        prev_daily_consumed = int(daily.consumed or 0) if daily is not None else 0
        if is_third_party:
            # Item 2+3 (2026-10-01): formula Old/New Machine values are the
            # admin's per-batch QUANTITY; the user types only the batches
            # (mandatory - checked before any write). Both Qty columns are text.
            # consumed = qty x batch for both machines, like standard products.
            # daily.batch stores '<typed old>/<typed new>' unconditionally and
            # daily.old_qty/new_qty keep the formula qty used for this save.
            if idx >= len(old_batches_list):
                # stale page without the Old-batch inputs -> keep the formula count
                old_batch = _num(getattr(row, 'old_machine', 0))
            old_qty = getattr(row, 'old_machine', 0) or 0
            new_qty = getattr(row, 'new_machine', 0) or 0
            old_consumed = round(old_qty * old_batch, 2)
            new_consumed = round(new_qty * new_batch, 2)
            stored_old_qty = old_qty
            stored_new_qty = new_qty
        else:
            old_qty = getattr(row, 'old_machine_qty', None)
            new_qty = getattr(row, 'new_machine_qty', None)
            if old_qty is None:
                old_qty = getattr(row, 'old_machine', 0) or 0
            if new_qty is None:
                new_qty = getattr(row, 'new_machine', 0) or 0
            old_qty = old_qty or 0
            new_qty = new_qty or 0
            old_consumed = round(old_qty * old_batch, 2)
            new_consumed = round(new_qty * new_batch, 2)
            stored_old_qty = None
            stored_new_qty = None
        opening = _shift_opening(product_name, row.material, prod_date, current_shift,
                                 res_closing.get(str(row.material).strip(), getattr(row, 'opening', 0) or 0))
        if daily is not None and (daily.opening or 0) > 0:
            # F1: keep the STORED opening on a re-save - a freshly recomputed
            # chain can drift lower and would silently corrupt opening/closing.
            opening = int(daily.opening)
        total_consumed = round(old_consumed + new_consumed, 2)
        if not daily:
            daily = MaterialDaily(product_name=product_name, material=row.material,
                                  date=prod_date, shift=current_shift)
            db.session.add(daily)
        daily.shift = current_shift
        daily.opening = int(opening)
        daily.consumed = int(round(total_consumed))
        daily.closing = max(int(opening) - int(round(total_consumed)), 0)
        # F2: tolerant matcher (exact -> normalized -> fuzzy) so a material
        # typed slightly differently on the formula vs Resource Maintenance
        # ('Maize Doc' vs 'Maize DOC') still updates the right stock row -
        # the same helper Purchases already use.
        live_res = _purchase_resource(str(row.material))
        if live_res is not None:
            # Only the CHANGE since this shift's last save touches the resource
            # (a re-save with the same numbers must not subtract again), and it
            # goes through consumed so the closing formula keeps it.
            res_delta = int(round(total_consumed)) - prev_daily_consumed
            if res_delta:
                live_res.consumed = max((live_res.consumed or 0) + res_delta, 0)
                live_res.recompute()
        if is_third_party:
            # Store the pair unconditionally (even 0/0) so a typed 0 survives;
            # the reader falls back to formula values only when NOTHING stored.
            daily.batch = '%s/%s' % (old_batch, new_batch)
            daily.old_qty = stored_old_qty
            daily.new_qty = stored_new_qty
        else:
            daily.batch = ('%s/%s' % (old_batch, new_batch)) if (old_batch or new_batch) else ''
        daily.old_bags = old_bags_int
        daily.new_bags = new_bags_int
        if supervisor_name:
            daily.supervisor_name = supervisor_name
        if is_today:
            pair = (('old', old_qty, old_consumed, old_batch), ('new', new_qty, new_consumed, new_batch))
            for machine, qty, consumed, batches in pair:
                entry = MachineProductionEntry.query.filter_by(product_name=product_name, material=row.material, machine=machine).first()
                if not entry:
                    entry = MachineProductionEntry(product_name=product_name, material=row.material, machine=machine)
                    db.session.add(entry)
                entry.batch = str(batches) if batches else ''
                entry.quantity = qty
                entry.consumed = consumed
                entry.output = round(consumed / 50.0, 2)
            row.old_batches = old_batch
            row.new_batches = new_batch
            if idx < len(bags_list):
                try:
                    row.total_bags = max(float(bags_list[idx]) if str(bags_list[idx]).strip() != '' else 0, 0)
                except (TypeError, ValueError):
                    pass
    db.session.commit()
    if not is_third_party:
        bags_delta = (int(old_bags_int) + int(new_bags_int)) - prev_bags_total
        if bags_delta:
            product.stock = max((product.stock or 0) + bags_delta, 0)
        if is_today:
            product.old_machine_bags = old_bags_int
            product.new_machine_bags = new_bags_int
        product.status = compute_product_status(product.stock or 0)
        product.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
    else:
        # Change #53: stamp the 3rd-party item so the Product Details
        # "Last Update" column follows the latest production save
        # (same format as Product.last_update).
        third_item.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
    db.session.commit()
    # Item 4 (2026-10-05): after a successful save the entry form must come back
    # EMPTY - supervisor, bags, batch cells and the materials the user picked
    # via ?selected= all clear. The values are already in material_daily, so the
    # date picker (which reloads without ?saved=1) brings them back when they
    # return to that date. Error redirects keep selected= so nothing typed is lost.
    return redirect(url_for('inventory_product', product_name=product_name, saved=1,
                            date=prod_date, shift=current_shift))


@app.route('/medicines')
def medicines():
    products = Product.query.filter(Product.published.is_(True)).order_by(Product.id).all()
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
    products = Product.query.filter(Product.published.is_(True)).order_by(Product.id).all()
    third_party = ThirdPartyItem.query.filter(ThirdPartyItem.published.is_(True)).order_by(ThirdPartyItem.id).all()
    product_names = [p.name for p in products] + [t.name for t in third_party]
    selected_product = request.args.get('product_name', '').strip()
    if selected_product not in product_names:
        selected_product = ''
    formula_materials = []
    if selected_product:
        rows = FormulaSheet.query.filter_by(product=selected_product).order_by(FormulaSheet.id).all()
        formula_materials = [row for row in rows]

    return render_template('production.html',
        products=[p.to_dict() for p in products],
        product_names=product_names,
        selected_product=selected_product,
        formula_materials=formula_materials,
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
        vendor_data=[v.to_dict() for v in Vendor.query.order_by(Vendor.vendor_name).all()],
        product_options=_vendor_product_options(),
        status_filter=status_filter, active_page='purchase_maintenance')


@app.route('/reports')
def reports():
    material_filter = request.args.get('material', 'all')
    report_start = request.args.get('start', '2026-05-01')
    report_end = request.args.get('end', '2026-09-05')

    products = Product.query.filter(Product.published.is_(True)).order_by(Product.id).all()

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
    inventory_rows = inventory_all if material_filter == 'all' else [r for r in inventory_all if r['material'] == material_filter]

    total_stock_value = sum(r['value'] for r in inventory_rows)
    total_materials = len(inventory_rows)
    total_batches = sum(r['batches'] for r in inventory_rows)
    need_attention = sum(1 for r in inventory_rows if r['status'] in ('Critical', 'Low'))
    largest_holdings = sorted(inventory_rows, key=lambda r: r['on_hand'], reverse=True)

    purchases = Purchase.query.order_by(Purchase.id.desc()).all()
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
    materials = analysis_all if material_filter == 'all' else [r for r in analysis_all if r['material'] == material_filter]
    alerts = [r for r in analysis_all if r['status'] in ('Low Stock', 'Critical')]
    if material_filter != 'all':
        alerts = [r for r in alerts if r['material'] == material_filter]

    batches = [p.to_dict() for p in ProductionEntry.query.order_by(ProductionEntry.date.desc()).all()]
    material_names = [p.name for p in products]

    return render_template('reports.html',
        inventory_rows=inventory_rows, largest_holdings=largest_holdings,
        total_stock_value=total_stock_value, total_materials=total_materials,
        total_batches=total_batches, need_attention=need_attention,
        purchase_rows=purchase_rows, purchase_total=purchase_total,
        outstanding_total=outstanding_total, materials=materials, alerts=alerts,
        batches=batches, material_names=material_names,
        selected_material=material_filter, report_start=report_start,
        report_end=report_end, active_page='reports'
    )


@app.route('/settings', methods=['GET', 'POST'])
def settings():
    record = _get_settings()
    saved = False
    if request.method == 'POST':
        record.company_name = request.form.get('company_name', '').strip() or record.company_name
        record.location = request.form.get('location', '').strip() or record.location
        record.currency_symbol = request.form.get('currency_symbol', '').strip() or '₹'
        # The "Inventory Rules" card was removed from this page: only touch
        # those settings when a (old) form still submits them, so a normal
        # Save never silently resets the stored values.
        if 'allocation_rule' in request.form:
            record.allocation_rule = request.form.get('allocation_rule', 'FIFO')
        if 'low_stock_notifications' in request.form:
            record.low_stock_notifications = True
        if 'daily_digest' in request.form:
            record.daily_digest = True
        db.session.commit()
        saved = True
    return render_template('settings.html', settings=record.to_dict(), saved=saved, active_page='settings')


def _active_shift():
    """Return (is_logged_in, shift_label, user_name) for the user session.

    Authentication is OPTIONAL (per requested scope): visitors may browse all
    pages without logging in. When a user DOES log in, their name + shift are
    kept in the session and its `shift` determines how new production entries
    are tagged for the admin's morning/night production update columns.
    """
    name = session.get('savi_user_name', '').strip()
    shift = session.get('savi_user_shift', '')
    if not name:
        return False, '', ''
    return True, shift, name


def _normalize_credentials(name, password):
    """Return (normalized_name, shift) if the credentials are valid, else (None, '').

    Password rule (as requested): exactly 6 characters.
      * first 3 letters 'MOR' + 3 digits  -> morning shift
      * first 3 letters 'NIG' + 3 digits  -> night shift
    Shift is decided purely from the password prefix, not the name.
    """
    name = str(name or '').strip()
    pwd = str(password or '').strip()
    if not name or len(pwd) != 6:
        return None, ''
    head = pwd[:3].upper()
    if head == 'MOR' and pwd[3:].isdigit():
        return name, 'morning'
    if head == 'NIG' and pwd[3:].isdigit():
        return name, 'night'
    return None, ''


@app.before_request
def _require_user_login():
    """Block the whole user portal until a user account is logged in.

    Only login/signup, static assets and the health probe stay open.
    After a successful login the user is sent straight to the dashboard.
    """
    endpoint = request.endpoint or ''
    open_endpoints = {
        'login', 'signup', 'static', 'api_health',
        'shift_login', 'shift_logout',  # legacy redirects kept for safety
        'api_auth_state', 'api_contact_info', 'api_login_message',  # login-page helpers
    }
    if endpoint in open_endpoints:
        return None
    if endpoint.startswith('static_') or endpoint.startswith('_static'):
        return None
    if session.get('savi_user_id'):
        # Security: admin members must not keep a user-portal session (they
        # were allowed before the auth split) - kick them back to login.
        if session.get('savi_user_role') == 'admin':
            try:
                _clear_active_user()
            except Exception:
                pass
            session.clear()
            return redirect(url_for('login'))
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


def _product_image_blob(filename):
    """Dashboard photo stored in the database by the admin app (Vercel has no
    S3 bucket), so the user portal still shows the photo after a reload."""
    base = os.path.basename(str(filename or ''))
    if not base:
        return None
    for model in (Product, ThirdPartyItem):
        row = model.query.filter_by(image=base).first()
        if row is not None and getattr(row, 'image_data', None):
            return bytes(row.image_data), (row.image_mime or 'image/jpeg')
    return None


@app.route('/product-images/<path:filename>')
def product_image(filename):
    return _send_stored_file('products', filename,
                             os.path.join(BASE_DIR, 'static', 'img', 'products'),
                             blob_lookup=_product_image_blob)


@app.route('/shift-login', methods=['GET', 'POST'])
def shift_login():
    return redirect(url_for('login'))


@app.route('/shift-logout')
def shift_logout():
    return redirect(url_for('logout'))


def _no_store(resp):
    """Keep login pages out of the back/forward cache (Back button fix)."""
    if not hasattr(resp, 'headers'):
        resp = make_response(resp)
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


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
        'login.html', error=msg, is_user=True, selected_shift=''), 429))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('savi_user_id'):
        return _no_store(redirect(url_for('dashboard')))
    error = ''
    posted_shift = (request.form.get('shift') or '').strip().lower() if request.method == 'POST' else ''
    if request.method == 'POST':
        login_id = request.form.get('login_id', '').strip()
        password = request.form.get('password', '')
        _ip = _login_client_ip()
        if _login_lock_remaining(_ip, login_id) > 0:
            return _login_throttled_response(login_id)
        if not re.fullmatch(r'\d{6}', login_id):
            _login_record_failure(_ip, login_id)
            error = 'Login ID must be exactly 6 digits (numbers only).'
        else:
            user = User.query.filter_by(login_id=login_id).first()
            # Security (2026-09-27): the user portal accepts ONLY role='user'
            # accounts. Admin members log in on the admin portal only.
            can_login = bool(user) and user.role == 'user' and check_password_hash(user.password, password)
            if not can_login:
                _login_record_failure(_ip, login_id)
                error = 'Invalid ID or password, or this account is not allowed to log in.'
            else:
                _login_clear_failures(_ip, login_id)
                # Shift is CHOSEN AT EVERY LOGIN (2026-09-28): to change shift the
                # user logs out and picks Morning/Night at login. The choice is
                # persisted to the account so the admin User Access table always
                # shows the user's current shift. Requests that send no shift
                # (older pages / API-style logins) keep the account's shift.
                if posted_shift in ('morning', 'night'):
                    chosen_shift = posted_shift
                else:
                    chosen_shift = str(user.shift or '').strip().lower()
                    if chosen_shift not in ('morning', 'night'):
                        chosen_shift = 'morning'
                if (user.shift or '') != chosen_shift:
                    user.shift = chosen_shift
                    db.session.commit()
                # Change #42: clear any pre-login cookie state (session
                # fixation) and stamp the absolute session lifetime clock.
                session.clear()
                session['savi_user_id'] = user.id
                session['savi_user_name'] = user.full_name
                session['savi_user_shift'] = chosen_shift
                session['savi_user_role'] = user.role
                session['savi_auth_at'] = time.time()
                # Change #50 (2026-10-07): the shift chosen HERE at login is
                # also written to the shared settings.active_shift, so the
                # ADMIN Production Update page opens on (and keeps offering)
                # the same shift the user just picked - "recognize the shift
                # automatically when user selects it in the login page".
                # The admin's own Morning/Night buttons still write it too
                # (last action wins); the user portal's own pages keep using
                # the session shift first, so nothing else changes.
                _set_active_shift(chosen_shift)
                _set_active_user(user)
                _bump_change_version()
                return _no_store(redirect(url_for('dashboard')))
    return _no_store(render_template('login.html', error=error, is_user=True,
                                     selected_shift=posted_shift))


@app.route('/api/auth-state')
def api_auth_state():
    """Public: is this browser already logged in? Used by the login page's
    Back-button handler to bounce straight back to the dashboard."""
    return _ok({"logged_in": bool(session.get('savi_user_id'))})


@app.route('/api/contact-info')
def api_contact_info():
    """Public: the admin contact number configured in admin Settings, so the
    login page can offer a call/WhatsApp link ('contact the admin'). Falls back
    to the owner's recovery number when Settings has none yet."""
    phone = (_get_settings().admin_contact_phone or '').strip()
    if not phone:
        phone = DEFAULT_ADMIN_CONTACT_PHONE
    return _ok({"phone": phone})


@app.route('/api/messages/login-send', methods=['POST'])
def api_login_message():
    """Public: a short message from a (possibly locked-out) visitor to the admin.
    Deliberately open - it is the only way a person who cannot log in can reach
    the admin. The message shows up in the admin inbox like any other."""
    data = request.get_json(silent=True) or {}
    text = str(data.get('message', '')).strip()
    if not text:
        return _fail("Message cannot be empty")
    if len(text) > 500:
        return _fail("Message is too long (max 500 characters)")
    # Basic flood guard: one unauthenticated message per session per 30s.
    now = time.time()
    last = session.get('_last_login_msg', 0)
    if now - float(last or 0) < 30:
        return _fail("Please wait a moment before sending another message", 429)
    session['_last_login_msg'] = now
    msg = Message(text=text, sender="User",
                  time=datetime.now().strftime('%Y-%m-%d %H:%M'), seen_by="User")
    db.session.add(msg)
    db.session.commit()
    return _ok({"success": True, "message": "Message sent to the admin."})


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    return _no_store(redirect(url_for('login')))


@app.route('/logout')
def logout():
    _clear_active_user()
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


@app.route('/api/admin-change-monitor')
def api_admin_change_monitor():
    """Version token bumped by admin-portal writes so this UI can prompt a refresh."""
    return _ok({"version": _admin_change_version_value()})


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
    )
    db.session.add(product)
    db.session.commit()
    return jsonify({"success": True, "product": product.to_dict()}), 201


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
    product.status = compute_product_status(product.stock)
    db.session.commit()
    return _ok({"product": product.to_dict()})


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
        # An UNCHANGED save is not a stock take. The edit box is prefilled
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
        # Update flow).
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
            "consumed": row.consumed, "batch": row.batch or ""}


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
    db.session.commit()
    _pm_record(row.product_name, {"t": "edit", "id": row.id,
                                  "before": before, "after": _pm_snapshot(row)})
    return _ok({"material": row.to_dict(), **_pm_counts(row.product_name)})


@app.route('/api/product-materials/bulk', methods=['PUT'])
def api_pm_bulk():
    """Update consumed values for many rows at once (Add Consumed).

    Body: {product_name, items: [{id, consumed}]}. Closing = opening -
    consumed is recalculated per row; today's daily log is synced.
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
    for it in items:
        if not isinstance(it, dict) or 'id' not in it:
            continue
        try:
            rid = int(it['id'])
            consumed = int(it.get('consumed', 0))
        except (TypeError, ValueError):
            return _fail("id and consumed must be numbers")
        row = rows.get(rid)
        if not row:
            db.session.rollback()
            return _fail(f"Row {rid} not found in {product_name}", 404)
        if consumed < 0:
            return _fail(f"{row.material}: consumed cannot be negative")
        if consumed > row.opening:
            db.session.rollback()
            return _fail(f"{row.material}: consumed ({consumed}) exceeds opening ({row.opening})", 409)
        if consumed != row.consumed:
            changes.append({"id": rid, "before": _pm_snapshot(row),
                            "after": {**_pm_snapshot(row), "consumed": consumed,
                                      "closing": row.opening - consumed}})
    if not changes:
        return _ok({"updated": [], **_pm_counts(product_name)})
    today = datetime.now().strftime("%Y-%m-%d")
    updated = []
    for ch in changes:
        row = rows[ch["id"]]
        _pm_apply(row, ch["after"])
        daily = MaterialDaily.query.filter_by(
            product_name=product_name, material=row.material, date=today).first()
        if daily:
            daily.consumed = row.consumed
            daily.closing = row.closing
        updated.append(row.to_dict())
    db.session.commit()
    _pm_record(product_name, {"t": "bulk", "changes": changes})
    return _ok({"updated": updated, **_pm_counts(product_name)})


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
    elif e["t"] == "bulk":
        for ch in e["changes"]:
            row = ProductMaterial.query.get(ch["id"])
            if not row:
                db.session.rollback()
                h["undo"].append(e)
                return _fail("A row no longer exists, cannot undo")
            _pm_apply(row, ch["before"])
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
    elif e["t"] == "bulk":
        for ch in e["changes"]:
            row = ProductMaterial.query.get(ch["id"])
            if not row:
                db.session.rollback()
                h["redo"].append(e)
                return _fail("A row no longer exists, cannot redo")
            _pm_apply(row, ch["after"])
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
    entry = ProductionEntry(product=product, date=datetime.now().strftime('%Y-%m-%d'),
                            quantity=quantity, batch=batch, status=status, notes=notes)
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
                                batch=batch, status=status, notes=notes)
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


def _compute_credit_due(start_value, credit_days):
    """Due date = start (arrival/purchase) + credit_days. Returns dd-mm-yyyy or ''."""
    try:
        days = int(credit_days or 0)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return ""
    start = _parse_ddmm(start_value) or date.today()
    return (start + timedelta(days=days)).strftime("%d-%m-%Y")


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


def _valid_vehicle_number(value):
    """Uppercase letters + digits + spaces/hyphens, must contain both a letter and a digit.

    The client auto-inserts hyphens after every 2 characters (KA-01-AB-1234);
    legacy rows use spaces (KA 01 AB 1234) - both are accepted.
    """
    value = re.sub(r'\s+', ' ', str(value or '').strip().upper())
    if not value:
        return ''
    if not re.fullmatch(r'[A-Z0-9 \-]{2,20}', value):
        return None
    if not (re.search(r'[A-Z]', value) and re.search(r'\d', value)):
        return None
    return value


class SlipSaveError(Exception):
    """User-facing problem with an uploaded slip (wrong type, too large, empty)."""


def _prepare_slip(file_storage, folder, allowed, prefix, type_label):
    """Validate an uploaded slip and store it durably.

    Returns (filename, blob, mime). `blob` is bytes only when the file was
    stored in the shared database — the Vercel deployment has no durable
    filesystem and no S3 credentials, so without this fallback every upload
    fails with 503. When blob is not None the caller must persist it on the
    row's slip_data/slip_mime columns (slip_attachment still holds the
    filename so the View links keep working). Returns ('', None, '') when no
    file was selected. Raises SlipSaveError for user-facing problems and
    FileStorage* errors for storage outages.
    """
    if not file_storage or not file_storage.filename:
        return '', None, ''
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in allowed:
        raise SlipSaveError(f"Slip attachment must be a {type_label} file")
    safe = secure_filename(os.path.splitext(file_storage.filename)[0]) or 'slip'
    stamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
    filename = f"{prefix}_{stamp}_{safe[:40]}{ext}"
    max_mb = max(_max_file_bytes() // (1024 * 1024), 1)
    if storage_status() == 'unconfigured':
        # Serverless deployment without S3: keep the bytes in the DB.
        data = file_storage.read()
        if not data:
            raise SlipSaveError("The selected file is empty")
        if len(data) > _max_file_bytes():
            raise SlipSaveError(f"The file is too large (max {max_mb} MB)")
        mime = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        return filename, data, mime
    try:
        save_upload(file_storage, folder, filename, allowed)
        # Store the bare filename (not save_upload's folder-prefixed key) so
        # /driver-slips/<name> links round-trip cleanly.
        return filename, None, ''
    except ValueError as exc:
        message = str(exc)
        if 'too large' in message:
            raise SlipSaveError(f"The file is too large (max {max_mb} MB)")
        if 'empty' in message:
            raise SlipSaveError("The selected file is empty")
        raise SlipSaveError(f"Slip attachment must be a {type_label} file")


@app.route('/purchase-slips/<path:filename>')
def purchase_slip(filename):
    return _send_stored_file('purchases', filename, PURCHASE_UPLOAD_DIR)


@app.route('/driver-slips/<path:filename>')
def driver_slip(filename):
    return _send_stored_file('driver_slips', filename, DRIVER_UPLOAD_DIR)


@app.route('/sales-invoices/<path:filename>')
def sales_invoice(filename):
    return _send_stored_file('sales_slips', filename, SALES_UPLOAD_DIR)


def _sale_now():
    return datetime.now().strftime("%d %b %Y, %I:%M %p")


def _sale_date_str(data):
    """Date field helper for the sales APIs: returns (dd-mm-yyyy, error)."""
    date_str = str(data.get('date', '')).strip()
    if not date_str:
        return None, "Date is required"
    if _parse_ddmm(date_str) is None:
        return None, "Date must be a valid dd-mm-yyyy date"
    return date_str, None


def _sale_int(value, label):
    text = str(value if value is not None else '').strip().replace(',', '')
    if not text:
        return None, f"{label} is required"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None, f"{label} must be a number"
    if number != number or number in (float('inf'), float('-inf')):
        # F4: NaN/inf (e.g. "nan", "1e999") are not valid money counts and
        # would crash int() / corrupt the DB.
        return None, f"{label} must be a finite number"
    if number != int(number):
        return None, f"{label} must be a whole number"
    return int(number), None


def _sale_raw(data, key):
    """Optional field as a stripped string. NOTE: never use `data.get(k, '')
    or ''` for money fields - a JSON int 0 is falsy, so the `or` collapses it
    to '' and the typed zero is silently dropped (Change #52 bug)."""
    value = data.get(key, '')
    if value is None:
        return ''
    return str(value).strip()


def _sale_float(value, label):
    """Change #60: optional decimal number (the Globle Discount amount or
    percentage allow paise/decimals, unlike the whole-rupee money fields)."""
    text = str(value if value is not None else '').strip().replace(',', '')
    if not text:
        return None, f"{label} is required"
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None, f"{label} must be a number"
    if number != number or number in (float('inf'), float('-inf')):
        # F4: reject NaN/inf (e.g. "nan", "1e999") instead of storing them.
        return None, f"{label} must be a finite number"
    return number, None


def _ps_discount_check(data, eff_total):
    """Change #60: parse the Globle Discount sent to the product-sales API.
    Returns (amount, percent, error). Amount wins over percentage when both
    are typed (the modal keeps them mutually exclusive); percent must be
    0..100, neither may be negative, and the resulting discount may not
    exceed the effective Total Amount (the typed total, else the same
    qty x price fallback the render uses)."""
    discount_amount = None
    raw_amt = _sale_raw(data, 'discount_amount')
    if raw_amt:
        discount_amount, error = _sale_float(raw_amt, 'Globle discount amount')
        if error:
            return None, None, error
        if discount_amount < 0:
            return None, None, "Globle discount amount cannot be negative"
    discount_percent = None
    raw_pct = _sale_raw(data, 'discount_percent')
    if raw_pct:
        discount_percent, error = _sale_float(raw_pct, 'Globle discount percentage')
        if error:
            return None, None, error
        if discount_percent < 0 or discount_percent > 100:
            return None, None, "Globle discount percentage must be between 0 and 100"
    if discount_amount is not None:
        value = discount_amount
    elif discount_percent is not None:
        value = float(eff_total) * discount_percent / 100.0
    else:
        value = 0.0
    if value > float(eff_total) + 1e-9:
        return None, None, "Globle discount cannot be more than the Total Amount"
    return discount_amount, discount_percent, None


# ---------- Change #56 (2026-10-09): multi-product Add Sales ----------
def _parse_ps_items(raw):
    """Write path: parse the Add Sales `items` field (JSON list, or the same
    JSON as a FormData string) into [{"product","qty","price"}] - `price` is
    the optional per-product Price box from Change #59 (None = blank).
    Returns (items, error). Duplicate products are aggregated; the first
    non-blank price wins."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None, "Product is required"
    if not isinstance(parsed, list) or not parsed:
        return None, "Product is required"
    order, agg, prices = [], {}, {}
    for entry in parsed:
        if not isinstance(entry, dict):
            return None, "Product is required"
        name = str(entry.get('product', '') or '').strip()
        if not name:
            return None, "Product is required"
        qty, error = _sale_int(entry.get('qty', entry.get('quantity')), 'Quantity')
        if error:
            return None, error
        if qty <= 0:
            return None, "Quantity must be more than 0"
        # Change #59: optional per-product Price box (blank = None).
        price = None
        raw_price = entry.get('price', '')
        if raw_price is not None and str(raw_price).strip() != '':
            try:
                price = float(str(raw_price).strip().replace(',', ''))
            except (TypeError, ValueError):
                return None, "Price must be a non-negative number"
            if price < 0:
                return None, "Price must be a non-negative number"
        if name not in agg:
            order.append(name)
            agg[name] = qty
            prices[name] = price
        else:
            agg[name] += qty
            if prices[name] is None:
                prices[name] = price
    return [{"product": name, "qty": agg[name], "price": prices[name]}
            for name in order], None


def _ps_dump_items(items):
    """Store the parsed item list; the optional per-product price (Change #59)
    is kept only when typed, so a blank-price sale keeps the exact Change #56
    JSON shape [{"product","qty"}]."""
    out = []
    for it in items:
        entry = {"product": it["product"], "qty": it["qty"]}
        if it.get("price") is not None:
            entry["price"] = it["price"]
        out.append(entry)
    return json.dumps(out)


def _ps_items(product, quantity, items_json):
    """Read path: [(product, qty)] for a STORED sale. `items` is the JSON list
    saved by Change #56; empty/NULL = a legacy single-product row, whose whole
    sale is the `product` + `quantity` columns."""
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


@app.route('/raw-materials-sales')
def raw_materials_sales():
    sales = RawMaterialSale.query.order_by(RawMaterialSale.id.desc()).all()
    materials = Resource.query.filter(Resource.group == 'Raw Materials').order_by(Resource.name).all()
    return render_template('raw_materials_sales.html',
                           sales=[s.to_dict() for s in sales],
                           materials=[m.to_dict() for m in materials],
                           active_page='raw_materials_sales')


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


@app.route('/api/raw-materials-sales', methods=['POST'])
def api_raw_materials_sales_create():
    data = request.get_json(silent=True) or request.form
    date_str, error = _sale_date_str(data)
    if error:
        return _fail(error)
    material = str(data.get('material', '')).strip()
    if not material:
        return _fail("Material is required")
    resource = Resource.query.filter(Resource.group == 'Raw Materials',
                                     Resource.name == material).first()
    if resource is None:
        return _fail("Material not found in Raw Materials")
    quantity, error = _sale_int(data.get('quantity'), 'Quantity')
    if error:
        return _fail(error)
    if quantity <= 0:
        return _fail("Quantity must be more than 0")
    available = int(resource.closing or 0)
    if quantity > available:
        return _fail(f"Only {available} kg available in stock for {material}")
    amount_raw = data.get('amount', '')
    if not str(amount_raw or '').strip():
        amount_raw = data.get('amount_received', '')
    amount = 0
    if str(amount_raw or '').strip():
        amount, error = _sale_int(amount_raw, 'Amount received')
        if error:
            return _fail(error)
        if amount < 0:
            return _fail("Amount received cannot be negative")
    buyer = str(data.get('buyer', '')).strip()
    customer_contact = str(data.get('customer_contact', '')).strip()
    if customer_contact and not re.fullmatch(r'\d{10}', customer_contact):
        return _fail("Customer contact number must be exactly 10 digits")
    bill_qty = 0
    if str(data.get('bill_quantity', '') or '').strip():
        bill_qty, error = _sale_int(data.get('bill_quantity'), 'Quantity in Bill')
        if error:
            return _fail(error)
        if bill_qty < 0:
            return _fail("Quantity in Bill cannot be negative")
    balance = 0
    if str(data.get('balance_amount', '') or '').strip():
        balance, error = _sale_int(data.get('balance_amount'), 'Balance amount')
        if error:
            return _fail(error)
        if balance < 0:
            return _fail("Balance amount cannot be negative")
    total_amount = 0
    if str(data.get('total_amount', '') or '').strip():
        total_amount, error = _sale_int(data.get('total_amount'), 'Total amount')
        if error:
            return _fail(error)
        if total_amount < 0:
            return _fail("Total amount cannot be negative")
    invoice_number = str(data.get('invoice_number', '')).strip()
    inv_name, inv_blob, inv_mime = '', None, ''
    if request.files.get('invoice_attachment'):
        try:
            inv_name, inv_blob, inv_mime = _prepare_slip(
                request.files.get('invoice_attachment'), 'sales_slips',
                ALLOWED_SALES_EXTENSIONS, 'rsale', 'JPG, JPEG, PNG or PDF')
        except SlipSaveError as exc:
            return _fail(str(exc))
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    weigh_name, weigh_blob, weigh_mime = '', None, ''
    if request.files.get('weighment_attachment'):
        try:
            weigh_name, weigh_blob, weigh_mime = _prepare_slip(
                request.files.get('weighment_attachment'), 'sales_slips',
                ALLOWED_SALES_EXTENSIONS, 'rsweigh', 'JPG, JPEG, PNG or PDF')
        except SlipSaveError as exc:
            return _fail(str(exc))
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    sale = RawMaterialSale(
        date=date_str, material=material, quantity=quantity,
        amount=amount, buyer=buyer,
        customer_contact=customer_contact,
        invoice_number=invoice_number,
        bill_quantity=bill_qty, balance_amount=balance,
        total_amount=total_amount,
        invoice_attachment=inv_name or '', invoice_data=inv_blob,
        invoice_mime=inv_mime or '',
        weighment_attachment=weigh_name or '', weighment_data=weigh_blob,
        weighment_mime=weigh_mime or '', created=_sale_now())
    db.session.add(sale)
    # The sale goes through Resource.sold, so the closing formula
    # (opening + purchased - consumed - sold) drops by exactly this quantity.
    resource.sold = (resource.sold or 0) + quantity
    resource.last_update = _sale_now()
    resource.recompute()
    db.session.flush()
    db.session.add(StockMovement(date=datetime.now().strftime('%d %b %Y'),
                                 material=material, movement='Sale Out',
                                 quantity=quantity, unit='kg',
                                 reference='RMS%s' % sale.id))
    db.session.commit()
    return jsonify({"success": True, "sale": sale.to_dict(),
                    "resource": resource.to_dict()}), 201


@app.route('/api/raw-materials-sales/<int:sale_id>', methods=['PUT'])
def api_raw_materials_sales_update(sale_id):
    sale = RawMaterialSale.query.get(sale_id)
    if not sale:
        return _fail("Sale not found", 404)
    # Change #54 (edit button): mirror the POST validations, then move the
    # stock by exactly the delta this edit introduces (old sale returns to the
    # old material, the new quantity comes out of the new material).
    data = request.get_json(silent=True) or request.form
    date_str = sale.date
    if 'date' in data and str(data.get('date') or '').strip():
        date_str, error = _sale_date_str(data)
        if error:
            return _fail(error)
    material = str(data.get('material', sale.material) or '').strip()
    if not material:
        return _fail("Material is required")
    resource = Resource.query.filter(Resource.group == 'Raw Materials',
                                     Resource.name == material).first()
    if resource is None:
        return _fail("Material not found in Raw Materials")
    quantity, error = _sale_int(data.get('quantity', sale.quantity), 'Quantity')
    if error:
        return _fail(error)
    if quantity <= 0:
        return _fail("Quantity must be more than 0")
    old_qty = int(sale.quantity or 0)
    old_material = sale.material
    same_material = (material == old_material)
    # The current closing already has THIS sale's old quantity deducted, so a
    # same-material edit may go up to closing + old_qty.
    available = int(resource.closing or 0) + (old_qty if same_material else 0)
    if quantity > available:
        return _fail(f"Only {available} kg available in stock for {material}")
    amount = int(sale.amount or 0)
    if 'amount' in data or 'amount_received' in data:
        raw_amount = _sale_raw(data, 'amount') or _sale_raw(data, 'amount_received')
        if raw_amount:
            amount, error = _sale_int(raw_amount, 'Amount received')
            if error:
                return _fail(error)
            if amount < 0:
                return _fail("Amount received cannot be negative")
    customer_contact = str(data.get('customer_contact', sale.customer_contact) or '').strip()
    if customer_contact and not re.fullmatch(r'\d{10}', customer_contact):
        return _fail("Customer contact number must be exactly 10 digits")
    bill_qty = int(sale.bill_quantity or 0)
    if 'bill_quantity' in data:
        raw_bill = _sale_raw(data, 'bill_quantity')
        if raw_bill:
            bill_qty, error = _sale_int(raw_bill, 'Quantity in Bill')
            if error:
                return _fail(error)
            if bill_qty < 0:
                return _fail("Quantity in Bill cannot be negative")
        else:
            bill_qty = 0
    balance = int(sale.balance_amount or 0)
    if 'balance_amount' in data:
        raw_balance = _sale_raw(data, 'balance_amount')
        if raw_balance:
            balance, error = _sale_int(raw_balance, 'Balance amount')
            if error:
                return _fail(error)
            if balance < 0:
                return _fail("Balance amount cannot be negative")
        else:
            balance = 0
    total_amount = int(sale.total_amount or 0)
    if 'total_amount' in data:
        raw_total = _sale_raw(data, 'total_amount')
        if raw_total:
            total_amount, error = _sale_int(raw_total, 'Total amount')
            if error:
                return _fail(error)
            if total_amount < 0:
                return _fail("Total amount cannot be negative")
        else:
            total_amount = 0
    invoice_number = str(data.get('invoice_number', sale.invoice_number) or '').strip()
    # Optional invoice replacement (leave empty to keep the current file).
    inv_name, inv_blob, inv_mime = (sale.invoice_attachment or ''), sale.invoice_data, (sale.invoice_mime or '')
    if request.files.get('invoice_attachment'):
        try:
            inv_name, inv_blob, inv_mime = _prepare_slip(
                request.files.get('invoice_attachment'), 'sales_slips',
                ALLOWED_SALES_EXTENSIONS, 'rsale', 'JPG, JPEG, PNG or PDF')
        except SlipSaveError as exc:
            return _fail(str(exc))
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
        if sale.invoice_attachment:
            try:
                delete_upload('sales_slips', os.path.basename(sale.invoice_attachment))
            except StoredFileNotFound:
                pass
            except (FileStorageUnavailable, FileStorageError):
                pass
    sale.date = date_str
    sale.material = material
    sale.quantity = quantity
    sale.amount = amount
    sale.buyer = str(data.get('buyer', sale.buyer) or '').strip()
    sale.customer_contact = customer_contact
    sale.invoice_number = invoice_number
    sale.bill_quantity = bill_qty
    sale.balance_amount = balance
    sale.total_amount = total_amount
    sale.invoice_attachment = inv_name or ''
    sale.invoice_data = inv_blob
    sale.invoice_mime = inv_mime or ''
    # Stock: subtract the old sale from the old material, add the new one.
    stamp = _sale_now()
    old_res = Resource.query.filter(Resource.group == 'Raw Materials',
                                    Resource.name == old_material).first()
    if old_res is not None:
        old_res.sold = max((old_res.sold or 0) - old_qty, 0)
        old_res.last_update = stamp
        old_res.recompute()
    resource.sold = (resource.sold or 0) + quantity
    resource.last_update = stamp
    resource.recompute()
    mv = StockMovement.query.filter_by(reference='RMS%s' % sale.id).first()
    if mv is not None:
        mv.quantity = quantity
        mv.material = material
    db.session.commit()
    return jsonify({"success": True, "sale": sale.to_dict(),
                    "resource": resource.to_dict()})


@app.route('/api/raw-materials-sales/<int:sale_id>', methods=['DELETE'])
def api_raw_materials_sales_delete(sale_id):
    sale = RawMaterialSale.query.get(sale_id)
    if not sale:
        return _fail("Sale not found", 404)
    resource = Resource.query.filter(Resource.group == 'Raw Materials',
                                     Resource.name == sale.material).first()
    if resource is not None:
        resource.sold = max((resource.sold or 0) - (sale.quantity or 0), 0)
        resource.last_update = _sale_now()
        resource.recompute()
    if sale.invoice_attachment:
        try:
            delete_upload('sales_slips', os.path.basename(sale.invoice_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    if sale.weighment_attachment:
        try:
            delete_upload('sales_slips', os.path.basename(sale.weighment_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    StockMovement.query.filter_by(reference='RMS%s' % sale.id).delete()
    db.session.delete(sale)
    db.session.commit()
    return jsonify({"success": True})


@app.route('/api/product-sales', methods=['POST'])
def api_product_sales_create():
    data = request.get_json(silent=True) or request.form
    date_str, error = _sale_date_str(data)
    if error:
        return _fail(error)
    items_raw = _sale_raw(data, 'items')
    if items_raw:
        # Change #56: one sale can carry several products.
        items, error = _parse_ps_items(items_raw)
        if error:
            return _fail(error)
    else:
        product_name = str(data.get('product', '')).strip()
        if not product_name:
            return _fail("Product is required")
        quantity, error = _sale_int(data.get('quantity'), 'Quantity')
        if error:
            return _fail(error)
        if quantity <= 0:
            return _fail("Quantity must be more than 0")
        items = [{"product": product_name, "qty": quantity, "price": None}]
    # Resolve every product and check each stock BEFORE any write.
    product_rows = []
    total_qty = 0
    for it in items:
        name = it['product']
        qty = it['qty']
        prod = Product.query.filter(Product.name == name).first()
        if prod is None:
            return _fail("Product not found")
        stock = int(prod.stock or 0)
        if qty > stock:
            return _fail(f"Only {stock} bags available in stock for {name}")
        product_rows.append((prod, qty))
        total_qty += qty
    quantity = total_qty
    price_by_name = {prod.name: float(prod.price or 0) for prod, _ in product_rows}
    raw_amount = _sale_raw(data, 'amount')
    if raw_amount:
        amount, error = _sale_int(raw_amount, 'Amount')
        if error:
            return _fail(error)
        if amount < 0:
            return _fail("Amount cannot be negative")
    else:
        amount = _ps_amount_fallback([(it['product'], it['qty']) for it in items],
                                     price_by_name)
    location = str(data.get('location', '') or '').strip()[:120]
    customer = str(data.get('customer', '')).strip()
    customer_contact = str(data.get('customer_contact', '')).strip()
    if customer_contact and not re.fullmatch(r'\d{10}', customer_contact):
        return _fail("Customer contact number must be exactly 10 digits")
    # Change #59: Salesman Name box (typed manually, optional).
    salesman_name = str(data.get('salesman_name', '') or '').strip()[:120]
    invoice_number = str(data.get('invoice_number', '')).strip()
    balance = 0
    if _sale_raw(data, 'balance_amount'):
        balance, error = _sale_int(data.get('balance_amount'), 'Balance amount')
        if error:
            return _fail(error)
        if balance < 0:
            return _fail("Balance amount cannot be negative")
    raw_total = _sale_raw(data, 'total_amount')
    total_amount = None
    if raw_total:
        # Change #60: the box was renamed Total Amount (Amount to be paid is
        # now the computed column after the Globle Discount).
        total_amount, error = _sale_int(raw_total, 'Total amount')
        if error:
            return _fail(error)
        if total_amount < 0:
            return _fail("Total amount cannot be negative")
    # Change #60: Globle Discount (amount OR percentage) off the Total Amount.
    eff_total = (total_amount if total_amount is not None else
                 _ps_amount_fallback([(it['product'], it['qty']) for it in items],
                                     price_by_name))
    discount_amount, discount_percent, error = _ps_discount_check(data, eff_total)
    if error:
        return _fail(error)
    inv_name, inv_blob, inv_mime = '', None, ''
    if request.files.get('invoice_attachment'):
        try:
            inv_name, inv_blob, inv_mime = _prepare_slip(
                request.files.get('invoice_attachment'), 'sales_slips',
                ALLOWED_SALES_EXTENSIONS, 'psale', 'JPG, JPEG, PNG or PDF')
        except SlipSaveError as exc:
            return _fail(str(exc))
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    sale = ProductSale(
        date=date_str, product=', '.join(it['product'] for it in items),
        quantity=quantity, amount=amount, customer=customer,
        customer_contact=customer_contact, invoice_number=invoice_number,
        balance_amount=balance, total_amount=total_amount,
        discount_amount=discount_amount, discount_percent=discount_percent,
        invoice_attachment=inv_name or '', invoice_data=inv_blob,
        invoice_mime=inv_mime or '', created=_sale_now(),
        location=location, salesman_name=salesman_name,
        items=_ps_dump_items(items))
    db.session.add(sale)
    stamp = _sale_now()
    for prod, qty in product_rows:
        prod.stock = max(int(prod.stock or 0) - qty, 0)
        prod.status = compute_product_status(prod.stock)
        prod.last_update = stamp
    db.session.flush()
    for it in items:
        db.session.add(StockMovement(date=datetime.now().strftime('%d %b %Y'),
                                     material=it['product'], movement='Sale Out',
                                     quantity=it['qty'], unit='bag',
                                     reference='PS%s' % sale.id))
    db.session.commit()
    return jsonify({"success": True, "sale": sale.to_dict(),
                    "product": product_rows[0][0].to_dict()}), 201


@app.route('/api/product-sales/<int:sale_id>', methods=['PUT'])
def api_product_sales_update(sale_id):
    sale = ProductSale.query.get(sale_id)
    if not sale:
        return _fail("Sale not found", 404)
    # Change #54 (edit button): mirror the POST validations, then move the
    # product stock by exactly the delta (old sale returns to the old product,
    # the new quantity comes out of the new product).
    data = request.get_json(silent=True) or request.form
    date_str = sale.date
    if 'date' in data and str(data.get('date') or '').strip():
        date_str, error = _sale_date_str(data)
        if error:
            return _fail(error)
    old_items = _ps_items(sale.product, sale.quantity, sale.items)
    items_raw = _sale_raw(data, 'items')
    if items_raw:
        # Change #56: the Add Sales modal can carry several products.
        new_items, error = _parse_ps_items(items_raw)
        if error:
            return _fail(error)
    else:
        # Legacy single-product PUT (no `items` field) - unchanged.
        product_name = str(data.get('product', sale.product) or '').strip()
        if not product_name:
            return _fail("Product is required")
        quantity, error = _sale_int(data.get('quantity', sale.quantity), 'Quantity')
        if error:
            return _fail(error)
        if quantity <= 0:
            return _fail("Quantity must be more than 0")
        new_items = [{"product": product_name, "qty": quantity, "price": None}]
    # Stock already has THIS sale's old quantities deducted, so a product that
    # stays on the sale may go up to stock + its own old quantity.
    old_map = {}
    for _name, _qty in old_items:
        old_map[_name] = old_map.get(_name, 0) + _qty
    product_rows = []
    total_qty = 0
    for it in new_items:
        name = it['product']
        qty = it['qty']
        prod = Product.query.filter(Product.name == name).first()
        if prod is None:
            return _fail("Product not found")
        available = int(prod.stock or 0) + old_map.get(name, 0)
        if qty > available:
            return _fail(f"Only {available} bags available in stock for {name}")
        product_rows.append((prod, qty))
        total_qty += qty
    quantity = total_qty
    price_by_name = {prod.name: float(prod.price or 0) for prod, _ in product_rows}
    raw_amount = _sale_raw(data, 'amount')
    if raw_amount:
        amount, error = _sale_int(raw_amount, 'Amount')
        if error:
            return _fail(error)
        if amount < 0:
            return _fail("Amount cannot be negative")
    elif 'amount' in data or 'amount_received' in data:
        # blank/0-only payload: recompute from qty x price like the POST does
        amount = _ps_amount_fallback([(it['product'], it['qty']) for it in new_items],
                                     price_by_name)
    else:
        amount = int(sale.amount or 0)
    customer = str(data.get('customer', sale.customer) or '').strip()
    customer_contact = str(data.get('customer_contact', sale.customer_contact) or '').strip()
    if customer_contact and not re.fullmatch(r'\d{10}', customer_contact):
        return _fail("Customer contact number must be exactly 10 digits")
    invoice_number = str(data.get('invoice_number', sale.invoice_number) or '').strip()
    balance = int(sale.balance_amount or 0)
    if 'balance_amount' in data:
        raw_balance = _sale_raw(data, 'balance_amount')
        if raw_balance:
            balance, error = _sale_int(raw_balance, 'Balance amount')
            if error:
                return _fail(error)
            if balance < 0:
                return _fail("Balance amount cannot be negative")
        else:
            balance = 0
    total_amount = sale.total_amount
    if 'total_amount' in data:
        raw_total = _sale_raw(data, 'total_amount')
        if raw_total:
            # Change #60: renamed box (Amount to be paid is now computed).
            total_amount, error = _sale_int(raw_total, 'Total amount')
            if error:
                return _fail(error)
            if total_amount < 0:
                return _fail("Total amount cannot be negative")
        else:
            total_amount = None
    # Change #60: the edit modal always sends BOTH discount keys (a cleared
    # box sends '' and clears it); a JSON partial PUT without either key keeps
    # the stored values, and one with only ONE key clears the other (the
    # stored value of a partially-sent discount would be ambiguous anyway).
    discount_amount = sale.discount_amount
    discount_percent = sale.discount_percent
    if 'discount_amount' in data or 'discount_percent' in data:
        _merged = {}
        if 'discount_amount' in data:
            _merged['discount_amount'] = _sale_raw(data, 'discount_amount')
        if 'discount_percent' in data:
            _merged['discount_percent'] = _sale_raw(data, 'discount_percent')
        eff_total = (total_amount if total_amount is not None else
                     _ps_amount_fallback([(it['product'], it['qty']) for it in new_items],
                                         price_by_name))
        discount_amount, discount_percent, error = _ps_discount_check(_merged, eff_total)
        if error:
            return _fail(error)
    # Optional invoice replacement (leave empty to keep the current file).
    inv_name, inv_blob, inv_mime = (sale.invoice_attachment or ''), sale.invoice_data, (sale.invoice_mime or '')
    if request.files.get('invoice_attachment'):
        try:
            inv_name, inv_blob, inv_mime = _prepare_slip(
                request.files.get('invoice_attachment'), 'sales_slips',
                ALLOWED_SALES_EXTENSIONS, 'psale', 'JPG, JPEG, PNG or PDF')
        except SlipSaveError as exc:
            return _fail(str(exc))
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
        if sale.invoice_attachment:
            try:
                delete_upload('sales_slips', os.path.basename(sale.invoice_attachment))
            except StoredFileNotFound:
                pass
            except (FileStorageUnavailable, FileStorageError):
                pass
    sale.date = date_str
    sale.product = ', '.join(it['product'] for it in new_items)
    sale.quantity = quantity
    sale.amount = amount
    sale.customer = customer
    sale.customer_contact = customer_contact
    # Change #59: only overwrite when the key is present, so a JSON partial
    # PUT (no salesman field) keeps the stored name; the edit modal always
    # sends it (a cleared box clears the name).
    if 'salesman_name' in data:
        sale.salesman_name = str(data.get('salesman_name') or '').strip()[:120]
    sale.invoice_number = invoice_number
    sale.balance_amount = balance
    sale.total_amount = total_amount
    sale.discount_amount = discount_amount
    sale.discount_percent = discount_percent
    sale.invoice_attachment = inv_name or ''
    sale.invoice_data = inv_blob
    sale.invoice_mime = inv_mime or ''
    sale.items = _ps_dump_items(new_items)
    if 'location' in data:
        sale.location = str(data.get('location') or '').strip()[:120]
    # Stock: give every old item back, then deduct the new quantities.
    stamp = _sale_now()
    for name, qty in old_items:
        if qty <= 0:
            continue
        row = Product.query.filter(Product.name == name).first()
        if row is not None:
            row.stock = int(row.stock or 0) + qty
            row.status = compute_product_status(row.stock)
            row.last_update = stamp
    for prod, qty in product_rows:
        prod.stock = max(int(prod.stock or 0) - qty, 0)
        prod.status = compute_product_status(prod.stock)
        prod.last_update = stamp
    StockMovement.query.filter_by(reference='PS%s' % sale.id).delete()
    for it in new_items:
        db.session.add(StockMovement(date=datetime.now().strftime('%d %b %Y'),
                                     material=it['product'], movement='Sale Out',
                                     quantity=it['qty'], unit='bag',
                                     reference='PS%s' % sale.id))
    db.session.commit()
    return jsonify({"success": True, "sale": sale.to_dict(),
                    "product": product_rows[0][0].to_dict()})


@app.route('/api/product-sales/<int:sale_id>', methods=['DELETE'])
def api_product_sales_delete(sale_id):
    sale = ProductSale.query.get(sale_id)
    if not sale:
        return _fail("Sale not found", 404)
    # Change #56: give every product on the sale its bags back.
    stamp = _sale_now()
    for name, qty in _ps_items(sale.product, sale.quantity, sale.items):
        if qty <= 0:
            continue
        product = Product.query.filter(Product.name == name).first()
        if product is not None:
            product.stock = int(product.stock or 0) + qty
            product.status = compute_product_status(product.stock)
            product.last_update = stamp
    if sale.invoice_attachment:
        try:
            delete_upload('sales_slips', os.path.basename(sale.invoice_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    StockMovement.query.filter_by(reference='PS%s' % sale.id).delete()
    db.session.delete(sale)
    db.session.commit()
    return jsonify({"success": True})


# ---------- Change #55 (2026-10-09): Remarks page ----------
# A remark records a partial payment (Amount Received) against a raw-material
# or product sale. Saving subtracts the payment from the sale's current
# balance_amount (so the sales page's Balance column updates) and stores the
# entered Date + Mode of Payment in the sales_remarks table.

@app.route('/remarks')
def remarks():
    price_by_name = {p.name: float(p.price or 0)
                     for p in Product.query.order_by(Product.name).all()}
    rm_rows = []
    for s in RawMaterialSale.query.order_by(RawMaterialSale.id.desc()).all():
        rm_rows.append({
            "id": s.id,
            "invoice": s.invoice_number or "",
            "customer": s.buyer or "-",
            "product": s.material or "-",
            "total": int(s.total_amount or 0),
            "received": int(s.amount or 0),
            "balance": int(s.balance_amount or 0),
        })
    ps_rows = []
    for s in ProductSale.query.order_by(ProductSale.id.desc()).all():
        # amount_to_pay = typed total_amount, else qty x price (Change #52).
        _ta = s.total_amount
        total = (int(_ta) if _ta is not None else _ps_amount_fallback(
            _ps_items(s.product, s.quantity, s.items), price_by_name))
        ps_rows.append({
            "id": s.id,
            "invoice": s.invoice_number or "",
            "customer": s.customer or "-",
            "product": s.product or "-",
            "total": total,
            "received": int(s.amount or 0),
            "balance": int(s.balance_amount or 0),
        })
    all_remarks = SalesRemark.query.order_by(SalesRemark.id.desc()).all()
    return render_template('remarks.html',
                           rm_sales=rm_rows,
                           ps_sales=ps_rows,
                           rm_remarks=[r.to_dict() for r in all_remarks
                                       if (r.sale_type or '') == 'raw_material'],
                           ps_remarks=[r.to_dict() for r in all_remarks
                                       if (r.sale_type or '') == 'product'],
                           active_page='remarks')


def _remark_total(sale_type, sale):
    """Fetched Total/Amount-to-be-paid for a sale, snapshot onto the remark."""
    if sale_type == 'raw_material':
        return int(sale.total_amount or 0)
    if sale.total_amount is not None:
        return int(sale.total_amount)
    # Change #56: qty x price summed over every product on the sale.
    total = 0
    for name, qty in _ps_items(sale.product, sale.quantity, sale.items):
        prod = Product.query.filter(Product.name == name).first()
        total += qty * float(getattr(prod, 'price', 0) or 0)
    return int(round(total))


@app.route('/api/remarks', methods=['POST'])
def api_remarks_create():
    data = request.get_json(silent=True) or request.form
    sale_type = str(data.get('sale_type', '')).strip()
    if sale_type not in ('raw_material', 'product'):
        return _fail("sale_type must be raw_material or product")
    try:
        sale_id = int(str(data.get('sale_id', '')).strip())
    except (TypeError, ValueError):
        return _fail("Invoice is required")
    Model = RawMaterialSale if sale_type == 'raw_material' else ProductSale
    sale = Model.query.get(sale_id)
    if not sale:
        return _fail("Sale not found", 404)
    amount, error = _sale_int(data.get('amount_received'), 'Amount Received')
    if error:
        return _fail(error)
    if amount <= 0:
        return _fail("Amount Received must be more than 0")
    date_str, error = _sale_date_str(data)
    if error:
        return _fail(error)
    if _parse_ddmm(date_str) is not None and _parse_ddmm(date_str) > datetime.now().date():
        return _fail("Date cannot be in the future")
    payment_mode = str(data.get('payment_mode', '')).strip()
    if not payment_mode:
        return _fail("Mode of Payment is required")
    # Change #57: optional free-text block (textarea under the Date box).
    notes = str(data.get('notes', '') or '').strip()
    if len(notes) > 5000:
        return _fail("Notes must be 5000 characters or fewer")
    cur_balance = int(sale.balance_amount or 0)
    if amount > cur_balance:
        return _fail("Amount Received cannot be more than the balance amount (%d)" % cur_balance)
    new_balance = cur_balance - amount
    # The sales page's Balance Amount column reads balance_amount - update it.
    sale.balance_amount = new_balance
    if sale_type == 'raw_material':
        customer = sale.buyer or ''
        product = sale.material or ''
    else:
        customer = sale.customer or ''
        product = sale.product or ''
    remark = SalesRemark(
        sale_type=sale_type, sale_id=sale.id,
        invoice_number=sale.invoice_number or '',
        customer_name=customer, product=product,
        total_amount=_remark_total(sale_type, sale),
        amount_received=amount, balance=new_balance,
        date=date_str, payment_mode=payment_mode, notes=notes,
        created=_sale_now())
    db.session.add(remark)
    db.session.commit()
    return jsonify({"success": True, "remark": remark.to_dict(),
                    "sale": sale.to_dict()}), 201


@app.route('/api/remarks/<int:remark_id>', methods=['PUT'])
def api_remarks_update(remark_id):
    """Change #58: edit a remark from the user Remarks page. Re-applies the
    payment against the sale: the sale balance already had the OLD amount
    deducted, so available = balance + old amount, then new balance =
    available - new amount (other remarks on the sale stay untouched)."""
    remark = SalesRemark.query.get(remark_id)
    if not remark:
        return _fail("Remark not found", 404)
    data = request.get_json(silent=True) or request.form
    Model = RawMaterialSale if (remark.sale_type or '') == 'raw_material' else ProductSale
    sale = Model.query.get(remark.sale_id or 0)
    if sale is None:
        return _fail("Sale not found", 404)
    amount, error = _sale_int(data.get('amount_received'), 'Amount Received')
    if error:
        return _fail(error)
    if amount <= 0:
        return _fail("Amount Received must be more than 0")
    date_str, error = _sale_date_str(data)
    if error:
        return _fail(error)
    if _parse_ddmm(date_str) is not None and _parse_ddmm(date_str) > datetime.now().date():
        return _fail("Date cannot be in the future")
    payment_mode = str(data.get('payment_mode', '')).strip()
    if not payment_mode:
        return _fail("Mode of Payment is required")
    notes = str(data.get('notes', '') or '').strip()
    if len(notes) > 5000:
        return _fail("Notes must be 5000 characters or fewer")
    available = int(sale.balance_amount or 0) + int(remark.amount_received or 0)
    if amount > available:
        return _fail("Amount Received cannot be more than the balance amount (%d)" % available)
    new_balance = available - amount
    sale.balance_amount = new_balance
    remark.amount_received = amount
    remark.balance = new_balance
    remark.date = date_str
    remark.payment_mode = payment_mode
    remark.notes = notes
    db.session.commit()
    return jsonify({"success": True, "remark": remark.to_dict(),
                    "sale": sale.to_dict()})


@app.route('/api/remarks/<int:remark_id>', methods=['DELETE'])
def api_remarks_delete(remark_id):
    remark = SalesRemark.query.get(remark_id)
    if not remark:
        return _fail("Remark not found", 404)
    # Undo this payment: the amount goes back onto the sale's balance.
    Model = RawMaterialSale if (remark.sale_type or '') == 'raw_material' else ProductSale
    sale = Model.query.get(remark.sale_id or 0)
    if sale is not None:
        sale.balance_amount = int(sale.balance_amount or 0) + int(remark.amount_received or 0)
    db.session.delete(remark)
    db.session.commit()
    return jsonify({"success": True})


def _extract_qty(value):
    import re as _re
    match = _re.search(r"[-+]?\d+(?:\.\d+)?", str(value or '').replace(',', ''))
    if not match:
        return ''
    try:
        num = float(match.group(0))
    except (TypeError, ValueError):
        return ''
    if num != num or num in (float('inf'), float('-inf')):
        # F4: guard against 1e999-style overflow that the regex can still parse.
        return ''
    if num == int(num):
        return str(int(num))
    return repr(num).rstrip('0').rstrip('.')


@app.route('/driver-details')
def driver_details():
    drivers = DriverDetails.query.order_by(DriverDetails.id.desc()).all()
    return render_template('driver_details.html',
        drivers=[d.to_dict() for d in drivers],
        active_page='driver_details'
    )


@app.route('/api/driver-details', methods=['POST'])
def api_driver_details_create():
    data = request.get_json(silent=True) or request.form
    driver_name = str(data.get('driver_name', '')).strip()
    contact_number = str(data.get('contact_number', '')).strip()
    if not driver_name:
        return _fail("Driver Name is required")
    if contact_number and not _valid_phone(contact_number):
        return _fail("Contact number must be exactly 10 digits (0-9)")
    vehicle = _valid_vehicle_number(data.get('vehicle_number', ''))
    if vehicle is None:
        return _fail("Vehicle number must use uppercase letters and numbers only (e.g. KA-99-PU-8907)")
    if not vehicle:
        return _fail("Vehicle Number is required")
    arrival_date = str(data.get('arrival_date', '')).strip() or datetime.now().strftime('%d-%m-%Y')
    drv_arr = _parse_ddmm(arrival_date)
    if drv_arr is not None and drv_arr > datetime.now().date():
        # Item 10 (2026-10-01): driver arrivals cannot be dated in the future.
        return _fail("Date of Arrival cannot be in the future")
    if not request.files.get('slip_attachment'):
        return _fail("Invoice attachment is required (JPG, JPEG, PNG or PDF)")
    if not request.files.get('weighment_attachment'):
        return _fail("Weigment attachment is required (JPG, JPEG, PNG or PDF)")
    try:
        slip_name, slip_blob, slip_mime = _prepare_slip(
            request.files.get('slip_attachment'), 'driver_slips',
            ALLOWED_DRIVER_SLIP_EXTENSIONS, 'dslip', 'JPG, JPEG, PNG or PDF')
    except SlipSaveError as exc:
        return _fail(str(exc))
    except (FileStorageUnavailable, FileStorageError) as exc:
        return _fail(str(exc), 503)
    try:
        weigh_name, weigh_blob, weigh_mime = _prepare_slip(
            request.files.get('weighment_attachment'), 'driver_slips',
            ALLOWED_DRIVER_SLIP_EXTENSIONS, 'dweigh', 'JPG, JPEG, PNG or PDF')
    except SlipSaveError as exc:
        return _fail(str(exc))
    except (FileStorageUnavailable, FileStorageError) as exc:
        return _fail(str(exc), 503)
    record = DriverDetails(
        driver_name=driver_name,
        contact_number=contact_number,
        vehicle_number=vehicle,
        arrival_date=arrival_date,
        qty_in_bill=_extract_qty(data.get('qty_in_bill', '')),
        actual_qty=_extract_qty(data.get('actual_qty', '')),
        slip_attachment=slip_name or '',
        slip_data=slip_blob,
        slip_mime=slip_mime or '',
        weighment_attachment=weigh_name or '',
        weighment_data=weigh_blob,
        weighment_mime=weigh_mime or '',
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({"success": True, "driver": record.to_dict()}), 201


@app.route('/api/driver-details/<int:driver_id>', methods=['DELETE'])
def api_driver_details_delete(driver_id):
    record = DriverDetails.query.get(driver_id)
    if not record:
        return _fail("Driver details not found", 404)
    if record.slip_attachment:
        try:
            delete_upload('driver_slips', os.path.basename(record.slip_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    if getattr(record, 'weighment_attachment', ''):
        try:
            delete_upload('driver_slips', os.path.basename(record.weighment_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    db.session.delete(record)
    db.session.commit()
    return jsonify({"success": True})


@app.route('/api/purchases', methods=['GET'])
def api_purchases_list():
    status = request.args.get('status', 'all')
    query = Purchase.query.order_by(Purchase.id.desc())
    if status in ('Paid', 'Partial', 'Unpaid'):
        query = query.filter_by(status=status)
    return _ok({"purchases": [p.to_dict() for p in query.all()]})


def _purchase_stock_qty(text):
    """Number a purchase contributes to stock: the ACTUAL quantity
    (contain_quantity) with the legacy free-text quantity as fallback."""
    qty_text = str(text or '').replace(',', '')
    qty_match = re.search(r"[-+]?\d+(?:\.\d+)?", qty_text)
    if not qty_match:
        return 0
    try:
        num = float(qty_match.group(0))
    except (TypeError, ValueError):
        return 0
    if num != num or num in (float('inf'), float('-inf')):
        # F4: guard against 1e999-style overflow that the regex can still parse.
        return 0
    return int(num)


def _purchase_resource(product_name):
    """Match a purchase product to its Resource Maintenance row. Exact/
    case-insensitive first, then a typo-tolerant fuzzy fallback so entries
    such as 'maiz' still update the existing 'Maize' row (Change #54: shared
    by POST and the edit PUT so both apply identical stock matching)."""
    product_name = (product_name or '').strip()
    if not product_name:
        return None
    resource = Resource.query.filter(Resource.name.ilike(product_name)).first()
    if resource:
        return resource
    candidates = Resource.query.all()
    normalized = re.sub(r"\s+", " ", product_name).strip().casefold()
    for candidate in candidates:
        candidate_norm = re.sub(r"\s+", " ", str(candidate.name or "")).strip().casefold()
        if candidate_norm == normalized:
            return candidate
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
        return best
    return None


@app.route('/api/purchases', methods=['POST'])
def api_purchases_create():
    data = request.get_json(silent=True) or request.form
    vendor = str(data.get('vendor_name', '')).strip()
    vendor_phone = str(data.get('vendor_phone', '')).strip()
    v_number = str(data.get('v_number', '')).strip() or _next_v_number()
    if not vendor or not vendor_phone:
        return _fail("vendor_name and vendor_phone are required")
    vendor_phone = _valid_phone(vendor_phone)
    if not vendor_phone:
        return _fail("Vendor phone number must be exactly 10 digits (0-9)")
    if Purchase.query.filter_by(v_number=v_number).first():
        return _fail("Record number already exists", 409)
    vehicle = _valid_vehicle_number(data.get('vehicle_number', ''))
    if vehicle is None:
        return _fail("Vehicle number must use uppercase letters and numbers only (e.g. KA-99-PU-8907)")
    driver_name = str(data.get('driver_name', '')).strip()
    driver_phone = str(data.get('driver_phone', '')).strip()
    if not driver_name:
        return _fail("Driver Name is required")
    if driver_phone and not _valid_phone(driver_phone):
        return _fail("Driver Contact Number must be exactly 10 digits (0-9)")
    invoice_number = str(data.get('invoice_number', '')).strip()
    if not invoice_number:
        return _fail("Invoice Number is required")
    if not vehicle:
        return _fail("Vehicle Number is required")
    if not str(data.get('bill_quantity', '')).strip():
        return _fail("Quantity in Bill is required")
    if not str(data.get('contain_quantity', '')).strip():
        return _fail("Actual Quantity is required")
    # F5: Actual/contain quantity must parse as a non-negative number. The
    # client requires a number, but a JSON/scripted POST with e.g. "abc" used
    # to be accepted and silently contribute 0 to stock while the row saved
    # the garbage text. Reject it here so the stock feed can't be poisoned.
    _contain_num = _purchase_stock_qty(data.get('contain_quantity'))
    _contain_text = str(data.get('contain_quantity') or '').replace(',', '').strip()
    if not re.search(r"\d", _contain_text) or _contain_num < 0:
        return _fail("Actual Quantity must be a non-negative number (kg)")
    arrival_date = str(data.get('arrival_date', '')).strip()
    if not arrival_date:
        return _fail("Date of Arrival is required")
    arr_d = _parse_ddmm(arrival_date)
    if arr_d is not None and arr_d > datetime.now().date():
        # Item 7 (2026-10-01): arrivals cannot be dated in the future.
        return _fail("Date of Arrival cannot be in the future")
    if not request.files.get('slip_attachment'):
        return _fail("Invoice attachment is required (JPG, JPEG or PNG)")
    if not request.files.get('weighment_attachment'):
        return _fail("Weigment attachment is required (JPG, JPEG or PNG)")
    try:
        slip_name, slip_blob, slip_mime = _prepare_slip(
            request.files.get('slip_attachment'), 'purchases',
            ALLOWED_SLIP_EXTENSIONS, 'slip', 'JPG, JPEG or PNG')
    except SlipSaveError as exc:
        return _fail(str(exc))
    except (FileStorageUnavailable, FileStorageError) as exc:
        return _fail(str(exc), 503)
    try:
        weigh_name, weigh_blob, weigh_mime = _prepare_slip(
            request.files.get('weighment_attachment'), 'purchases',
            ALLOWED_SLIP_EXTENSIONS, 'pweigh', 'JPG, JPEG or PNG')
    except SlipSaveError as exc:
        return _fail(str(exc))
    except (FileStorageUnavailable, FileStorageError) as exc:
        return _fail(str(exc), 503)
    # Item 7 (2026-10-01): Amount Paid + Remaining are mandatory (only empty /
    # missing values are rejected; "0" is a valid amount).
    amount_raw = str(data.get('amount_paid', '')).strip()
    remaining_raw = str(data.get('remaining', '')).strip()
    if not amount_raw or not remaining_raw:
        return _fail("Amount Paid and Remaining are both required")
    amount = _parse_int(amount_raw)
    remaining = _parse_int(remaining_raw)
    if amount < 0 or remaining < 0:
        return _fail("Amounts cannot be negative")
    status = str(data.get('status', '')).strip()
    if not status:
        status = 'Paid' if remaining == 0 else ('Unpaid' if amount == 0 else 'Partial')
    try:
        credit_days = int(data.get('credit_days', 0) or 0)
    except (TypeError, ValueError):
        credit_days = 0
    if credit_days < 0:
        credit_days = 0
    credit_due_date = _compute_credit_due(arrival_date or data.get('date_display', ''), credit_days)
    last_return = str(data.get('last_return_date', '')).strip()
    last_ret_date = _parse_ddmm(last_return)
    if last_ret_date is not None:
        credit_due_date = last_ret_date.strftime('%d-%m-%Y')
        arr_d = _parse_ddmm(arrival_date or data.get('date_display', ''))
        if arr_d is not None and last_ret_date >= arr_d:
            credit_days = (last_ret_date - arr_d).days
    purchase = Purchase(
        vendor_name=vendor, vendor_phone=vendor_phone, address=str(data.get('address', '')),
        v_number=v_number, invoice_number=invoice_number,
        date_display=str(data.get('date_display', datetime.now().strftime('%d %b %Y'))),
        product=str(data.get('product', '')), quantity=str(data.get('quantity', '')),
        amount_paid=amount, remaining=remaining,
        payment_mode=str(data.get('payment_mode', 'Cash')), status=status,
        vehicle_number=vehicle,
        bill_quantity=str(data.get('bill_quantity', '')).strip(),
        bill_unit=str(data.get('bill_unit', 'Kgs')).strip() or 'Kgs',
        contain_quantity=str(data.get('contain_quantity', '')).strip(),
        contain_unit=str(data.get('contain_unit', 'Kgs')).strip() or 'Kgs',
        slip_attachment=slip_name or '',
        slip_data=slip_blob,
        slip_mime=slip_mime or '',
        weighment_attachment=weigh_name or '',
        weighment_data=weigh_blob,
        weighment_mime=weigh_mime or '',
        arrival_date=arrival_date,
        credit_days=credit_days, credit_due_date=credit_due_date,
        driver_name=driver_name,
        driver_phone=driver_phone,
    )
    db.session.add(purchase)

    # Auto-store driver details from the purchase submission, so the Driver
    # Details page receives the same driver info used in Purchase Maintenance.
    if driver_name or driver_phone:
        db.session.add(DriverDetails(
            driver_name=driver_name,
            contact_number=driver_phone,
            vehicle_number=vehicle or '',
            arrival_date=arrival_date or datetime.now().strftime('%d-%m-%Y'),
            qty_in_bill=_extract_qty(data.get('bill_quantity', '')),
            actual_qty=_extract_qty(data.get('contain_quantity', '')),
            slip_attachment=slip_name or '',
            # Copy the blob too, so the driver row's own View link works
            # (the link points at /driver-slips/<name>, not /purchase-slips/).
            slip_data=slip_blob,
            slip_mime=slip_mime or '',
            weighment_attachment=weigh_name or '',
            weighment_data=weigh_blob,
            weighment_mime=weigh_mime or '',
        ))

    # A purchase immediately increases the matching Resource Maintenance stock.
    # Item 7 (2026-10-01): the ACTUAL quantity (contain_quantity) is what really
    # arrived and feeds the stock; the old free-text quantity stays as a
    # fallback for legacy rows that never stored an actual quantity.
    purchase_qty = _purchase_stock_qty(purchase.contain_quantity or purchase.quantity)
    resource = _purchase_resource(purchase.product)
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
    purchase = Purchase.query.get(purchase_id)
    if not purchase:
        return _fail("Purchase not found", 404)
    # Change #54 (edit button): accept FormData from the edit modal as well as
    # JSON. Capture the stock picture BEFORE any field changes so the Resource
    # counters can be moved by exactly the delta this edit introduces.
    data = request.get_json(silent=True) or request.form
    old_qty = _purchase_stock_qty(purchase.contain_quantity or purchase.quantity)
    old_product = (purchase.product or '').strip()
    # --- validation (mirrors POST; only fields present in the payload are
    # checked, so a partial JSON PUT keeps working) ---
    if 'vendor_name' in data or 'vendor_phone' in data:
        if (not str(data.get('vendor_name', purchase.vendor_name) or '').strip()
                or not str(data.get('vendor_phone', purchase.vendor_phone) or '').strip()):
            return _fail("vendor_name and vendor_phone are required")
        new_vp = _valid_phone(data.get('vendor_phone', purchase.vendor_phone))
        if not new_vp:
            return _fail("Vendor phone number must be exactly 10 digits (0-9)")
        purchase.vendor_phone = new_vp
    if 'vehicle_number' in data and data['vehicle_number'] is not None:
        vehicle = _valid_vehicle_number(data['vehicle_number'])
        if vehicle is None:
            return _fail("Vehicle number must use uppercase letters and numbers only (e.g. KA-99-PU-8907)")
        if not vehicle:
            return _fail("Vehicle Number is required")
        purchase.vehicle_number = vehicle
    if 'driver_name' in data and not str(data['driver_name'] or '').strip():
        return _fail("Driver Name is required")
    if 'driver_phone' in data and str(data['driver_phone'] or '').strip():
        if not _valid_phone(str(data['driver_phone']).strip()):
            return _fail("Driver Contact Number must be exactly 10 digits (0-9)")
    if 'invoice_number' in data and not str(data['invoice_number'] or '').strip():
        return _fail("Invoice Number is required")
    if 'bill_quantity' in data and not str(data['bill_quantity'] or '').strip():
        return _fail("Quantity in Bill is required")
    if 'contain_quantity' in data and not str(data['contain_quantity'] or '').strip():
        return _fail("Actual Quantity is required")
    if 'contain_quantity' in data and str(data['contain_quantity'] or '').strip():
        # F5 (edit path): same non-negative-number rule as POST, so a bad
        # value can't be smuggled in via the edit modal / JSON PUT and
        # silently feed 0 to the stock delta.
        _c_text = str(data['contain_quantity'] or '').replace(',', '').strip()
        if not re.search(r"\d", _c_text) or _purchase_stock_qty(data['contain_quantity']) < 0:
            return _fail("Actual Quantity must be a non-negative number (kg)")
    if 'arrival_date' in data:
        arrival = str(data['arrival_date'] or '').strip()
        if not arrival:
            return _fail("Date of Arrival is required")
        arr_d = _parse_ddmm(arrival)
        if arr_d is not None and arr_d > datetime.now().date():
            return _fail("Date of Arrival cannot be in the future")
    for field in ('vendor_name', 'vendor_phone', 'address', 'product', 'quantity',
                  'payment_mode', 'status', 'date_display',
                  'bill_quantity', 'bill_unit', 'contain_quantity', 'contain_unit', 'arrival_date',
                  'driver_name', 'driver_phone', 'invoice_number'):
        if field in data and data[field] is not None:
            setattr(purchase, field, str(data[field]))
    for field in ('amount_paid', 'remaining'):
        if field in data:
            text = str(data[field]).replace(',', '').strip()
            if text == '':
                return _fail("Amount Paid and Remaining are both required")
            try:
                _fval = float(text)
            except (TypeError, ValueError):
                return _fail(f"{field} must be a number")
            if _fval != _fval or _fval in (float('inf'), float('-inf')):
                # F4: "nan"/"1e999" would otherwise crash int(float(...)).
                return _fail(f"{field} must be a finite number")
            value = int(_fval)
            if value < 0:
                return _fail(f"{field} cannot be negative")
            setattr(purchase, field, value)
    # Status auto-calculates from the amounts (same rule as POST) unless an
    # explicit status was sent.
    if ('amount_paid' in data or 'remaining' in data) and not str(data.get('status') or '').strip():
        purchase.status = ('Paid' if purchase.remaining == 0
                           else ('Unpaid' if purchase.amount_paid == 0 else 'Partial'))
    if 'credit_days' in data:
        try:
            purchase.credit_days = max(int(data['credit_days']), 0)
        except (TypeError, ValueError):
            return _fail("credit_days must be a number")
        purchase.credit_due_date = _compute_credit_due(
            purchase.arrival_date or purchase.date_display, purchase.credit_days)
    if 'last_return_date' in data:
        last_ret_date = _parse_ddmm(str(data.get('last_return_date') or '').strip())
        if last_ret_date is not None:
            purchase.credit_due_date = last_ret_date.strftime('%d-%m-%Y')
            arr_d = _parse_ddmm(purchase.arrival_date or purchase.date_display or '')
            if arr_d is not None and last_ret_date >= arr_d:
                purchase.credit_days = (last_ret_date - arr_d).days
    # --- optional attachment replacement (leave the file inputs empty to keep
    # the current files) ---
    for field, folder, prefix, label in (
            ('slip_attachment', 'purchases', 'slip', 'Invoice'),
            ('weighment_attachment', 'purchases', 'pweigh', 'Weigment')):
        fs = request.files.get(field)
        if not fs:
            continue
        try:
            new_name, new_blob, new_mime = _prepare_slip(
                fs, folder, ALLOWED_SLIP_EXTENSIONS, prefix, 'JPG, JPEG or PNG')
        except SlipSaveError as exc:
            return _fail(str(exc))
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
        old_file = getattr(purchase, field) or ''
        if old_file:
            try:
                delete_upload(folder, os.path.basename(old_file))
            except StoredFileNotFound:
                pass
            except (FileStorageUnavailable, FileStorageError):
                pass
        setattr(purchase, field, new_name or '')
        setattr(purchase, field.replace('_attachment', '_data'), new_blob)
        setattr(purchase, field.replace('_attachment', '_mime'), new_mime or '')
    # --- stock delta: purchases feed Resource.purchased, so an edit moves the
    # counter by exactly (new - old). Same-row or product-change both work:
    # subtract the old contribution, add the new one. ---
    new_qty = _purchase_stock_qty(purchase.contain_quantity or purchase.quantity)
    new_product = (purchase.product or '').strip()
    stock_changed = (new_qty != old_qty) or (new_product != old_product)
    if stock_changed:
        stamp = datetime.now().strftime("%d %b %Y, %I:%M %p")
        old_res = _purchase_resource(old_product) if old_product else None
        new_res = _purchase_resource(new_product) if new_product else None
        if old_res is not None and old_qty:
            old_res.purchased = max((old_res.purchased or 0) - old_qty, 0)
            old_res.last_update = stamp
            old_res.recompute()
        if new_res is not None and new_qty:
            new_res.purchased = (new_res.purchased or 0) + new_qty
            new_res.last_update = stamp
            new_res.recompute()
        mv = StockMovement.query.filter_by(reference=purchase.v_number).first()
        if mv is not None:
            mv.quantity = new_qty
            mv.material = new_product or mv.material
            if not new_qty:
                db.session.delete(mv)
    db.session.commit()
    return _ok({"purchase": purchase.to_dict()})


@app.route('/api/purchases/<int:purchase_id>', methods=['DELETE'])
def api_purchases_delete(purchase_id):
    purchase = Purchase.query.get(purchase_id)
    if not purchase:
        return _fail("Purchase not found", 404)
    # Change #54: the row's stock contribution is taken back on delete (the
    # confirm dialog promises this and the create PUT feed made it true) -
    # same delta helpers the edit PUT uses, plus the movement row.
    old_qty = _purchase_stock_qty(purchase.contain_quantity or purchase.quantity)
    old_product = (purchase.product or '').strip()
    old_res = _purchase_resource(old_product) if (old_product and old_qty) else None
    if old_res is not None:
        old_res.purchased = max((old_res.purchased or 0) - old_qty, 0)
        old_res.last_update = datetime.now().strftime("%d %b %Y, %I:%M %p")
        old_res.recompute()
    if purchase.v_number:
        StockMovement.query.filter_by(reference=purchase.v_number).delete()
    if purchase.slip_attachment:
        try:
            delete_upload('purchases', os.path.basename(purchase.slip_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
    if getattr(purchase, 'weighment_attachment', ''):
        try:
            delete_upload('purchases', os.path.basename(purchase.weighment_attachment))
        except StoredFileNotFound:
            pass
        except (FileStorageUnavailable, FileStorageError) as exc:
            return _fail(str(exc), 503)
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


def _vendor_product_options():
    """Product candidate lists per register group (Raw Materials / Medicines / Chemicals)."""
    groups = ["Raw Materials", "Medicines", "Chemicals"]
    options = {g: [] for g in groups}
    for r in Resource.query.order_by(Resource.group, Resource.name).all():
        if r.group in options and r.name not in options[r.group]:
            options[r.group].append(r.name)
    return options


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
        product_options=_vendor_product_options(),
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


def _parse_vendor_products(data):
    """Read vendor products (dict {group: [...]} / list / str). Returns (json_text, first_product)."""
    products = data.get('products')
    grouped = None
    flat = []
    if isinstance(products, dict):
        grouped = {str(g): [str(p).strip() for p in items if str(p).strip()]
                   for g, items in products.items()}
        grouped = {k: v for k, v in grouped.items() if v}
        for k in ("Raw Materials", "Chemicals", "Medicines"):
            flat.extend(grouped.get(k, []))
    elif isinstance(products, (list, tuple)):
        flat = [str(p).strip() for p in products if str(p).strip()]
    elif isinstance(products, str):
        flat = [p.strip() for p in products.split(',') if p.strip()]
    if grouped:
        products_json = json.dumps(grouped)
    elif flat:
        products_json = json.dumps(flat)
    else:
        products_json = ""
    first = flat[0] if flat else str(data.get('product', '')).strip()
    return products_json, first


@app.route('/api/vendors', methods=['POST'])
def api_vendors_create():
    data = request.get_json(silent=True) or {}
    vendor = str(data.get('vendor_name', '')).strip()
    if not vendor:
        return _fail("vendor_name is required")
    # Change #51 (2026-10-07): vendor contact number is NOT mandatory - blank
    # is stored as '', and a typed value must still be exactly 10 digits.
    phone_raw = str(data.get('vendor_phone', '') or '').strip()
    phone = ''
    if phone_raw:
        phone = _valid_phone(phone_raw)
        if not phone:
            return _fail("Phone number must be exactly 10 digits (0–9)")
    # Item 6 (2026-10-01): GSTIN is optional - only format-checked when given.
    gstin_raw = str(data.get('gstin', '') or '').strip()
    gstin = ''
    if gstin_raw:
        gstin = _valid_gstin(gstin_raw)
        if not gstin:
            return _fail("GSTIN must be exactly 15 alphanumeric characters")
    amount = _parse_int(data.get('amount_paid', 0))
    remaining = _parse_int(data.get('remaining', 0))
    if amount < 0 or remaining < 0:
        return _fail("Amounts cannot be negative")
    status = str(data.get('status', '')).strip()
    if not status:
        status = 'Paid' if remaining == 0 else ('Unpaid' if amount == 0 else 'Partial')
    category = str(data.get('category', '')).strip()
    if not category:
        return _fail("category is required")
    if category not in VENDOR_CATEGORIES:
        return _fail("category must be one of: " + ", ".join(VENDOR_CATEGORIES))
    products_json, first_product = _parse_vendor_products(data)
    record = Vendor(
        vendor_name=vendor, vendor_phone=phone, gstin=gstin, address=str(data.get('address', '')),
        date_display=str(data.get('date_display', datetime.now().strftime('%d %b %Y'))),
        category=category,
        product=first_product, products=products_json,
        quantity=str(data.get('quantity', '')),
        amount_paid=amount, remaining=remaining,
        payment_mode=str(data.get('payment_mode', 'Cash')), status=status,
    )
    db.session.add(record)
    db.session.commit()
    return jsonify({"success": True, "vendor": record.to_dict()}), 201


@app.route('/api/vendors/<int:vendor_id>', methods=['PUT'])
def api_vendors_update(vendor_id):
    record = Vendor.query.get(vendor_id)
    if not record:
        return _fail("Vendor not found", 404)
    data = request.get_json(silent=True) or {}
    if 'vendor_phone' in data and data['vendor_phone'] is not None:
        # Change #51: blank clears the number; a typed value must be 10 digits.
        phone_raw = str(data['vendor_phone']).strip()
        if phone_raw:
            phone = _valid_phone(phone_raw)
            if not phone:
                return _fail("Phone number must be exactly 10 digits")
            record.vendor_phone = phone
        else:
            record.vendor_phone = ''
    for field in ('vendor_name', 'address', 'product', 'quantity',
                  'payment_mode', 'status', 'date_display'):
        if field in data and data[field] is not None:
            setattr(record, field, str(data[field]))
    if 'products' in data:
        products_json, first_product = _parse_vendor_products(data)
        record.products = products_json
        if first_product:
            record.product = first_product
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
        "daily_history": daily_history,
        "alerts": alerts,
        "settings": settings.to_dict(),
    }


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


@app.route('/api/notifications', methods=['GET'])
def api_notifications():
    payload = _backup_payload()
    notes = []
    for r in payload["purchases"]:
        alert = _credit_alert_raw(r["vendor_name"], r.get("credit_days", 0),
                                  r.get("credit_due_date", ""), r.get("remaining", 0),
                                  r.get("status", ""))
        if not alert:
            continue
        due_on = alert["due_date"]
        if alert["overdue"]:
            notes.append({"icon": "fa-exclamation-circle", "tone": "danger",
                          "title": "Credit overdue",
                          "text": f"{alert['vendor']} is overdue by {-alert['days_left']} day(s) — Rs {r.get('remaining', 0):,} pending.",
                          "time": f"Due {due_on}"})
        elif alert["days_left"] == 0:
            notes.append({"icon": "fa-hourglass-half", "tone": "warn",
                          "title": "Credit due today",
                          "text": f"{alert['vendor']} payment is due today — Rs {r.get('remaining', 0):,} pending.",
                          "time": f"Due {due_on}"})
        else:
            notes.append({"icon": "fa-hourglass-half", "tone": "warn",
                          "title": "Credit due soon",
                          "text": f"{alert['vendor']} is due in {alert['days_left']} day(s) — Rs {r.get('remaining', 0):,} pending.",
                          "time": f"Due {due_on}"})
        if len(notes) >= 15:
            break
    for p in payload["products"]:
        if p["status"] == "Low Stock":
            notes.append({"icon": "fa-exclamation-triangle", "tone": "warn",
                          "title": "Low stock", "text": f"{p['name']} is low ({p['stock']} left).",
                          "time": "Needs attention"})
    for r in payload["resources"]:
        if r["status"] == "Critical":
            notes.append({"icon": "fa-times-circle", "tone": "danger",
                          "title": "Critical stock", "text": f"{r['name']} is critical ({r['closing']} left).",
                          "time": r["last_update"]})
        if len(notes) >= 12:
            break
    for p in payload["production"]:
        if p["status"] == "In Progress":
            notes.append({"icon": "fa-cogs", "tone": "info",
                          "title": "Production running", "text": f"{p['product']} batch {p['batch']} in progress.",
                          "time": p["date"]})
    outstanding = sum(p["remaining"] for p in payload["purchases"])
    if outstanding > 0 and len(notes) < 15:
        notes.append({"icon": "fa-file-invoice", "tone": "info",
                      "title": "Outstanding payments",
                      "text": f"Rs {outstanding:,} pending across purchase records.",
                      "time": "Accounts"})
    return _ok({"notifications": notes[:15], "count": len(notes[:15])})


@app.context_processor
def inject_nav_products():
    try:
        names = [p.name for p in Product.query.filter(Product.published.is_(True)).order_by(Product.id).all()]
        standard_names = {'Savi Gold Plus', 'Promin Gold', 'Promin Plus', 'Promin', 'Savi Gold', 'Savi Supreme'}
        production_names = []
        for p in Product.query.filter(Product.published.is_(True)).order_by(Product.id).all():
            if p.name in standard_names:
                production_names.append(p.name)
                continue
            saved = FormulaSheet.query.filter(FormulaSheet.product == p.name).first()
            if saved:
                production_names.append(p.name)
        third_names = [t.name for t in ThirdPartyItem.query.filter(ThirdPartyItem.published.is_(True)).order_by(ThirdPartyItem.name).all()]
    except Exception:
        names, production_names, third_names = [], [], []
    return {"nav_products": names, "nav_production_products": production_names, "nav_third_party": third_names}


@app.context_processor
def inject_user_profile():
    info = {
        "current_user_name": session.get('savi_user_name', ''),
        "current_user_role": session.get('savi_user_role', ''),
        "current_user_shift": session.get('savi_user_shift', ''),
        "current_user_login": "",
        "current_user_phone": "",
        "current_user_created": "",
    }
    uid = session.get('savi_user_id')
    if uid:
        try:
            row = db.session.get(User, uid)
            if row:
                info.update({
                    "current_user_name": row.full_name,
                    "current_user_role": row.role,
                    "current_user_shift": row.shift or "",
                    "current_user_login": row.login_id,
                    "current_user_phone": row.phone or "",
                    "current_user_created": row.created or "",
                })
        except Exception:
            pass
    return info


@app.context_processor
def inject_maintenance_mode():
    try:
        settings = _get_settings()
        return {
            "page_maintenance_mode": bool(settings.maintenance_mode),
            "page_maintenance_message": settings.maintenance_message or "",
        }
    except Exception:
        return {"page_maintenance_mode": False, "page_maintenance_message": ""}

@app.route('/api/third-party-items', methods=['GET'])
def api_third_party_items():
    items = ThirdPartyItem.query.order_by(ThirdPartyItem.name).all()
    return _ok({"items": [i.to_dict() for i in items]})


@app.route('/api/third-party-items', methods=['POST'])
def api_third_party_create():
    data = request.get_json(silent=True) or {}
    name = str(data.get('name', '')).strip()
    if not name:
        return _fail("Name required")
    item = ThirdPartyItem(name=name, category=data.get('category',''),
                           materials=data.get('materials','[]'), price=data.get('price',0))
    db.session.add(item)
    db.session.commit()
    return _ok({"success": True, "item": item.to_dict()})


@app.route('/api/formulas', methods=['GET'])
def api_formulas_list():
    sheets = FormulaSheet.query.order_by(FormulaSheet.id.desc()).all()
    return _ok({"formulas": [f.to_dict() for f in sheets]})


@app.route('/api/formulas', methods=['POST'])
def api_formulas_create():
    data = request.get_json(silent=True) or {}
    product = str(data.get('product', '')).strip()
    if not product:
        return _fail("Product is required")
    ingredients = data.get('ingredients', [])
    if not isinstance(ingredients, list) or not ingredients:
        return _fail("Ingredients must be a non-empty list")
    for ln in ingredients:
        if not isinstance(ln, dict): continue
        material = str(ln.get('material', '')).strip()
        category = str(ln.get('category', '')).strip()
        # F4: a crafted payload with old_machine="nan"/"1e999"/"abc" used to
        # reach bare float() and 500 the request. Reject non-finite values.
        try:
            old_q = float(ln.get('old_machine', 0) or 0)
            new_q = float(ln.get('new_machine', 0) or 0)
        except (TypeError, ValueError):
            return _fail("old_machine and new_machine must be numbers")
        if old_q != old_q or old_q in (float('inf'), float('-inf')):
            return _fail("old_machine must be a finite number")
        if new_q != new_q or new_q in (float('inf'), float('-inf')):
            return _fail("new_machine must be a finite number")
        if not material: continue
        existing = FormulaSheet.query.filter_by(product=product, category=category, material=material).first()
        if not existing:
            db.session.add(FormulaSheet(product=product, category=category, material=material,
                                         old_machine=old_q, new_machine=new_q,
                                         updated=datetime.now().strftime('%d %b %Y')))
    db.session.commit()
    return _ok({"success": True})


@app.route('/api/maintenance-status')
def api_maintenance_status():
    settings = _get_settings()
    return _ok({"maintenance_mode": bool(settings.maintenance_mode),
                "maintenance_message": settings.maintenance_message or ""})


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
    """Item 1 (2026-10-05): per-message delete for the inbox. Only removes the
    single row - /api/messages/clear wipes the whole table and had no UI."""
    msg = db.session.get(Message, message_id)
    if not msg:
        return _fail("Message not found")
    db.session.delete(msg)
    db.session.commit()
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()
    return _ok({"success": True, "messages": [m.to_dict() for m in reversed(msgs)]})


@app.route('/api/messages/send', methods=['POST'])
def api_send_message():
    """User -> Admin message. marked seen-by-User at birth so it never shows
    up as 'unread for me' on this portal, and counts as unread for the admin."""
    data = request.get_json(silent=True) or {}
    text = str(data.get('message', '')).strip()
    if not text:
        return _fail("Message cannot be empty")
    if len(text) > 500:
        return _fail("Message is too long (max 500 characters)")
    msg = Message(text=text, sender="User",
                  time=datetime.now().strftime('%Y-%m-%d %H:%M'), seen_by="User")
    db.session.add(msg)
    db.session.commit()
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()
    return _ok({"success": True, "messages": [m.to_dict() for m in reversed(msgs)]})


@app.route('/api/messages/status', methods=['GET'])
def api_message_status():
    msgs = Message.query.order_by(Message.id.desc()).limit(5).all()

    def _seen_by_user(m):
        return 'User' in [s.strip() for s in (m.seen_by or '').split(',') if s.strip()]

    unread = [m for m in msgs if not _seen_by_user(m)]
    return _ok({"unread_count": len(unread), "messages": [m.to_dict() for m in reversed(msgs)]})


@app.route('/api/messages/clear', methods=['POST'])
def api_clear_messages():
    from sqlalchemy import text as _text
    try:
        db.session.execute(_text("DELETE FROM messages"))
        db.session.commit()
    except Exception:
        pass
    return _ok({"success": True})


if __name__ == '__main__':
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, port=5001)
