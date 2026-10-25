"""Run one SAVI Nutrition backup right now.

Designed for a cPanel cron job on the Namecheap server, where the web process
may be idle/recycled at the scheduled time:

    /home/USER/virtualenv/savi/admin/3.11/bin/python \\
        /home/USER/savi/admin/backup_once.py

The slot argument is free-form ('manual' by default). The automatic 12:00 AM /
6:00 PM scheduler inside the admin app keeps working as before; this script is
only a reliable fallback for shared hosting.
"""
from __future__ import annotations

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT_DIR = APP_DIR.parent

for _extra in (str(ROOT_DIR), str(APP_DIR)):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

import deploy_env  # noqa: E402

deploy_env.configure(app_root=APP_DIR)

from app import app, _run_daily_backup  # noqa: E402


def main(argv: list[str]) -> int:
    slot = (argv[1] if len(argv) > 1 else "manual").strip() or "manual"
    try:
        with app.app_context():
            result = _run_daily_backup(slot)
    except Exception as exc:  # pragma: no cover - reported to cron mail
        print("Backup failed: %s" % exc)
        return 1
    if result.get("skipped"):
        print("Skipped: %s" % result.get("message", ""))
        return 0
    print(result.get("message") or "Backup saved.")
    local = result.get("local") or {}
    if local.get("folder"):
        print("Folder: %s" % local["folder"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
