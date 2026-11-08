"""Safe SQLite backup and restore helpers for the bot database.

Commands:
    python -m scripts.database_maintenance backup
    python -m scripts.database_maintenance list
    python -m scripts.database_maintenance restore <backup-file> --confirm-restore

Restore is an offline operation: stop the bot service first. A pre-restore
backup is created automatically and SQLite WAL sidecars are preserved next
to it rather than silently discarded.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config.settings import settings


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _integrity_check(path: Path) -> None:
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise RuntimeError(f"SQLite integrity check failed for {path.name}: {rows[:3]}")


def _copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    _integrity_check(destination)


def backup_database(source: Path, backup_dir: Path, keep: int = 14) -> Path:
    if not source.is_file():
        raise FileNotFoundError(f"Database does not exist: {source}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"shop_bot_{_utc_stamp()}.sqlite3"
    temporary = target.with_suffix(target.suffix + f".{uuid4().hex}.tmp")
    try:
        _copy_database(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    backups = sorted(backup_dir.glob("shop_bot_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max(1, keep):]:
        old.unlink()
    return target


def restore_database(source_backup: Path, target: Path, backup_dir: Path) -> Path:
    if not source_backup.is_file():
        raise FileNotFoundError(f"Backup does not exist: {source_backup}")
    if source_backup.resolve() == target.resolve():
        raise ValueError("A database file cannot be restored onto itself.")
    _integrity_check(source_backup)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    rollback = backup_dir / f"pre_restore_{stamp}.sqlite3"
    temporary = target.with_name(target.name + f".restore-{uuid4().hex}.tmp")
    sidecars = [Path(str(target) + suffix) for suffix in ("-wal", "-shm") ]
    moved_sidecars: list[tuple[Path, Path]] = []
    moved_target = False
    archived_target = target.with_name(target.name + f".pre-restore-{stamp}")
    try:
        if target.exists():
            _copy_database(target, rollback)
        _copy_database(source_backup, temporary)
        if target.exists():
            os.replace(target, archived_target)
            moved_target = True
        for sidecar in sidecars:
            if sidecar.exists():
                preserved = backup_dir / f"{target.name}.pre-restore-{stamp}{sidecar.suffix}"
                os.replace(sidecar, preserved)
                moved_sidecars.append((preserved, sidecar))
        os.replace(temporary, target)
        _integrity_check(target)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        if target.exists():
            failed_target = backup_dir / f"{target.name}.failed-restore-{stamp}"
            os.replace(target, failed_target)
        if moved_target and archived_target.exists():
            os.replace(archived_target, target)
        for preserved, original in reversed(moved_sidecars):
            if preserved.exists() and not original.exists():
                os.replace(preserved, original)
        raise
    return rollback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe SQLite backup/restore")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backup", help="create and verify a consistent SQLite backup")
    sub.add_parser("list", help="list available backups")
    restore = sub.add_parser("restore", help="restore a verified backup (bot must be stopped)")
    restore.add_argument("backup_file", type=Path)
    restore.add_argument("--confirm-restore", action="store_true", help="required safety confirmation")
    args = parser.parse_args(argv)

    database = Path(settings.database_full_path)
    backup_dir = Path(os.environ.get("DATABASE_BACKUP_DIR", str(database.parent / "backups")))
    try:
        if args.command == "backup":
            result = backup_database(database, backup_dir)
            print(f"BACKUP_OK: {result}")
        elif args.command == "list":
            files = sorted(backup_dir.glob("shop_bot_*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
            for item in files:
                print(f"{item.name}\t{item.stat().st_size} bytes")
            print(f"BACKUP_COUNT: {len(files)}")
        else:
            if not args.confirm_restore:
                parser.error("restore requires --confirm-restore after stopping the bot service")
            rollback = restore_database(args.backup_file.resolve(), database, backup_dir)
            print(f"RESTORE_OK: {database}")
            print(f"PRE_RESTORE_BACKUP: {rollback}")
    except Exception as exc:
        print(f"MAINTENANCE_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
