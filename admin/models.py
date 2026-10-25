"""SQLAlchemy models for Savi Nutrition — higher-level persistent backend.

All tables mirror the previous in-memory demo structures so templates
keep receiving identical keys (UI stays as-is).
"""
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def compute_product_status(stock: int, low_threshold: int = 2000, critical_threshold: int = 1000) -> str:
    if stock <= critical_threshold:
        return "Critical"
    if stock <= low_threshold:
        return "Low Stock"
    return "In Stock"


def compute_raw_status(closing: int, min_stock: int) -> str:
    if closing <= min_stock * 0.2:
        return "Critical"
    if closing <= min_stock:
        return "Low Stock"
    return "Available"


def compute_resource_status(closing: int, minimum: int = 250) -> str:
    """Resource stock condition used by Raw Materials, Medicines and Chemicals.

    Fixed business thresholds requested by the user: below 5,000 = Critical,
    5,000-9,999 = Low Stock, and 10,000 kg or above = Good. ``minimum`` is
    retained for database compatibility.
    """
    if closing < 5000:
        return "Critical"
    if closing < 10000:
        return "Low Stock"
    return "Good"


class Product(db.Model):
    __tablename__ = "products"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    category = db.Column(db.String(120), default="Finished Goods")
    price = db.Column(db.Integer, default=0)
    stock = db.Column(db.Integer, default=0)
    image = db.Column(db.String(120), default="")
    # Dashboard photo bytes (MEDIUMBLOB) - the serverless fallback so a photo
    # still saves on Vercel where no S3 bucket is configured. Never serialised
    # into to_dict().
    image_data = db.Column(db.LargeBinary)
    image_mime = db.Column(db.String(100))
    status = db.Column(db.String(40), default="In Stock")
    published = db.Column(db.Boolean, default=False)
    old_machine_bags = db.Column(db.Float, default=None)
    new_machine_bags = db.Column(db.Float, default=None)
    last_update = db.Column(db.String(80), default="")

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "category": self.category,
            "price": self.price, "stock": self.stock,
            "image": self.image, "status": self.status, "published": self.published,
            "last_update": self.last_update or "",
        }


class RawMaterialSale(db.Model):
    """One raw-material sale entered on the user portal Raw Materials Sales page.
    The quantity is deducted from the matching Resource via Resource.sold so the
    closing-stock formula (opening + purchased - consumed - sold) stays honest."""
    __tablename__ = "raw_material_sales"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(40), default="")
    material = db.Column(db.String(120), nullable=False)
    quantity = db.Column(db.Integer, default=0)
    amount = db.Column(db.Integer, default=0)
    buyer = db.Column(db.String(120), default="")
    customer_contact = db.Column(db.String(20), default="")
    invoice_number = db.Column(db.String(60), default="")
    bill_quantity = db.Column(db.Integer, default=0)
    balance_amount = db.Column(db.Integer, default=0)
    total_amount = db.Column(db.Integer, default=0)
    invoice_attachment = db.Column(db.String(255), default="")
    invoice_data = db.Column(db.LargeBinary, nullable=True)
    invoice_mime = db.Column(db.String(100), default="")
    weighment_attachment = db.Column(db.String(255), default="")
    weighment_data = db.Column(db.LargeBinary, nullable=True)
    weighment_mime = db.Column(db.String(100), default="")
    created = db.Column(db.String(80), default="")

    def to_dict(self):
        return {
            "id": self.id, "date": self.date or "", "material": self.material,
            "quantity": self.quantity or 0, "amount": self.amount or 0,
            "buyer": self.buyer or "",
            "customer_contact": self.customer_contact or "",
            "invoice_number": self.invoice_number or "",
            "bill_quantity": self.bill_quantity or 0,
            "balance_amount": self.balance_amount or 0,
            "total_amount": self.total_amount or 0,
            "invoice_attachment": self.invoice_attachment or "",
            "weighment_attachment": self.weighment_attachment or "",
            "created": self.created or "",
        }


