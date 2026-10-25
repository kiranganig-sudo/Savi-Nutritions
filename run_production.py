"""Single-server production launcher for Savi Nutrition.

Starts ONE process on port 5000 that serves both portals:
  Admin portal : http://localhost:5000/
  User portal  : http://localhost:5000/user

Uses Waitress when installed, otherwise the Werkzeug server.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
ADMIN_DIR = ROOT / "admin"
USER_DIR = ROOT / "user"
DB_PATH = ADMIN_DIR / "savi.db"
PORT = int(os.environ.get("PORT", "5000"))


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _write_env_if_missing() -> None:
    if ENV_PATH.exists():
        return
    key = secrets.token_hex(32)
    ENV_PATH.write_text(
        "\n".join(
            [
                "# Generated for local production. Do not commit this file.",
                "# DATABASE_URL is left empty so the existing admin/savi.db is used.",
                "FLASK_ENV=production",
                "FLASK_DEBUG=0",
                f"SECRET_KEY={key}",
                "SAVI_NO_SEED=1",
                "SECURE_COOKIE=0",
                "HOST=0.0.0.0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    print(f"Created {ENV_PATH} with a new SECRET_KEY (SQLite data unchanged).")


def _apply_env() -> None:
    _write_env_if_missing()
    file_values = _parse_env_file(ENV_PATH)
    for key, value in file_values.items():
        os.environ.setdefault(key, value)

    os.environ["FLASK_ENV"] = "production"
    os.environ["FLASK_DEBUG"] = "0"
    os.environ["SAVI_NO_SEED"] = "1"

    if not os.environ.get("SECRET_KEY"):
        key = secrets.token_hex(32)
        os.environ["SECRET_KEY"] = key
        with ENV_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"SECRET_KEY={key}\n")

    # Keep the live SQLite database unless the operator already set DATABASE_URL.
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url.startswith("mysql+pymysql://") and (
        "YOUR_PASSWORD" in db_url or "YOUR_HOST" in db_url
    ):
        os.environ.pop("DATABASE_URL", None)
        print("Ignored placeholder DATABASE_URL from .env.example — using admin/savi.db.")


class _MountPathFix:
    """DispatcherMiddleware passes an empty PATH_INFO for an exact /user hit."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        if not environ.get("PATH_INFO"):
            environ["PATH_INFO"] = "/"
        return self.app(environ, start_response)


def main() -> int:
    if not ADMIN_DIR.is_dir() or not USER_DIR.is_dir():
        print("admin/ and user/ must stay side by side. Aborting.")
        return 1
    if not DB_PATH.exists():
        print(f"Warning: {DB_PATH} was not found. A new empty database may be created.")
    else:
        print(f"Using existing database: {DB_PATH}")

    _apply_env()

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from admin.app import app as admin_app, start_backup_scheduler
    from user.app import app as user_app
    from werkzeug.middleware.dispatcher import DispatcherMiddleware

    start_backup_scheduler()

    application = DispatcherMiddleware(
        admin_app, {"/user": _MountPathFix(user_app)}
    )

    host = os.environ.get("HOST", "0.0.0.0")
    print("Starting production server (demo seed off)...")
    print(f"  Admin panel : http://127.0.0.1:{PORT}/")
    print(f"  User portal : http://127.0.0.1:{PORT}/user")
    print("Close this window or press Ctrl+C to stop the server.")

    try:
        from waitress import serve
    except ImportError:
        serve = None

    if serve is not None:
        serve(application, host=host, port=PORT, threads=8, ident="savi")
    else:
        from werkzeug.serving import run_simple

        print("waitress not installed — using the Werkzeug server.")
        run_simple(host, PORT, application, threaded=True,
                   use_reloader=False, use_debugger=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
