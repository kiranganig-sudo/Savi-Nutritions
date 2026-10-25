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


PRODUCTS = [
    {"name": "Savi Gold Plus", "category": "Finished Goods", "price": 2000, "stock": 2600, "image": "savigold_plus.jpeg"},
    {"name": "Promin Gold", "category": "Finished Goods", "price": 2200, "stock": 1500, "image": "promin_gold.jpeg"},
    {"name": "Promin Plus", "category": "Finished Goods", "price": 2200, "stock": 1750, "image": "promin_plus.jpeg"},
    {"name": "Promin", "category": "Finished Goods", "price": 2200, "stock": 800, "image": "promin.jpeg"},
    {"name": "Savi Gold", "category": "Finished Goods", "price": 1500, "stock": 450, "image": "savi_gold.jpeg"},
    {"name": "Savi Supreme", "category": "Finished Goods", "price": 1350, "stock": 3200, "image": "savi_supreme.jpeg"},
]

PRODUCT_RAW_MATERIALS = {
    "Promin": ["Maize Bran", "Maize DOC", "Calcium LSP", "Copra", "Salt", "Rapeseed", "Rice DDGS", "DCP", "Magnesium sulpate"],
    "Promin Plus": ["Maize", "Maize Bran", "Maize DOC", "Calcium LSP", "Salt", "Rapeseed", "Rice", "DORB", "Wheat Bran", "Rumikon (soda)", "Magnesium sulphate"],
    "Promin Gold": ["Maize", "Maize Bran", "Maize DOC", "Calcium LSP", "Copra", "Salt", "Rapeseed", "Rice", "Rice DDGS", "DCP", "Rumkion (soda)", "Fat", "Magnesium sulpate"],
    "Savi Gold Plus": ["Maize", "Maize Bran", "Maize DOC", "Calcium LSP", "Copra", "Salt", "Rice Polish", "Rapeseed", "Rice", "DORB", "Urea", "Wheat Bran", "Molasses", "Bentonite"],
    "Savi Gold": ["Maize", "Maize Bran", "Maize Doc", "Varagu", "Calcium LSP", "Salt", "Rice polish", "Hullar", "Coffee husk", "Rapeseed", "Rice", "DORB", "Urea", "Molasses", "Bentonite"],
    "Savi Supreme": ["Maize", "Maize Bran", "Maize DOC", "Varagu", "Calcium LSP", "Copra", "Salt", "Rice polish", "Hullar", "Coffee husk", "Rapeseed", "Rice", "DORB", "Urea", "Molasses"],
}

