"""Daily backup service: saves Excel + PDF + JSON + ZIP locally and copies
them into a Google Drive synced folder.

The scheduler fires at 12:00 AM and 6:00 PM (local computer time) plus one
catch-up run when the desktop app opens (once per day). Google Drive support
uses the folder synced by "Google Drive for Desktop" - no API keys needed.
"""
import json
import os
import re
import shutil
import threading
import time
from datetime import date, datetime, timedelta

try:
    from .export_utils import build_backup_zip, build_excel_bytes, build_pdf_bytes
except ImportError:  # flat import (python app.py from the admin folder)
    from export_utils import build_backup_zip, build_excel_bytes, build_pdf_bytes

SCHEDULE_SLOTS = ((0, 0), (18, 0))  # 12:00 AM and 6:00 PM
DEFAULT_KEEP_DAYS = 30
_DATE_FOLDER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_scheduler_guard = threading.Lock()
_scheduler_started = False


def _expand_path(path):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def default_backup_dir():
    """Default local backup folder (survives app updates)."""
    override = os.environ.get("SAVI_BACKUP_DIR", "").strip()
    if override:
        return _expand_path(override)
    if os.name == "nt":
        return _expand_path(os.path.join(os.path.expanduser("~"), "SaviNutrition", "Backups"))
    return _expand_path(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "backups"))


def resolve_backup_dir(configured):
    text = (configured or "").strip()
    return _expand_path(text) if text else default_backup_dir()


def _payload_json(payload):
    return json.dumps(payload, indent=1, default=str).encode("utf-8")


def write_daily_backup(payload, local_dir, slot="startup", keep_days=DEFAULT_KEEP_DAYS):
    """Write the daily Excel, PDF, JSON and ZIP files into <local_dir>/<date>/."""
    now = datetime.now()
    base_dir = resolve_backup_dir(local_dir)
    day_dir = os.path.join(base_dir, now.strftime("%Y-%m-%d"))
    os.makedirs(day_dir, exist_ok=True)

    stamp = now.strftime("%Y%m%d_%H%M%S")
    prefix = "savi_" + stamp

    excel = build_excel_bytes(payload)
    pdf = build_pdf_bytes(payload)
    raw_json = _payload_json(payload)
    zip_name, zip_bytes = build_backup_zip(payload, stamp)

    files = {
        "excel": os.path.join(day_dir, prefix + ".xlsx"),
        "pdf": os.path.join(day_dir, prefix + ".pdf"),
        "json": os.path.join(day_dir, prefix + ".json"),
        "zip": os.path.join(day_dir, zip_name),
    }
    for key, data in (("excel", excel), ("pdf", pdf), ("json", raw_json), ("zip", zip_bytes)):
        with open(files[key], "wb") as handle:
            handle.write(data)

    removed = prune_old_backups(base_dir, keep_days)
    return {
        "folder": day_dir,
        "stamp": stamp,
        "slot": slot,
        "files": files,
        "pruned": removed,
    }


def copy_to_drive(backup_result, drive_dir):
    """Copy the daily files into the Google Drive synced folder and verify."""
    source_dir = backup_result["folder"]
    dest_root = _expand_path(drive_dir)
    dest_dir = os.path.join(dest_root, os.path.basename(source_dir))
    os.makedirs(dest_dir, exist_ok=True)

    copied = []
    failed = []
    for path in backup_result["files"].values():
        name = os.path.basename(path)
        target = os.path.join(dest_dir, name)
        try:
            shutil.copy2(path, target)
            if os.path.isfile(target) and os.path.getsize(target) == os.path.getsize(path):
                copied.append(target)
            else:
                failed.append(name)
        except Exception:
            failed.append(name)

    verified = not failed and len(copied) == len(backup_result["files"])
    marker = os.path.join(dest_dir, "upload-ok.txt")
    if verified:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(
                "Savi Nutrition backup verified\n"
                "Copied: %s\n"
                "Files: %d\n"
                "Google Drive for Desktop will upload this folder automatically.\n"
                % (datetime.now().strftime("%d %b %Y, %I:%M %p"), len(copied))
            )
    return {
        "folder": dest_dir,
        "copied": copied,
        "failed": failed,
        "verified": verified,
    }


def check_drive_folder(drive_dir):
    """6 PM safety check: confirm the Drive folder exists and is writable."""
    path = _expand_path(drive_dir)
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".savi-drive-check")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write(datetime.now().isoformat())
        os.remove(probe)
        return {"ok": True, "path": path, "message": "Google Drive folder is reachable."}
    except Exception as exc:
        return {"ok": False, "path": path, "message": "Google Drive folder is not reachable: %s" % exc}


def prune_old_backups(base_dir, keep_days):
    """Delete dated backup folders older than keep_days."""
    try:
        days = int(keep_days)
    except (TypeError, ValueError):
        days = DEFAULT_KEEP_DAYS
    days = max(1, min(days, 3650))
    cutoff = date.today() - timedelta(days=days)
    removed = 0
    base_dir = resolve_backup_dir(base_dir)
    if not os.path.isdir(base_dir):
        return 0
    for name in os.listdir(base_dir):
        if not _DATE_FOLDER_RE.match(name):
            continue
        try:
            folder_day = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue
        if folder_day < cutoff:
            try:
                shutil.rmtree(os.path.join(base_dir, name))
                removed += 1
            except Exception:
                pass
    return removed


def start_scheduler(runner, poll_seconds=20, startup_delay=8):
    """Start the daemon scheduler once per process.

    `runner(slot)` performs the backup. slot is 'startup', '00:00' or '18:00'.
    """
    global _scheduler_started
    if os.environ.get("SAVI_DISABLE_BACKUP_SCHEDULER") == "1":
        return False
    with _scheduler_guard:
        if _scheduler_started:
            return False
        _scheduler_started = True

    def _loop():
        time.sleep(startup_delay)
        try:
            runner("startup")
        except Exception:
            pass
        last_fired = ""
        while True:
            try:
                now = datetime.now()
                for hour, minute in SCHEDULE_SLOTS:
                    if now.hour == hour and now.minute == minute:
                        key = now.strftime("%Y-%m-%d %H:%M")
                        if key != last_fired:
                            last_fired = key
                            runner("%02d:%02d" % (hour, minute))
            except Exception:
                pass
            time.sleep(max(5, int(poll_seconds)))

    thread = threading.Thread(target=_loop, name="savi-daily-backup", daemon=True)
    thread.start()
    return True
