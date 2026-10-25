"""Shared environment loader for the Namecheap (cPanel / Passenger) hosting.

Both subdomains run their own Flask app from its own folder, but they share one
secrets file that lives OUTSIDE the web root:

    ~/savi/config/.env          real values  (never committed to git)
    ~/savi/config/.env.example  template     (safe to commit)

``admin/passenger_wsgi.py`` and ``user/passenger_wsgi.py`` call
:func:`configure` BEFORE importing ``app``, because both Flask apps read
``DATABASE_URL`` / ``SECRET_KEY`` at import time.

Rules
-----
* A real environment variable always wins over the file. cPanel's "Environment
  variables" panel (or a shell ``export``) is therefore still authoritative and
  the loader never overwrites anything that is already set.
* A missing file is not an error - the apps fall back to their local defaults.
* Nothing here is Vercel specific. On a normal desktop run this module is not
  imported at all (``run_production.py`` handles the local ``.env``).
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent

#: Environment variables the apps need in production.
DEFAULT_VALUES = {
    "FLASK_ENV": "production",
    "FLASK_DEBUG": "0",
    "SAVI_NO_SEED": "1",
    "SECURE_COOKIE": "1",
}


def _candidate_files(app_root: Path | None) -> list[Path]:
    """Files to try, in order. The first existing one wins per key."""
    candidates: list[Path] = []
    explicit = os.environ.get("SAVI_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if app_root is not None:
        # ~/savi/admin  ->  ~/savi/config/.env
        candidates.append(Path(app_root).resolve().parent / "config" / ".env")
        candidates.append(Path(app_root).resolve().parent / ".env")
    candidates.append(APP_ROOT / "config" / ".env")
    candidates.append(APP_ROOT / ".env")
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in candidates:
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE file (comments and blank lines ignored)."""
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(app_root: Path | None = None) -> dict[str, str]:
    """Load the first readable env file and apply it with ``setdefault``."""
    applied: dict[str, str] = {}
    for path in _candidate_files(app_root):
        if not path.is_file():
            continue
        for key, value in parse_env_file(path).items():
            if key in os.environ:
                continue
            os.environ[key] = value
            applied[key] = value
        break
    return applied


def ensure_directories(app_root: Path | None = None) -> None:
    """Create the shared uploads folder (used by both subdomains)."""
    root = Path(app_root).resolve().parent if app_root else APP_ROOT
    upload_dir = Path(os.environ.get("UPLOAD_DIR", "").strip() or (root / "uploads"))
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def configure(app_root: Path | None = None) -> dict[str, str]:
    """Load config/.env, then apply production defaults where nothing is set."""
    applied = load_env(app_root)
    for key, value in DEFAULT_VALUES.items():
        os.environ.setdefault(key, value)
    if app_root is not None:
        shared_uploads = Path(app_root).resolve().parent / "uploads"
        os.environ.setdefault("UPLOAD_DIR", str(shared_uploads))
        # Daily backups belong next to the app, not in the hosting account's
        # home directory. backup_service.default_backup_dir() reads this.
        os.environ.setdefault("SAVI_BACKUP_DIR", str(Path(app_root).resolve().parent / "backups"))
    ensure_directories(app_root)
    return applied