class ProductSale(db.Model):
    """One finished-goods sale entered on the user portal Product Sales page.
    The quantity is deducted from Product.stock on create and restored on delete."""
    __tablename__ = "product_sales"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(40), default="")
    product = db.Column(db.String(120), nullable=False)
    quantity = db.Column(db.Integer, default=0)
    amount = db.Column(db.Integer, default=0)
    customer = db.Column(db.String(120), default="")
    invoice_number = db.Column(db.String(60), default="")
    customer_contact = db.Column(db.String(20), default="")
    # Change #59 (2026-10-09): typed in Add Sales after the contact number,
    # shown in the Salesman column on both portals.
    salesman_name = db.Column(db.String(120), default="")
    balance_amount = db.Column(db.Integer, default=0)
    # Change #60 (2026-10-09): Add Sales re-spec. The typed box "Amount to be
    # paid" was renamed **Total Amount**; the new **Globle Discount** may be
    # entered as an amount OR a percentage (amount wins when both are set) and
    # is subtracted from the Total Amount to give the new **Amount to be paid**
    # column (computed at render time, never stored). NULL = never entered.
    total_amount = db.Column(db.Integer, nullable=True, default=None)
    discount_amount = db.Column(db.Float, nullable=True, default=None)
    discount_percent = db.Column(db.Float, nullable=True, default=None)
    invoice_attachment = db.Column(db.String(255), default="")
    invoice_data = db.Column(db.LargeBinary, nullable=True)
    invoice_mime = db.Column(db.String(100), default="")
    created = db.Column(db.String(80), default="")
    # Change #56 (2026-10-09): multi-product sale. `location` is the typed
    # Location; `items` is a JSON list [{"product": name, "qty": bags}] -
    # empty/NULL = legacy single-product row (product + quantity columns).
    # Change #59: each entry may carry an optional "price" (the per-product
    # Price box) - stored only when one was typed.
    location = db.Column(db.String(120), default="")
    items = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id, "date": self.date or "", "product": self.product,
            "quantity": self.quantity or 0, "amount": self.amount or 0,
            "customer": self.customer or "",
            "invoice_number": self.invoice_number or "",
            "customer_contact": self.customer_contact or "",
            "salesman_name": self.salesman_name or "",
            "balance_amount": self.balance_amount or 0,
            "total_amount": self.total_amount,
            "discount_amount": self.discount_amount,
            "discount_percent": self.discount_percent,
            "invoice_attachment": self.invoice_attachment or "",
            "created": self.created or "",
            "location": self.location or "",
            "items": self.items or "",
        }


class SalesRemark(db.Model):
    """One payment remark saved from the user portal Remarks page (Change #55).
    Tracks a partial payment (Amount Received) against a raw-material or product
    sale: new balance = sale's balance - amount received, and the sale row's
    balance_amount is updated to the new balance so the sales page shows it."""
    __tablename__ = "sales_remarks"
    id = db.Column(db.Integer, primary_key=True)
    sale_type = db.Column(db.String(20), default="")   # 'raw_material' | 'product'
    sale_id = db.Column(db.Integer, default=0)
    invoice_number = db.Column(db.String(60), default="")
    customer_name = db.Column(db.String(120), default="")
    product = db.Column(db.String(120), default="")
    total_amount = db.Column(db.Integer, default=0)
    amount_received = db.Column(db.Integer, default=0)
    balance = db.Column(db.Integer, default=0)
    date = db.Column(db.String(40), default="")
    payment_mode = db.Column(db.String(40), default="")
    # Change #57 (2026-10-09): free-text block under the Date box in Add Remarks.
    notes = db.Column(db.Text, default="")
    created = db.Column(db.String(80), default="")

    def to_dict(self):
        return {
            "id": self.id, "sale_type": self.sale_type or "",
            "sale_id": self.sale_id or 0,
            "invoice_number": self.invoice_number or "",
            "customer_name": self.customer_name or "",
            "product": self.product or "",
            "total_amount": self.total_amount or 0,
            "amount_received": self.amount_received or 0,
            "balance": self.balance or 0,
            "date": self.date or "",
            "payment_mode": self.payment_mode or "",
            "notes": self.notes or "",
            "created": self.created or "",
        }


