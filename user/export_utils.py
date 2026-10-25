"""Backup / export engine: Excel workbook, PDF report, ZIP bundle.

All builders take plain python data (lists of dicts) so routes only need
to query the DB and pass the results in. UI stays untouched.
"""
import io
import json
import zipfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

GREEN = "1F6B4B"
LIGHT_GREEN = "E7F5EA"
THIN_BORDER = Border(
    left=Side(style="thin", color="D8DDD9"),
    right=Side(style="thin", color="D8DDD9"),
    top=Side(style="thin", color="D8DDD9"),
    bottom=Side(style="thin", color="D8DDD9"),
)


def vendor_products_display(v):
    """Pretty-print a vendor's products JSON for exports."""
    raw = str(v.get("products") or "")
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parts = []
            for g, items in parsed.items():
                if items:
                    parts.append(f"{g}: {', '.join(str(p) for p in items)}")
            if parts:
                return " | ".join(parts)
        elif isinstance(parsed, list):
            return ", ".join(str(p) for p in parsed)
    return v.get("product", "")


def _style_sheet(ws, headers, rows):
    header_fill = PatternFill("solid", fgColor=GREEN)
    header_font = Font(bold=True, color="FFFFFF", size=11)
    for col, title in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=title)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER
    for r, row in enumerate(rows, start=2):
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.border = THIN_BORDER
            cell.alignment = Alignment(vertical="center")
            if r % 2 == 0:
                cell.fill = PatternFill("solid", fgColor="F4FAF6")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    widths = {}
    for c, title in enumerate(headers, start=1):
        longest = len(str(title))
        for r in range(2, min(len(rows) + 2, 60)):
            val = ws.cell(row=r, column=c).value
            if val is not None:
                longest = max(longest, len(str(val)))
        widths[c] = min(longest + 4, 42)
    for c, w in widths.items():
        ws.column_dimensions[get_column_letter(c)].width = w


