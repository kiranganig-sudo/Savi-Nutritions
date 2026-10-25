import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask
from werkzeug.security import generate_password_hash

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin.models import User, db


def bootstrap(database_url, accounts):
    if not database_url.strip():
        raise RuntimeError("A target DATABASE_URL is required.")
    if len(accounts) != 2:
        raise RuntimeError("Exactly two admin accounts are required.")
    for account in accounts:
        if not account["login_id"] or len(account["password"]) < 6 or not account["full_name"]:
            raise RuntimeError("Each admin needs a name, login ID, and password of at least 6 characters.")
    app = Flask("savi_bootstrap")
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        existing = User.query.filter_by(role="admin").count()
        if existing:
            raise RuntimeError(f"Target database already contains {existing} admin account(s).")
        timestamp = datetime.now().strftime("%d %b %Y, %I:%M %p")
        for account in accounts:
            db.session.add(User(
                full_name=account["full_name"],
                login_id=account["login_id"],
                phone="",
                password=generate_password_hash(account["password"]),
                password_plain="",
                shift="",
                role="admin",
                created=timestamp,
            ))
        db.session.commit()
    return len(accounts)


def main():
    parser = argparse.ArgumentParser(description="Create the two initial admin accounts in an empty database.")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--name-1", default=os.environ.get("SAVI_ADMIN_NAME_1", ""))
    parser.add_argument("--login-1", default=os.environ.get("SAVI_ADMIN_LOGIN_1", ""))
    parser.add_argument("--password-1", default=os.environ.get("SAVI_ADMIN_PASSWORD_1", ""))
    parser.add_argument("--name-2", default=os.environ.get("SAVI_ADMIN_NAME_2", ""))
    parser.add_argument("--login-2", default=os.environ.get("SAVI_ADMIN_LOGIN_2", ""))
    parser.add_argument("--password-2", default=os.environ.get("SAVI_ADMIN_PASSWORD_2", ""))
    args = parser.parse_args()
    accounts = [
        {"full_name": args.name_1, "login_id": args.login_1, "password": args.password_1},
        {"full_name": args.name_2, "login_id": args.login_2, "password": args.password_2},
    ]
    count = bootstrap(args.database_url, accounts)
    print(f"Created {count} admin accounts.")


if __name__ == "__main__":
    main()