class ProductMaterial(db.Model):
    """Raw-material requirement row for one finished product."""
    __tablename__ = "product_materials"
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    product_name = db.Column(db.String(120), nullable=False)
    material = db.Column(db.String(120), nullable=False)
    unit = db.Column(db.String(20), default="kg")
    opening = db.Column(db.Integer, default=0)
    consumed = db.Column(db.Integer, default=0)
    closing = db.Column(db.Integer, default=0)
    batch = db.Column(db.String(80), default="")
    status = db.Column(db.String(40), default="Available")
    old_batches = db.Column(db.Integer, default=0)
    new_batches = db.Column(db.Integer, default=0)
    old_machine_qty = db.Column(db.Float, default=0)
    new_machine_qty = db.Column(db.Float, default=0)

    @property
    def total_batches(self):
        return (self.old_batches or 0) + (self.new_batches or 0)

    @property
    def total_output(self):
        return self.consumed * (self.total_batches)

    def to_dict(self):
        return {
            "id": self.id, "material": self.material, "unit": self.unit,
            "opening": self.opening, "consumed": self.consumed,
            "closing": self.closing, "batch": self.batch or "",
            "status": self.status,
            "old_batches": self.old_batches or 0,
            "new_batches": self.new_batches or 0,
            "old_machine_qty": self.old_machine_qty if self.old_machine_qty is not None else 0,
            "new_machine_qty": self.new_machine_qty if self.new_machine_qty is not None else 0,
            "total_batches": self.total_batches,
            "total_output": self.total_output,
        }


class MaterialDaily(db.Model):
    """Stored daily stock log per raw material (today + past days)."""
    __tablename__ = "material_daily"
    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(120), nullable=False)
    material = db.Column(db.String(120), nullable=False)
    date = db.Column(db.String(20), nullable=False)  # YYYY-MM-DD
    opening = db.Column(db.Integer, default=0)
    consumed = db.Column(db.Integer, default=0)
    closing = db.Column(db.Integer, default=0)
    batch = db.Column(db.String(80), default="")
    supervisor_name = db.Column(db.String(120), default="")
    shift = db.Column(db.String(10), default="morning")
    old_bags = db.Column(db.Integer, default=None)
    new_bags = db.Column(db.Integer, default=None)
    # 3rd-party products: supervisor-typed Old/New Machine Qty (kg) per material.
    # NULL = never stored for this shift.
    old_qty = db.Column(db.Float, default=None)
    new_qty = db.Column(db.Float, default=None)

    def to_dict(self):
        return {
            "id": self.id, "product_name": self.product_name,
            "material": self.material, "date": self.date,
            "opening": self.opening, "consumed": self.consumed,
            "closing": self.closing, "batch": self.batch or "",
            "supervisor_name": self.supervisor_name or "",
            "shift": self.shift or "morning",
            "old_bags": self.old_bags, "new_bags": self.new_bags,
            "old_qty": self.old_qty, "new_qty": self.new_qty,
        }