RESOURCE_CATEGORIES = {
    "Raw Materials": [
        ("Maize", "Raw Material"), ("Maize DDGS", "Raw Material"), ("Maize Bran", "Raw Material"),
        ("Maize DOC", "Raw Material"), ("Maize Powder", "Raw Material"), ("Varagu", "Raw Material"),
        ("Copra", "Raw Material"), ("Copra Cake", "Raw Material"), ("Copra Cake (AK)", "Raw Material"),
        ("Rice", "Raw Material"), ("Rice Polish", "Raw Material"), ("Rice DDGS", "Raw Material"),
        ("Salt", "Raw Material"), ("Hullar", "Raw Material"), ("Hullar Polish", "Raw Material"),
        ("Coffee Husk", "Raw Material"), ("Rapeseed", "Raw Material"), ("Ragi Husk", "Raw Material"),
        ("DORB", "Raw Material"), ("Soya", "Raw Material"), ("Cotton DOC", "Raw Material"),
        ("GNC", "Raw Material"), ("GNC DOC", "Raw Material"), ("Wheat Bran", "Raw Material"),
        ("Palm Kernel DOC", "Raw Material"), ("Molasses", "Raw Material"), ("Molasses Plus", "Raw Material"),
        ("Molasses Arom", "Raw Material"), ("Gingelly Cake", "Raw Material"), ("GN Cake", "Raw Material"),
        ("Oil Cake", "Raw Material"), ("Dried Paneer", "Raw Material"), ("Jowari", "Raw Material"),
        ("Mix DDGS", "Raw Material"), ("Stone Grit", "Raw Material"), ("Bentonite", "Raw Material"),
        ("Sugar Cap Cheeni", "Raw Material"), ("Turmeric Spent", "Raw Material"),
        ("Pomegranate Powder", "Raw Material"), ("Liver Powder", "Raw Material"),
    ],
    "Chemicals": [
        ("Calcium LSP", "Chemical"), ("DCP (Dicalcium Phosphate)", "Chemical"), ("Urea", "Chemical"),
        ("Mineral Powder", "Chemical"), ("Magnesium Sulphate", "Chemical"),
        ("Alkarb (Sodium Carbonate)", "Chemical"), ("Sodakarb", "Chemical"), ("Rumikon (Soda)", "Chemical"),
        ("Lysine", "Chemical"), ("Methionine", "Chemical"), ("Poultry Methionine", "Chemical"),
        ("Metipearl", "Chemical"), ("Metipro", "Chemical"), ("Fat", "Chemical"),
        ("Micro Fat", "Chemical"), ("Microfat Booster", "Chemical"), ("Yeast", "Chemical"),
        ("Yea-sacc", "Chemical"),
    ],
    "Medicines": [
        ("Addarome Vanco", "Medicine"), ("Aleta", "Medicine"), ("Aromax", "Medicine"),
        ("Biochal BAD3E", "Medicine"), ("Biotin", "Medicine"), ("Biotin 2%", "Medicine"),
        ("Choline 60", "Medicine"), ("Clostat-365", "Medicine"), ("Daimond V", "Medicine"),
        ("Endox Dry", "Medicine"), ("Fiberzyme", "Medicine"), ("Goudra Shakthi", "Medicine"),
        ("Heptacare (Toxin Binder)", "Medicine"), ("Kemtrace BMX", "Medicine"),
        ("Kemtrace Chromium", "Medicine"), ("Kemtrace Layer Special", "Medicine"),
        ("Kemtrace Maxim", "Medicine"), ("Kemzyme Fibroforte", "Medicine"),
        ("Kemzyme One Advance", "Medicine"), ("Kemzyme One Advance HC Dry", "Medicine"),
        ("Layer Spl", "Medicine"), ("Lexi-vit", "Medicine"), ("Liv 52", "Medicine"),
        ("Mycocurb", "Medicine"), ("Nipro-55", "Medicine"), ("NPS Enzyme", "Medicine"),
        ("PHYTASE", "Medicine"), ("PROTEASE", "Medicine"), ("Ramlax Industries", "Medicine"),
        ("Relimax 9", "Medicine"), ("Selliseo", "Medicine"), ("Toxifin 300", "Medicine"),
        ("Toxifin 360", "Medicine"), ("Zues Enzyme", "Medicine"),
    ],
}

PRODUCTIONS = [
    {"product": "Savi Gold Plus", "date": "2026-09-04", "quantity": 50, "batch": "SGP-2026-0904", "status": "Completed", "notes": "Morning batch"},
    {"product": "Promin Gold", "date": "2026-09-04", "quantity": 35, "batch": "PGL-2026-0904", "status": "Completed", "notes": "Quality checked"},
    {"product": "Promin Plus", "date": "2026-09-04", "quantity": 25, "batch": "PPL-2026-0904", "status": "In Progress", "notes": "Extended mixing"},
    {"product": "Promin", "date": "2026-09-03", "quantity": 60, "batch": "PRM-2026-0903", "status": "Completed", "notes": ""},
    {"product": "Savi Gold", "date": "2026-09-03", "quantity": 20, "batch": "SGD-2026-0903", "status": "Completed", "notes": "Fresh batch"},
    {"product": "Savi Supreme", "date": "2026-09-03", "quantity": 45, "batch": "SSP-2026-0903", "status": "Completed", "notes": ""},
]

