import argparse
import os
import sys
from pathlib import Path

from flask import Flask
from sqlalchemy import MetaData, create_engine, func, insert, select, text

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from admin.models import db


def _sqlite_url(path):
    return f"sqlite:///{Path(path).resolve().as_posix()}"


def _canonical_settings(rows):
    if len(rows) <= 1:
        return rows
    canonical = max(
        rows,
        key=lambda row: sum(value not in (None, "") for value in row.values()),
    )
    if "id" in canonical:
        canonical["id"] = 1
    return [canonical]


def _copy_table(source_connection, destination_connection, source_table, target_table):
    common_columns = [
        column_name
        for column_name in source_table.columns.keys()
        if column_name in target_table.columns
    ]
    missing_required = [
        column.name
        for column in target_table.columns
        if column.name not in common_columns
        and not column.nullable
        and column.default is None
        and column.server_default is None
    ]
    if missing_required:
        raise RuntimeError(
            f"{target_table.name} requires columns absent from the source: {', '.join(missing_required)}"
        )
    source_columns = [source_table.c[name] for name in common_columns]
    rows = [dict(row) for row in source_connection.execute(select(*source_columns)).mappings()]
    if target_table.name == "settings":
        rows = _canonical_settings(rows)
    if rows:
        destination_connection.execute(insert(target_table), rows)
    return len(rows)


def migrate(source_path, database_url, allow_sqlite_target=False):
    source_path = Path(source_path).expanduser().resolve()
    if not source_path.is_file():
        raise RuntimeError(f"SQLite source was not found: {source_path}")
    if not database_url.strip():
        raise RuntimeError("A target DATABASE_URL is required.")
    target_engine = create_engine(database_url, pool_pre_ping=True)
    if target_engine.dialect.name == "sqlite" and not allow_sqlite_target:
        raise RuntimeError("Refusing to use SQLite as the production target.")
    if target_engine.dialect.name == "sqlite":
        target_path = Path(target_engine.url.database).expanduser().resolve()
        if target_path == source_path:
            raise RuntimeError("Source and target databases must be different.")

    source_engine = create_engine(_sqlite_url(source_path))
    target_app = Flask("savi_migration")
    target_app.config["SQLALCHEMY_DATABASE_URI"] = database_url
    target_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(target_app)
    copied = {}
    with target_app.app_context():
        db.create_all()
        target_metadata = db.metadata
        with source_engine.connect() as source_connection:
            admins = source_connection.execute(
                text("SELECT COUNT(*) FROM users WHERE role = 'admin'")
            ).scalar() or 0
            if admins != 2:
                raise RuntimeError("The source database must contain exactly two admin accounts.")
            source_metadata = MetaData()
            source_metadata.reflect(bind=source_connection)
            with target_engine.connect() as check_connection:
                for table_name, target_table in target_metadata.tables.items():
                    count = check_connection.execute(select(func.count()).select_from(target_table)).scalar() or 0
                    if count:
                        raise RuntimeError(f"Target database is not empty: {table_name} has {count} rows.")
            with target_engine.begin() as destination_connection:
                mysql = target_engine.dialect.name == "mysql"
                if mysql:
                    destination_connection.execute(text("SET FOREIGN_KEY_CHECKS=0"))
                try:
                    for table_name in sorted(source_metadata.tables):
                        if table_name == "sqlite_sequence":
                            continue
                        if table_name not in target_metadata.tables:
                            raise RuntimeError(f"Target database has no model table: {table_name}")
                        copied[table_name] = _copy_table(
                            source_connection,
                            destination_connection,
                            source_metadata.tables[table_name],
                            target_metadata.tables[table_name],
                        )
                finally:
                    if mysql:
                        destination_connection.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    source_engine.dispose()
    target_engine.dispose()
    return copied


def main():
    parser = argparse.ArgumentParser(description="Copy the local SQLite data into an empty MySQL database.")
    parser.add_argument("--source", default=str(ROOT / "admin" / "savi.db"))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--allow-sqlite-target", action="store_true")
    args = parser.parse_args()
    copied = migrate(args.source, args.database_url, args.allow_sqlite_target)
    for table_name, count in sorted(copied.items()):
        print(f"{table_name}: {count}")
    print(f"Copied {sum(copied.values())} rows into the target database.")


if __name__ == "__main__":
    main()