class Resource(db.Model):
    """Resource Maintenance row: Raw Materials / Medicines / Chemicals."""
    __tablename__ = "resources"
    id = db.Column(db.Integer, primary_key=True)
    group = db.Column(db.String(80), nullable=False)  # Raw Materials | Medicines | ...
    name = db.Column(db.String(120), nullable=False)
    item_category = db.Column(db.String(120), default="")
    unit = db.Column(db.String(20), default="kg")
    opening = db.Column(db.Integer, default=0)
    purchased = db.Column(db.Integer, default=0)
    consumed = db.Column(db.Integer, default=0)
    sold = db.Column(db.Integer, default=0)
    closing = db.Column(db.Integer, default=0)
    min_stock = db.Column(db.Integer, default=250)
    status = db.Column(db.String(40), default="Good")
    last_update = db.Column(db.String(80), default="")

    def recompute(self):
        self.closing = max((self.opening or 0) + (self.purchased or 0)
                           - (self.consumed or 0) - (self.sold or 0), 0)
        self.status = compute_resource_status(self.closing, self.min_stock or 250)

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "category": self.item_category,
            "group": self.group, "unit": self.unit, "opening": self.opening,
            "purchased": self.purchased, "consumed": self.consumed,
            "sold": self.sold,
            "closing": self.closing, "min_stock": self.min_stock,
            "status": self.status, "last_update": self.last_update,
        }


class RawMaterial(db.Model):
    __tablename__ = "raw_materials"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    unit = db.Column(db.String(20), default="kg")
    opening = db.Column(db.Integer, default=0)
    consumed = db.Column(db.Integer, default=0)
    closing = db.Column(db.Integer, default=0)
    status = db.Column(db.String(40), default="Available")
    min_stock = db.Column(db.Integer, default=30)

    def recompute(self):
        if self.closing <= self.min_stock * 0.2:
            self.status = "Critical"
        elif self.closing <= self.min_stock:
            self.status = "Low Stock"
        else:
            self.status = "Available"

    def to_dict(self):
        return {
            "id": self.id, "name": self.name, "unit": self.unit,
            "opening": self.opening, "consumed": self.consumed,
            "closing": self.closing, "status": self.status,
            "min_stock": self.min_stock,
        }


class ProductionEntry(db.Model):
    __tablename__ = "production"
    id = db.Column(db.Integer, primary_key=True)
    product = db.Column(db.String(120), nullable=False)
    date = db.Column(db.String(20), nullable=False)  # YYYY-MM-DD
    quantity = db.Column(db.Integer, default=0)
    batch = db.Column(db.String(80), unique=True, nullable=False)
    status = db.Column(db.String(40), default="Completed")
    notes = db.Column(db.String(255), default="")
    machine = db.Column(db.String(20), default="Old Machine")
    consumed = db.Column(db.Integer, default=0)

    def to_dict(self):
        return {
            "id": self.id, "product": self.product, "date": self.date,
            "quantity": self.quantity, "batch": self.batch,
            "status": self.status, "notes": self.notes or "",
            "machine": self.machine or "Old Machine",
            "consumed": self.consumed or 0,
            "outcome": max((self.quantity or 0) - (self.consumed or 0), 0),
        }


class MachineProductionEntry(db.Model):
    """Saved production details from user-side old/new machine update pages."""
    __tablename__ = "machine_production_entries"
    id = db.Column(db.Integer, primary_key=True)
    product_name = db.Column(db.String(120), nullable=False)
    material = db.Column(db.String(120), nullable=False)
    machine = db.Column(db.String(20), nullable=False)
    batch = db.Column(db.String(80), default="")
    quantity = db.Column(db.Float, default=0)
    consumed = db.Column(db.Float, default=0)
    output = db.Column(db.Float, default=0)

    def to_dict(self):
        return {
            "id": self.id, "product_name": self.product_name,
            "material": self.material, "machine": self.machine,
            "batch": self.batch or "", "quantity": self.quantity or 0,
            "consumed": self.consumed or 0, "output": self.output or 0,
        }


