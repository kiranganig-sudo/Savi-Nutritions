# PENDING — do only when the user says so

## NOTE 1 — Formula-update popup in user panel (ON HOLD)
When the admin updates a formula, the user panel must show a popup.
Status: NOT IMPLEMENTED. Build it only after the user side is received.

## NOTE 2 — User side integration (WAITING FOR USER SIDE)
The user will send the USER SIDE after finishing the admin panel.
When it arrives:
1. Attach the user side AND this admin side to the same Flask server.
2. Make them interact — updates from ADMIN must be visible on the USER side
   (formulas, products, stock/consume changes).
3. Enforce roles: a normal user can edit ONLY consume values; everything
   else is admin-only. Admin keeps full edit rights.
   - User-editable surfaces (wire these to the user role): inventory
     Consume button (POST /api/product-materials/<id>/consume), Add
     Consumed bulk editor (PUT /api/product-materials/bulk), and the
     Production Old/New Machine row editor (PUT /api/production/<id>
     with batch+consumed only).
   - Everything else (add/delete products & materials, uploads, vendors,
     formulas, settings, backup) stays admin-only.
4. Consumption log already records an `actor` field ("Admin" by default) —
   the user side must send `actor: "User"` (or the logged-in name) on
   consume calls so the admin tracking tab shows who consumed what.

## NOTE 4 — Maintenance mode hookup to user side (WAITING FOR USER SIDE)
A Maintenance Mode toggle exists in Settings (Setting.maintenance_mode).
When ON, the user panel must stop working:
- User side: check `GET /api/maintenance-status` → returns
  {maintenance_mode: true|false}. When true, show the maintenance page
  (serve `GET /maintenance`) and block ALL user actions (consume, forms,
  navigation to inventory, etc.). Re-check periodically or on each action.
- Admin panel keeps full access; only the USER side is paused.
- To turn off, admin unchecks the toggle in Settings and saves.

## NOTE 3 — Admin-side edit buttons (hidden, kept ready)
Edit buttons were removed from the inventory list + product detail tables
because edit is admin-only. The PUT APIs and the hidden edit modals in
`finished_goods.html` / `product_materials.html` are intact — to restore,
re-add the pencil buttons calling `openEditProduct(...)` / `openEditPm(...)`.
