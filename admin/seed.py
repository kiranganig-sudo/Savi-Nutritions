"""Seed SQLite with the current demo data on first run (UI unchanged)."""
from datetime import datetime, timedelta

try:
    from .models import (
        Product, ProductMaterial, Resource, RawMaterial,
        ProductionEntry, Purchase, StockMovement, Setting,
        compute_product_status,
    )
except ImportError:
    from models import (
        Product, ProductMaterial, Resource, RawMaterial,
        ProductionEntry, Purchase, StockMovement, Setting,
        compute_product_status,
    )


# Change #45 (2026-10-06): user's replacement product list (21 items, verbatim).
# price/stock start at 0; the three exact-name matches keep their bundled photos.
PRODUCTS = [
    {"name": "Savi Super Gold 4 MM", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi 8020 Savi", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Supreme 6MM", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi Supreme 4 MM", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi Goat Feed 25 Kg", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi Goat Feed 50 Kg", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Calf Starter", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Nutri Mash", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Gold Plus", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi Mixed Feed", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi New gold 4mm", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Savi New gold 6MM", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Doctor Feed", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Promin", "category": "Finished Goods", "price": 0, "stock": 0, "image": "promin.jpeg"},
    {"name": "Promin Plus", "category": "Finished Goods", "price": 0, "stock": 0, "image": "promin_plus.jpeg"},
    {"name": "Promin Gold", "category": "Finished Goods", "price": 0, "stock": 0, "image": "promin_gold.jpeg"},
    {"name": "LCR for Cattle", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Grower", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "High Performance Calf Starter", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Layer", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
    {"name": "Low quality Feed", "category": "Finished Goods", "price": 0, "stock": 0, "image": ""},
]

# New products start with an EMPTY formula sheet (Change #27/#39) - no demo BOM.
PRODUCT_RAW_MATERIALS = {}

# Change #45: user's replacement material catalogs (verbatim names, counts
# 33 / 9 / 36). Same content in admin/seed.py, user/seed.py and the
# FORMULA_CATEGORIES dict in admin/app.py - keep all four in sync.
RESOURCE_CATEGORIES = {
    "Raw Materials": [
        ("Maize", "Raw Material"), ("Maize DDGS", "Raw Material"), ("Maize Bran", "Raw Material"),
        ("Maize DOC", "Raw Material"), ("Varagu", "Raw Material"), ("Copra(Coconut Doc)", "Raw Material"),
        ("Rice Polish", "Raw Material"), ("Hullar", "Raw Material"), ("Coffee Husk", "Raw Material"),
        ("Rapeseed", "Raw Material"), ("Ragi husk", "Raw Material"), ("Rice", "Raw Material"),
        ("DORB", "Raw Material"), ("Soya", "Raw Material"), ("Cotton DOC", "Raw Material"),
        ("GNC cake", "Raw Material"), ("Rice DDGS", "Raw Material"), ("Maize Powder", "Raw Material"),
        ("Wheat Bran", "Raw Material"), ("Jowari", "Raw Material"), ("Molasses", "Raw Material"),
        ("Turmeric Spent", "Raw Material"), ("Hallar polish", "Raw Material"), ("Fat", "Raw Material"),
        ("Molasses Plus", "Raw Material"), ("Stone grit", "Raw Material"), ("Micro fat", "Raw Material"),
        ("Copra Cake (AK)", "Raw Material"), ("Pomegranate Powder", "Raw Material"), ("Liver Powder", "Raw Material"),
        ("Dried Paneer", "Raw Material"), ("HUSK POWDER", "Raw Material"), ("Biscuit Animal Feed", "Raw Material"),
    ],
    "Chemicals": [
        ("Calcium LSP(Calcite powder)", "Chemical"), ("Salt", "Chemical"), ("Urea", "Chemical"),
        ("Bentonite", "Chemical"), ("DCP", "Chemical"), ("Rumikon (soda)", "Chemical"),
        ("Alkarb (sodium carbonate)", "Chemical"), ("Magnesium sulpate", "Chemical"), ("Sodakarb", "Chemical"),
    ],
    "Medicines": [
        ("Mineral powder", "Medicine"), ("Methinione", "Medicine"), ("sugarcap", "Medicine"),
        ("Yeast", "Medicine"), ("Heptacare(Toxin binder)", "Medicine"), ("Kemzyme fibroforte", "Medicine"),
        ("kemtrace maxim", "Medicine"), ("Mycocurb", "Medicine"), ("Relimax 9", "Medicine"),
        ("Endox dry", "Medicine"), ("Lysine", "Medicine"), ("Toxifin 360", "Medicine"),
        ("Metipearl", "Medicine"), ("Toxifin 300", "Medicine"), ("Kemtrace chromium", "Medicine"),
        ("Selliseo", "Medicine"), ("Biotin 2%", "Medicine"), ("Liv 52", "Medicine"),
        ("Kemtrace BMX", "Medicine"), ("Choline 60", "Medicine"), ("Layer Spl", "Medicine"),
        ("Aleta", "Medicine"), ("Clostat-365", "Medicine"), ("Lexi-vit", "Medicine"),
        ("Kemzyme One advance hc Dry", "Medicine"), ("Addarome vanco", "Medicine"),
        ("Nipro-55", "Medicine"), ("Zues Enzyme", "Medicine"), ("Poultry Methionine", "Medicine"),
        ("Aromax", "Medicine"), ("Yea-sacc", "Medicine"), ("Biochal BAD3E", "Medicine"),
        ("Metipro", "Medicine"), ("PROTEASE", "Medicine"), ("PHYTASE", "Medicine"),
        ("NPS Enzyme", "Medicine"),
    ],
}

# Demo history seeded only on an EMPTY database - old product names are gone,
# so there is nothing meaningful left to resurrect (Change #45).
PRODUCTIONS = []

PURCHASES = []

MOVEMENTS = []


def seed_database(db):
    """Insert demo data only when tables are empty. Returns True if seeded."""
    if Product.query.count() > 0:
        return False

    product_ids = {}
    for p in PRODUCTS:
        row = Product(
            name=p["name"], category=p["category"], price=p["price"],
            stock=p["stock"], image=p["image"],
            status=compute_product_status(p["stock"]),
            published=True,
        )
        db.session.add(row)
        db.session.flush()
        product_ids[p["name"]] = row.id

    for product_name, materials in PRODUCT_RAW_MATERIALS.items():
        pid = product_ids.get(product_name)
        prefix = "".join(w[0] for w in product_name.split() if w).upper()[:3] or "BAT"
        for index, material in enumerate(materials, start=1):
            opening = 100 + (index * 25)
            consumed = 10 + (index * 5)
            closing = max(opening - consumed, 0)
            db.session.add(ProductMaterial(
                product_id=pid, product_name=product_name, material=material,
                unit="kg", opening=opening, consumed=consumed, closing=closing,
                batch=f"{prefix}-{index:03d}",
                status="Low Stock" if closing < 150 else "Available",
            ))

    for group, items in RESOURCE_CATEGORIES.items():
        for i, (name, item_category) in enumerate(items, 1):
            opening = 500 + i * 25
            consumed = 40 + i * 7
            closing = opening - consumed
            minimum = 250
            if closing >= 10000:
                status = "Good"
            elif closing >= 5000:
                status = "Low Stock"
            else:
                status = "Critical"
            db.session.add(Resource(
                group=group, name=name, item_category=item_category, unit="kg",
                opening=opening, consumed=consumed, closing=closing,
                min_stock=minimum, status=status,
                last_update="05 Sep 2026, 06:08 PM",
            ))

    # RawMaterial summary mirrors finished goods (same as previous demo).
    for p in PRODUCTS:
        low = p["stock"] <= 30
        critical = p["stock"] <= 6
        status = "Critical" if critical else ("Low Stock" if low else "Available")
        db.session.add(RawMaterial(
            name=p["name"], unit="unit", opening=p["stock"], consumed=0,
            closing=p["stock"], status=status, min_stock=30,
        ))

    for pr in PRODUCTIONS:
        db.session.add(ProductionEntry(**pr))
    for pu in PURCHASES:
        db.session.add(Purchase(**pu))
    for mv in MOVEMENTS:
        db.session.add(StockMovement(**mv))

    db.session.add(Setting(
        company_name="SaviNutritions Pvt Ltd", location="Main Warehouse",
        currency_symbol="₹", allocation_rule="FIFO",
        low_stock_notifications=True, daily_digest=True,
    ))
    db.session.commit()
    return True
