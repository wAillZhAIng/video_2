"""Clear a stopped Video Factory installation while keeping model configuration."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path


KEEP_TABLES = frozenset({
    "ai_services", "alembic_version", "generation_workflows", "local_models",
    "model_directories", "model_lab_defaults",
})
SYSTEM_SCOPED_TABLES = frozenset({"quality_profiles", "route_policies"})
CONFIG_PATH_COLUMNS = {
    "ai_services": ("settings_json", "last_test_json", "capabilities_json"),
    "generation_workflows": ("source_url",),
    "model_directories": ("path",),
    "local_models": ("path", "workflow_path", "verification_json"),
    "model_lab_defaults": ("configuration_json",),
    "route_policies": ("settings_json",),
}


def quoted_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def absolute(root, path):
    return Path(os.path.abspath(root / path))


def is_link(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def checked(root, path):
    path = absolute(root, path)
    if path == root or not path.is_relative_to(root):
        raise ValueError("Cleanup path is outside this application.")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.exists() or ancestor.is_symlink():
            if is_link(ancestor):
                raise ValueError("A configured cleanup path crosses a symlink or junction.")
    return path


def strings(value):
    if isinstance(value, str):
        if value.startswith(("{", "[")):
            try:
                yield from strings(json.loads(value))
                return
            except (ValueError, RecursionError):
                pass
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def config_files(connection, tables, root, media, targets):
    """Retain any actual model or workflow paths inside the business-file roots."""
    retained = set()
    for table, columns in CONFIG_PATH_COLUMNS.items():
        if table not in tables:
            continue
        present = {col[1] for col in connection.execute(f"PRAGMA table_info({quoted_identifier(table)})")}
        fields = ",".join(quoted_identifier(column) for column in columns if column in present)
        if not fields:
            continue
        condition = " WHERE scope = 'system'" if table in SYSTEM_SCOPED_TABLES else ""
        for row in connection.execute(f"SELECT {fields} FROM {quoted_identifier(table)}" + condition):
            for value in strings(list(row)):
                if not value or "\x00" in value or "://" in value:
                    continue
                path = Path(value)
                # Existing file settings can be absolute, application-relative,
                # or a media-relative workflow path from an older version.
                for base in (root, media):
                    candidate = absolute(base, path)
                    if not any(candidate.is_relative_to(target) for target in targets):
                        continue
                    try:
                        if candidate.exists() or candidate.is_symlink():
                            retained.add(candidate)
                    except OSError:
                        raise ValueError("Could not verify a path referenced by model configuration.") from None
    return retained


def clear_tree(path, protected):
    if path in protected:
        return
    if is_link(path):
        if os.name == "nt" and path.is_dir() and not path.is_symlink():
            path.rmdir()  # Remove only the junction, never its target.
        else:
            path.unlink()
    elif path.is_dir():
        if os.path.ismount(path):
            raise ValueError("A nested mount was found inside the business-file directory.")
        for item in path.iterdir():
            clear_tree(item, protected)
        if not any(path.iterdir()):
            path.rmdir()
    else:
        path.unlink()


def reset(root, database="data/video_factory.db", media="data/media", *, runtime_targets=(), retained_files=()):
    root = Path(root).resolve(strict=True)
    data = root / "data"
    database, media = checked(root, database), checked(root, media)
    if not database.is_relative_to(data) or database == data or not media.is_relative_to(data) or media == data:
        raise ValueError("Expected this application's SQLite database and media under its data directory.")
    if database.is_relative_to(media):
        raise ValueError("The media directory overlaps the SQLite database.")
    targets = tuple(dict.fromkeys(
        checked(root, path) for path in (media, database.parent / "schema_backups", data / "tmp", data / "imports",
                                          root / "logs", root / "reports", *runtime_targets)
    ))
    identity_file = os.getenv("VIDEO_FACTORY_SECRET_KEY_FILE")
    protected = {data / ".secrets", database}
    if identity_file:
        protected.add(absolute(root, identity_file))
    if any(location.is_relative_to(target) for location in protected for target in targets):
        raise ValueError("A cleanup directory contains the database or credential store.")
    if any(target.exists() and not target.is_dir() for target in targets):
        raise ValueError("An expected cleanup directory is not a directory.")

    cleared = 0
    if database.exists():
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("PRAGMA defer_foreign_keys = ON")
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )}
            if not {"projects", "ai_services"}.issubset(tables):
                raise ValueError("The SQLite file is not an initialized Video Factory database.")
            protected.update(config_files(connection, tables, root, media, targets))
            for table in sorted(tables - KEEP_TABLES):
                condition = " WHERE scope IS NULL OR scope != 'system'" if table in SYSTEM_SCOPED_TABLES else ""
                cleared += connection.execute("DELETE FROM " + quoted_identifier(table) + condition).rowcount
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Model configuration references business data. No database changes were committed.")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    protected.update(absolute(root, path) for path in retained_files)
    for target in targets:
        if target.exists() and target not in protected:
            for child in target.iterdir():
                clear_tree(child, protected)
    return cleared


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/video_factory.db")
    parser.add_argument("--media", default="data/media")
    args = parser.parse_args()
    try:
        if __file__ == "<stdin>":
            if os.getenv("VIDEO_FACTORY_CONTAINER") != "1":
                raise ValueError("Piped cleanup is available only in the application container.")
            root = Path("/app")
        else:
            root = Path(__file__).resolve().parents[1]
        deleted = reset(root, args.database, args.media)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"Business-data cleanup incomplete: {error}", file=sys.stderr)
        return 1
    print(f"Removed {deleted} business records and their files. Model configuration and credentials retained.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
