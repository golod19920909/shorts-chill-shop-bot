import sqlite3

from scripts.database_maintenance import backup_database, restore_database


def _make_db(path, value):
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE sample (value TEXT NOT NULL)")
        conn.execute("INSERT INTO sample(value) VALUES (?)", (value,))


def _read_value(path):
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT value FROM sample").fetchone()[0]


def test_backup_is_a_verified_consistent_sqlite_copy(tmp_path):
    database = tmp_path / "shop.db"
    _make_db(database, "original")
    backup = backup_database(database, tmp_path / "backups")
    assert backup.is_file()
    assert _read_value(backup) == "original"


def test_restore_keeps_a_pre_restore_copy(tmp_path):
    database = tmp_path / "shop.db"
    _make_db(database, "current")
    backup_dir = tmp_path / "backups"
    restore_source = backup_dir / "source.sqlite3"
    restore_source.parent.mkdir(parents=True)
    _make_db(restore_source, "restored")
    rollback = restore_database(restore_source, database, backup_dir)
    assert _read_value(database) == "restored"
    assert _read_value(rollback) == "current"
