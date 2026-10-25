"""Package the project into a clean, portable ZIP for export/distribution.

Run:  python make_export.py
Output: savi_nutrition_export_<timestamp>.zip  (placed next to this file)
Excludes caches, virtualenvs, the runtime DB, backups and temporary files.
"""
import os
import re
import time
import zipfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = BASE_DIR

EXCLUDE_DIRS = {"__pycache__", ".venv", "venv", "env", "temp_web_env",
                ".git", "instance", ".pytest_cache", ".ruff_cache",
                ".mypy_cache", "htmlcov", ".idea", ".vscode"}
EXCLUDE_FILES = {"savi.db", "savi.db-journal", "savi.db-wal", "savi.db-shm",
                 "*.pyc", "*.log", ".DS_Store", "Thumbs.db"}
EXCLUDE_PATTERNS = [r"^.*\.db$", r"^.*\.sqlite$", r"^savi_nutrition_export_.*\.zip$"]


def excluded(name, is_dir):
    if is_dir and name in EXCLUDE_DIRS:
        return True
    if not is_dir:
        if name in EXCLUDE_FILES:
            return True
        for pat in EXCLUDE_PATTERNS:
            if re.match(pat, name):
                return True
    return False


def main():
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(OUT_DIR, f"savi_nutrition_export_{stamp}.zip")
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(BASE_DIR):
            dirs[:] = [d for d in dirs if not excluded(d, True)]
            rel_root = os.path.relpath(root, BASE_DIR)
            for fname in sorted(files):
                if excluded(fname, False):
                    continue
                fpath = os.path.join(root, fname)
                arc = os.path.join(rel_root, fname) if rel_root != "." else fname
                zf.write(fpath, arc)
                count += 1
    print(f"Created {out_path}")
    print(f"Included {count} files (DB, caches and backups excluded).")
    print("Transfer the ZIP to run a fresh, clean copy of the app.")


if __name__ == "__main__":
    main()