PURCHASES = [
    {"vendor_name": "Agro Feed Suppliers", "vendor_phone": "+91 98110 22334", "address": "#12, KIADB, Tumkur\nKarnataka - 572103", "v_number": "VO0125", "date_display": "24 May 2026", "product": "Corn (Makki)", "quantity": "5,000 Kg", "amount_paid": 125000, "remaining": 0, "payment_mode": "Bank Transfer", "status": "Paid"},
    {"vendor_name": "Karnataka Grains & Cereals", "vendor_phone": "+91 98220 33445", "address": "#45, Yeshwanthpur\nBangalore - 560022", "v_number": "VO0124", "date_display": "22 May 2026", "product": "Soyabean Meal", "quantity": "2,500 Kg", "amount_paid": 62500, "remaining": 0, "payment_mode": "UPI", "status": "Paid"},
    {"vendor_name": "Sri Lakshmi Traders", "vendor_phone": "+91 98330 44556", "address": "#8, Peenya Industrial Area\nBangalore - 560058", "v_number": "VO0123", "date_display": "20 May 2026", "product": "Wheat Bran", "quantity": "4,000 Kg", "amount_paid": 72000, "remaining": 0, "payment_mode": "Bank Transfer", "status": "Paid"},
    {"vendor_name": "Shree Ram Oil Seeds", "vendor_phone": "+91 98440 55667", "address": "#16, GIDC, Hubli\nKarnataka - 580021", "v_number": "VO0122", "date_display": "18 May 2026", "product": "Groundnut Cake", "quantity": "1,800 Kg", "amount_paid": 57600, "remaining": 2400, "payment_mode": "Credit", "status": "Partial"},
    {"vendor_name": "Green Field Traders", "vendor_phone": "+91 98550 66778", "address": "#3, Railway Road\nMysore - 570001", "v_number": "VO0121", "date_display": "16 May 2026", "product": "Mustard Cake", "quantity": "1,500 Kg", "amount_paid": 37500, "remaining": 12500, "payment_mode": "Credit", "status": "Partial"},
    {"vendor_name": "Farmers Choice", "vendor_phone": "+91 98660 77889", "address": "#22, Market Yard\nDavanagere - 577004", "v_number": "VO0120", "date_display": "14 May 2026", "product": "Rice Bran", "quantity": "2,000 Kg", "amount_paid": 28000, "remaining": 7000, "payment_mode": "UPI", "status": "Partial"},
    {"vendor_name": "Nutri Ingredients Pvt Ltd", "vendor_phone": "+91 98770 88990", "address": "#11, KIADB, Belgaum\nKarnataka - 590016", "v_number": "VO0119", "date_display": "12 May 2026", "product": "Maize Gluten", "quantity": "1,200 Kg", "amount_paid": 54000, "remaining": 0, "payment_mode": "Bank Transfer", "status": "Paid"},
    {"vendor_name": "Om Sai Traders", "vendor_phone": "+91 98880 99001", "address": "#5, Industrial Area\nShimoga - 577201", "v_number": "VO0118", "date_display": "10 May 2026", "product": "Molasses", "quantity": "800 Ltr", "amount_paid": 16000, "remaining": 0, "payment_mode": "Cash", "status": "Paid"},
    {"vendor_name": "Bhavya Feed Solutions", "vendor_phone": "+91 98990 10112", "address": "#7, Hesaraghatta Road\nBangalore - 560088", "v_number": "VO0117", "date_display": "08 May 2026", "product": "Limestone Powder", "quantity": "1,600 Kg", "amount_paid": 19200, "remaining": 4800, "payment_mode": "Credit", "status": "Partial"},
    {"vendor_name": "Universal Traders", "vendor_phone": "+91 99000 21223", "address": "#19, Old Guntakal Road\nBallari - 583101", "v_number": "VO0116", "date_display": "05 May 2026", "product": "Dicalcium Phosphate", "quantity": "1,200 Kg", "amount_paid": 24000, "remaining": 24000, "payment_mode": "Credit", "status": "Unpaid"},
]

MOVEMENTS = [
    {"date": "05 Sep 2026", "material": "Savi Gold Plus", "movement": "Production In", "quantity": 700, "unit": "unit", "reference": "PROD-0905-01"},
    {"date": "05 Sep 2026", "material": "Savi Gold", "movement": "Production Out", "quantity": 663, "unit": "unit", "reference": "PROD-0905-02"},
    {"date": "04 Sep 2026", "material": "Promin Plus", "movement": "Production In", "quantity": 400, "unit": "unit", "reference": "PROD-0904-01"},
    {"date": "04 Sep 2026", "material": "Promin Gold", "movement": "Production Out", "quantity": 400, "unit": "unit", "reference": "PROD-0904-02"},
    {"date": "03 Sep 2026", "material": "Promin", "movement": "Production In", "quantity": 200, "unit": "unit", "reference": "PROD-0903-01"},
    {"date": "03 Sep 2026", "material": "Savi Supreme", "movement": "Production Out", "quantity": 45, "unit": "unit", "reference": "PROD-0903-02"},
]


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