class Purchase(db.Model):
    __tablename__ = "purchases"
    id = db.Column(db.Integer, primary_key=True)
    vendor_name = db.Column(db.String(160), nullable=False)
    address = db.Column(db.Text, default="")
    vendor_phone = db.Column(db.String(30), default="")
    v_number = db.Column(db.String(40), unique=True, nullable=False)
    invoice_number = db.Column(db.String(60), default="")
    date_display = db.Column(db.String(40), default="")
    product = db.Column(db.String(120), default="")
    quantity = db.Column(db.String(40), default="")
    amount_paid = db.Column(db.Integer, default=0)
    remaining = db.Column(db.Integer, default=0)
    payment_mode = db.Column(db.String(40), default="Cash")
    status = db.Column(db.String(20), default="Paid")
    vehicle_number = db.Column(db.String(30), default="")
    bill_quantity = db.Column(db.String(40), default="")
    bill_unit = db.Column(db.String(10), default="Kgs")
    contain_quantity = db.Column(db.String(40), default="")
    contain_unit = db.Column(db.String(10), default="Kgs")
    slip_attachment = db.Column(db.String(255), default="")
    # DB-backed slip storage: on Vercel there is no durable file storage, so
    # uploaded slip bytes live here (see _prepare_slip in user/app.py).
    slip_data = db.Column(db.LargeBinary)
    slip_mime = db.Column(db.String(100), default="")
    # Second attachment (Weigment) - same storage pattern as the slip (Invoice).
    weighment_attachment = db.Column(db.String(255), default="")
    weighment_data = db.Column(db.LargeBinary)
    weighment_mime = db.Column(db.String(100), default="")
    arrival_date = db.Column(db.String(20), default="")
    credit_days = db.Column(db.Integer, default=0)
    credit_due_date = db.Column(db.String(20), default="")
    driver_name = db.Column(db.String(120), default="")
    driver_phone = db.Column(db.String(30), default="")

    def to_dict(self):
        return {
            "id": self.id, "vendor_name": self.vendor_name,
            "address": self.address or "", "vendor_phone": self.vendor_phone or "",
            "v_number": self.v_number,
            "invoice_number": self.invoice_number or "",
            "date_display": self.date_display, "product": self.product,
            "quantity": self.quantity,
            "amount_paid": f"{self.amount_paid:,}",
            "remaining": f"{self.remaining:,}",
            "payment_mode": self.payment_mode, "status": self.status,
            "vehicle_number": self.vehicle_number or "",
            "bill_quantity": self.bill_quantity or "",
            "bill_unit": self.bill_unit or "Kgs",
            "contain_quantity": self.contain_quantity or "",
            "contain_unit": self.contain_unit or "Kgs",
            "slip_attachment": self.slip_attachment or "",
            "weighment_attachment": self.weighment_attachment or "",
            "arrival_date": self.arrival_date or "",
            "credit_days": self.credit_days or 0,
            "credit_due_date": self.credit_due_date or "",
            "driver_name": self.driver_name or "",
            "driver_phone": self.driver_phone or "",
        }


class Vendor(db.Model):
    """Master Data vendor record (user-created vendor list)."""
    __tablename__ = "vendors"
    id = db.Column(db.Integer, primary_key=True)
    vendor_name = db.Column(db.String(160), nullable=False)
    address = db.Column(db.Text, default="")
    vendor_phone = db.Column(db.String(30), default="")
    gstin = db.Column(db.String(15), default="")
    date_display = db.Column(db.String(40), default="")
    category = db.Column(db.String(40), default="Raw Materials")
    product = db.Column(db.String(120), default="")
    quantity = db.Column(db.String(40), default="")
    amount_paid = db.Column(db.Integer, default=0)
    remaining = db.Column(db.Integer, default=0)
    payment_mode = db.Column(db.String(40), default="Cash")
    status = db.Column(db.String(20), default="Paid")
    products = db.Column(db.Text, default="")

    def to_dict(self):
        return {
            "id": self.id, "vendor_name": self.vendor_name,
            "address": self.address or "", "vendor_phone": self.vendor_phone or "",
            "gstin": self.gstin or "",
            "date_display": self.date_display, "category": self.category or "Raw Materials",
            "product": self.product,
            "products": self.products or "",
            "quantity": self.quantity,
            "amount_paid": f"{self.amount_paid:,}",
            "remaining": f"{self.remaining:,}",
            "payment_mode": self.payment_mode, "status": self.status,
        }