def build_excel_bytes(payload: dict) -> bytes:
    """payload keys: company, generated_at, summary, products, product_materials,
    resources, raw_materials, production, purchases, movements, settings."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = payload.get("company", "SaviNutritions Pvt Ltd")
    ws["A1"].font = Font(bold=True, size=16, color=GREEN)
    ws["A2"] = f"Full backup generated on {payload.get('generated_at', '')}"
    ws["A2"].font = Font(size=11, color="66756C")
    ws["A4"] = "Metric"
    ws["B4"] = "Value"
    for cell in (ws["A4"], ws["B4"]):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=GREEN)
    for i, (k, v) in enumerate(payload.get("summary", {}).items(), start=5):
        ws.cell(row=i, column=1, value=k).border = THIN_BORDER
        ws.cell(row=i, column=2, value=v).border = THIN_BORDER
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 24

    sheets = [
        ("Products", ["ID", "Name", "Category", "Price (Rs)", "Stock", "Status"],
         [[p["id"], p["name"], p["category"], p["price"], p["stock"], p["status"]]
          for p in payload.get("products", [])]),
        ("Product Materials", ["Product", "Material", "Unit", "Opening", "Consumed", "Closing", "Batch", "Status"],
         [[m.get("product_name", ""), m["material"], m["unit"], m["opening"], m["consumed"], m["closing"], m.get("batch", ""), m["status"]]
          for m in payload.get("product_materials", [])]),
        ("Resources", ["Group", "Name", "Category", "Unit", "Opening", "Consumed", "Closing", "Min Stock", "Status", "Last Update"],
         [[r["group"], r["name"], r["item_category"], r["unit"], r["opening"], r["consumed"], r["closing"], r["min_stock"], r["status"], r["last_update"]]
          for r in payload.get("resources", [])]),
        ("Raw Materials", ["ID", "Name", "Unit", "Opening", "Consumed", "Closing", "Min Stock", "Status"],
         [[m["id"], m["name"], m["unit"], m["opening"], m["consumed"], m["closing"], m["min_stock"], m["status"]]
          for m in payload.get("raw_materials", [])]),
        ("Production", ["ID", "Product", "Date", "Qty", "Batch", "Status", "Notes"],
         [[p["id"], p["product"], p["date"], p["quantity"], p["batch"], p["status"], p.get("notes", "")]
          for p in payload.get("production", [])]),
        ("Purchases", ["Vendor", "Phone", "Address", "Record No", "Date", "Product", "Qty", "Paid (Rs)", "Remaining (Rs)", "Mode", "Status"],
         [[p["vendor_name"], p.get("vendor_phone", ""), p["address"], p["v_number"], p["date_display"], p["product"], p["quantity"], p["amount_paid"], p["remaining"], p["payment_mode"], p["status"]]
          for p in payload.get("purchases", [])]),
        ("Stock Movements", ["Date", "Material", "Movement", "Qty", "Unit", "Reference"],
         [[m["date"], m["material"], m["movement"], m["quantity"], m["unit"], m["reference"]]
          for m in payload.get("movements", [])]),
        ("Vendors", ["Vendor", "Phone", "Address", "Date", "Product", "Qty", "Paid (Rs)", "Remaining (Rs)", "Mode", "Status"],
         [[v["vendor_name"], v.get("vendor_phone", ""), v["address"], v["date_display"], vendor_products_display(v), v["quantity"], v["amount_paid"], v["remaining"], v["payment_mode"], v["status"]]
          for v in payload.get("vendors", [])]),
        ("Daily History", ["Date", "Product", "Material", "Opening", "Consumed", "Closing", "Batch"],
         [[d["date"], d["product_name"], d["material"], d["opening"], d["consumed"], d["closing"], d.get("batch", "")]
          for d in payload.get("daily_history", [])]),
    ]
    for title, headers, rows in sheets:
        ws = wb.create_sheet(title=title)
        _style_sheet(ws, headers, rows)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _pdf_table(data, col_widths=None, header_bg=colors.HexColor("#1F6B4B")):
    table = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (0, 0), (-1, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#D8DDD9")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4FAF6")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]
    table.setStyle(TableStyle(style))
    return table


def build_pdf_bytes(payload: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=16 * mm, rightMargin=16 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title="Savi Nutrition Backup Report")
    styles = getSampleStyleSheet()
    title_style = styles["Title"]
    title_style.textColor = colors.HexColor("#1F6B4B")
    heading_style = styles["Heading2"]
    heading_style.textColor = colors.HexColor("#1F6B4B")
    normal = styles["Normal"]

    story = [
        Paragraph("SAVI NUTRITION", title_style),
        Paragraph("Better Nutrition, Better Production", normal),
        Paragraph(f"Company Backup Report &mdash; {payload.get('company', '')}", heading_style),
        Paragraph(f"Generated on {payload.get('generated_at', '')}", normal),
        Spacer(1, 10),
    ]

    summary = payload.get("summary", {})
    story.append(Paragraph("Business Summary", heading_style))
    story.append(_pdf_table(
        [["Metric", "Value"]] + [[k, str(v)] for k, v in summary.items()],
        col_widths=[85 * mm, 85 * mm]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Products & Stock Position", heading_style))
    story.append(_pdf_table(
        [["Product", "Price (Rs)", "Stock", "Status"]] +
        [[p["name"], str(p["price"]), str(p["stock"]), p["status"]]
         for p in payload.get("products", [])],
        col_widths=[70 * mm, 35 * mm, 30 * mm, 35 * mm]))
    story.append(Spacer(1, 12))

    alerts = payload.get("alerts", [])
    story.append(Paragraph(f"Stock Alerts ({len(alerts)})", heading_style))
    if alerts:
        story.append(_pdf_table(
            [["Material", "On Hand", "Minimum", "Status"]] +
            [[x["material"], str(x["on_hand"]), str(x["minimum"]), x["status"]]
             for x in alerts[:25]],
            col_widths=[70 * mm, 35 * mm, 35 * mm, 30 * mm]))
    else:
        story.append(Paragraph("No low or critical stock items. All healthy.", normal))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Recent Production (latest 10)", heading_style))
    story.append(_pdf_table(
        [["Date", "Product", "Qty", "Batch", "Status"]] +
        [[p["date"], p["product"], str(p["quantity"]), p["batch"], p["status"]]
         for p in payload.get("production", [])[:10]],
        col_widths=[30 * mm, 55 * mm, 20 * mm, 40 * mm, 25 * mm]))
    story.append(Spacer(1, 12))

    story.append(Paragraph("Purchases & Outstanding", heading_style))
    story.append(_pdf_table(
        [["Vendor", "Phone", "Product", "Paid (Rs)", "Remaining (Rs)", "Status"]] +
        [[p["vendor_name"][:24], p.get("vendor_phone", ""), p["product"][:20],
          str(p["amount_paid"]), str(p["remaining"]), p["status"]]
         for p in payload.get("purchases", [])],
        col_widths=[45 * mm, 30 * mm, 35 * mm, 25 * mm, 25 * mm, 20 * mm]))
    story.append(Spacer(1, 16))
    story.append(Paragraph(
        "This document was generated automatically by the Savi Nutrition "
        "backup system and contains the complete business position at the time above.",
        normal))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#818883"))
        canvas.drawString(16 * mm, 12 * mm, "SaviNutritions Pvt Ltd - System Backup")
        canvas.drawRightString(A4[0] - 16 * mm, 12 * mm, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def build_backup_zip(payload: dict, stamp: str):
    """Returns (filename, bytes) for the full backup folder (zip)."""
    folder = f"savi_backup_{stamp}"
    excel = build_excel_bytes(payload)
    pdf = build_pdf_bytes(payload)
    data_json = json.dumps({
        "company": payload.get("company", ""),
        "generated_at": payload.get("generated_at", ""),
        "summary": payload.get("summary", {}),
        "products": payload.get("products", []),
        "product_materials": payload.get("product_materials", []),
        "resources": payload.get("resources", []),
        "raw_materials": payload.get("raw_materials", []),
        "production": payload.get("production", []),
        "purchases": payload.get("purchases", []),
        "movements": payload.get("movements", []),
        "vendors": payload.get("vendors", []),
        "daily_history": payload.get("daily_history", []),
        "settings": payload.get("settings", {}),
    }, indent=1, default=str).encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{folder}/savi_backup_{stamp}.xlsx", excel)
        zf.writestr(f"{folder}/savi_backup_{stamp}.pdf", pdf)
        zf.writestr(f"{folder}/data.json", data_json)
        zf.writestr(f"{folder}/README.txt",
                    f"Savi Nutrition backup\nCompany: {payload.get('company', '')}\n"
                    f"Generated: {payload.get('generated_at', '')}\n\n"
                    "Contents:\n"
                    f"- savi_backup_{stamp}.xlsx : complete data in Excel sheets\n"
                    f"- savi_backup_{stamp}.pdf : company information report\n"
                    "- data.json : raw data for safekeeping\n")
    return f"{folder}.zip", buf.getvalue()


def backup_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def backup_label() -> str:
    return datetime.now().strftime("%d %b %Y, %I:%M %p")