class Formula(db.Model):
    """Predefined feed formula.

    Ingredient lines: {category, material, old_machine, new_machine}.
    Older rows with {material, qty} are normalized on read.
    """
    __tablename__ = "formulas"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), unique=True, nullable=False)
    product = db.Column(db.String(120), default="")
    ingredients = db.Column(db.Text, default="[]")
    notes = db.Column(db.String(255), default="")
    created = db.Column(db.String(40), default="")

    def lines(self):
        import json
        try:
            data = json.loads(self.ingredients or "[]")
            data = data if isinstance(data, list) else []
        except (TypeError, ValueError):
            return []
        out = []
        for i, ln in enumerate(data, start=1):
            if not isinstance(ln, dict):
                continue
            try:
                old_q = float(ln.get("old_machine", ln.get("qty", 0)) or 0)
            except (TypeError, ValueError):
                old_q = 0
            try:
                new_q = float(ln.get("new_machine", ln.get("qty", 0)) or 0)
            except (TypeError, ValueError):
                new_q = 0
            out.append({
                "slno": i,
                "category": str(ln.get("category", "") or ""),
                "material": str(ln.get("material", "") or ""),
                "old_machine": old_q,
                "new_machine": new_q,
            })
        return out

    def to_dict(self):
        lines = self.lines()
        return {
            "id": self.id, "name": self.name, "product": self.product or "",
            "ingredients": lines, "lines_count": len(lines),
            "total_old": round(sum(l["old_machine"] for l in lines), 2),
            "total_new": round(sum(l["new_machine"] for l in lines), 2),
            "notes": self.notes or "", "created": self.created or "",
        }


class FormulaSheet(db.Model):
    """Production Formula working sheet: one old/new machine qty per material per list."""
    __tablename__ = "formula_sheet"
    id = db.Column(db.Integer, primary_key=True)
    product = db.Column(db.String(120), default="")
    category = db.Column(db.String(80), nullable=False)
    material = db.Column(db.String(120), nullable=False)
    old_machine = db.Column(db.Float, default=0)
    new_machine = db.Column(db.Float, default=0)
    updated = db.Column(db.String(40), default="")

    def to_dict(self):
        return {
            "id": self.id, "product": self.product or "",
            "category": self.category, "material": self.material,
            "old_machine": self.old_machine or 0, "new_machine": self.new_machine or 0,
            "updated": self.updated or "",
        }


class ThirdPartyItem(db.Model):
    """Admin-managed third-party items list on the formula page."""
    __tablename__ = "third_party_items"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    category = db.Column(db.String(80), default="Raw Materials")
    quantity = db.Column(db.Float, default=0)
    unit = db.Column(db.String(20), default="kg")
    materials = db.Column(db.Text, default="[]")  # JSON list of {material, opening}
    price = db.Column(db.Float, default=0)
    notes = db.Column(db.String(255), default="")
    created = db.Column(db.String(40), default="")
    published = db.Column(db.Boolean, default=False)
    image = db.Column(db.String(120), default="")
    # Dashboard photo bytes (MEDIUMBLOB) - the serverless fallback so a photo
    # still saves on Vercel where no S3 bucket is configured. Never serialised
    # into to_dict().
    image_data = db.Column(db.LargeBinary)
    image_mime = db.Column(db.String(100))
    # Production-update stamp ("%d %b %Y, %I:%M %p", same as Product.last_update)
    # written by the user-portal 3rd-party production save; shown as the
    # "Last Update" column on Product Details (admin + user).
    last_update = db.Column(db.String(60), default="")

    def mat_lines(self):
        import json
        try:
            data = json.loads(self.materials or "[]")
            return [x for x in data if isinstance(x, dict) and x.get("material")]
        except (TypeError, ValueError):
            return []

    def to_dict(self):
        return {
            "id": self.id, "name": self.name,
            "category": self.category or "Raw Materials",
            "quantity": self.quantity or 0, "unit": self.unit or "kg",
            "materials": self.mat_lines(),
            "materials_count": len(self.mat_lines()),
            "image": self.image or "",
            "price": self.price or 0,
            "notes": self.notes or "", "created": self.created or "",
            "last_update": self.last_update or "",
            "published": self.published,
        }


class ConsumptionLog(db.Model):
    """Who consumed what and when — admin tracking view."""
    __tablename__ = "consumption_log"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), default="")
    product_name = db.Column(db.String(120), default="")
    material = db.Column(db.String(120), default="")
    quantity = db.Column(db.Integer, default=0)
    actor = db.Column(db.String(40), default="Admin")

    def to_dict(self):
        return {
            "id": self.id, "date": self.date,
            "product_name": self.product_name, "material": self.material,
            "quantity": self.quantity, "actor": self.actor or "Admin",
        }


class StockMovement(db.Model):
    __tablename__ = "stock_movements"
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(40), default="")
    material = db.Column(db.String(120), default="")
    movement = db.Column(db.String(40), default="")
    quantity = db.Column(db.Integer, default=0)
    unit = db.Column(db.String(20), default="unit")
    reference = db.Column(db.String(80), default="")

    def to_dict(self):
        return {
            "date": self.date, "material": self.material,
            "movement": self.movement, "quantity": self.quantity,
            "unit": self.unit, "reference": self.reference,
        }


class Setting(db.Model):
    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(160), default="SaviNutritions Pvt Ltd")
    location = db.Column(db.String(160), default="Main Warehouse")
    currency_symbol = db.Column(db.String(8), default="₹")
    allocation_rule = db.Column(db.String(10), default="FIFO")
    low_stock_notifications = db.Column(db.Boolean, default=True)
    daily_digest = db.Column(db.Boolean, default=True)
    maintenance_mode = db.Column(db.Boolean, default=False)
    maintenance_message = db.Column(db.Text, default="")
    active_shift = db.Column(db.String(10), default="morning")
    change_version = db.Column(db.Integer, default=0)
    admin_change_version = db.Column(db.Integer, default=0)
    active_user_id = db.Column(db.Integer, default=0)
    active_user_name = db.Column(db.String(120), default="")
    active_user_login = db.Column(db.String(120), default="")
    active_user_phone = db.Column(db.String(30), default="")
    active_user_shift = db.Column(db.String(20), default="")
    active_user_at = db.Column(db.String(40), default="")
    admin_contact_phone = db.Column(db.String(30), default="")
    backup_enabled = db.Column(db.Boolean, default=True)
    backup_local_dir = db.Column(db.String(500), default="")
    backup_drive_dir = db.Column(db.String(500), default="")
    backup_keep_days = db.Column(db.Integer, default=30)
    backup_last_run = db.Column(db.String(60), default="")
    backup_last_status = db.Column(db.String(500), default="")
    backup_last_slot = db.Column(db.String(40), default="")

    def to_dict(self):
        return {
            "company_name": self.company_name, "location": self.location,
            "currency_symbol": self.currency_symbol,
            "allocation_rule": self.allocation_rule,
            "low_stock_notifications": self.low_stock_notifications,
            "daily_digest": self.daily_digest,
            "maintenance_mode": self.maintenance_mode,
            "maintenance_message": self.maintenance_message,
            "admin_contact_phone": self.admin_contact_phone or "",
            "backup_enabled": bool(self.backup_enabled) if self.backup_enabled is not None else True,
            "backup_local_dir": self.backup_local_dir or "",
            "backup_drive_dir": self.backup_drive_dir or "",
            "backup_keep_days": self.backup_keep_days or 30,
            "backup_last_run": self.backup_last_run or "",
            "backup_last_status": self.backup_last_status or "",
            "backup_last_slot": self.backup_last_slot or "",
            "active_shift": (self.active_shift or "morning"),
            "change_version": self.change_version or 0,
            "admin_change_version": self.admin_change_version or 0,
            "active_user": {
                "id": self.active_user_id or 0,
                "full_name": self.active_user_name or "",
                "login_id": self.active_user_login or "",
                "phone": self.active_user_phone or "",
                "shift": self.active_user_shift or "",
                "at": self.active_user_at or "",
            },
        }


class User(db.Model):
    """Registered account. Admin app uses role='admin'; the user portal uses
    role='user' with a fixed morning/night shift chosen at signup. The user's
    shift decides which shift the admin Production Update page is locked to."""
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(120), nullable=False)
    login_id = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(30), default="")
    password = db.Column(db.String(255), nullable=False)
    password_plain = db.Column(db.String(255), default="")
    shift = db.Column(db.String(20), default="")
    role = db.Column(db.String(20), default="user")
    created = db.Column(db.String(40), default="")

    def to_dict(self):
        return {
            "id": self.id, "full_name": self.full_name, "login_id": self.login_id,
            "phone": self.phone or "", "shift": self.shift or "",
            "role": self.role or "user", "created": self.created or "",
        }


class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    sender = db.Column(db.String(40), default="Admin")
    time = db.Column(db.String(40), default="")
    seen_by = db.Column(db.Text, default="")

    def to_dict(self):
        return {
            "id": self.id, "text": self.text, "sender": self.sender,
            "time": self.time, "seen_by": self.seen_by.split(",") if self.seen_by else [],
        }


class NotificationDismiss(db.Model):
    """Keys of notifications the admin has deleted so they stay hidden."""
    __tablename__ = "notification_dismiss"
    id = db.Column(db.Integer, primary_key=True)
    nkey = db.Column(db.String(200), unique=True, nullable=False)
    created = db.Column(db.String(40), default="")


class DriverDetails(db.Model):
    """Driver information entered by the user (shared driver_details table)."""
    __tablename__ = "driver_details"
    id = db.Column(db.Integer, primary_key=True)
    driver_name = db.Column(db.String(120), default="")
    contact_number = db.Column(db.String(30), default="")
    vehicle_number = db.Column(db.String(30), default="")
    arrival_date = db.Column(db.String(20), default="")
    qty_in_bill = db.Column(db.String(40), default="")
    actual_qty = db.Column(db.String(40), default="")
    slip_attachment = db.Column(db.String(255), default="")
    slip_data = db.Column(db.LargeBinary)
    slip_mime = db.Column(db.String(100), default="")
    # Second attachment (Weigment) - same storage pattern as the slip (Invoice).
    weighment_attachment = db.Column(db.String(255), default="")
    weighment_data = db.Column(db.LargeBinary)
    weighment_mime = db.Column(db.String(100), default="")

    def to_dict(self):
        return {
            "id": self.id, "driver_name": self.driver_name or "",
            "contact_number": self.contact_number or "",
            "vehicle_number": self.vehicle_number or "",
            "arrival_date": self.arrival_date or "",
            "qty_in_bill": self.qty_in_bill or "",
            "actual_qty": self.actual_qty or "",
            "slip_attachment": self.slip_attachment or "",
            "weighment_attachment": self.weighment_attachment or "",
        }


class PasswordReset(db.Model):
    """One admin Forgot-Password OTP. Table created by db.create_all().
    code_hash = sha256 of the 6-digit code (never store the code itself)."""
    __tablename__ = "password_resets"
    id = db.Column(db.Integer, primary_key=True)
    login_id = db.Column(db.String(120), nullable=False)
    code_hash = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0)
    used = db.Column(db.Boolean, default=False)
