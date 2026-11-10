# -*- coding: utf-8 -*-
"""Schema迁移器测试：旧库升级 + 新库幂等跳过 + v7时间列毫秒化"""

import asyncio
import os
import sqlite3
import tempfile
from datetime import datetime

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.database.migrations import MIGRATIONS


def test_migration_registry_valid():
    """迁移注册表版本递增、DDL为ADD COLUMN形式"""
    versions = [v for v, *_ in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    for _, _, table, ddls in MIGRATIONS:
        for ddl in ddls:
            assert "ADD COLUMN" in ddl and table in ddl


def test_migration_skips_fresh_db():
    """新库（create_all已含全列）迁移自动跳过不报错"""
    tmp = tempfile.mkdtemp()
    settings = Settings()
    settings.database.sqlite_path = os.path.join(tmp, "fresh.db")

    async def run():
        db = DatabaseManager(settings.database)
        await db.initialize()
        svc = DatabaseService(db)
        await svc.save_cookie_account(uid=9, nickname="N", cookie_string="c")
        return db, svc

    db, svc = asyncio.run(run())
    try:
        accounts = asyncio.run(svc.get_cookie_accounts())
        assert len(accounts) == 1
    finally:
        asyncio.run(db.close())


def test_table_columns_information_schema_filters_current_db():
    """列存在性探测限定当前库/schema：多库同名表不再让本库缺列被误判为已存在"""
    from blivepost.database.migrations import _table_columns

    class _FakeResult(list):
        pass

    class _FakeConn:
        def __init__(self):
            self.sql = None
            self.params = None

        async def execute(self, sql, params=None):
            self.sql = str(sql)
            self.params = params
            return _FakeResult()

    conn = _FakeConn()
    asyncio.run(_table_columns(conn, "mysql", "cookie_account"))
    assert "table_name = :table" in conn.sql
    assert "table_schema = DATABASE()" in conn.sql
    assert conn.params == {"table": "cookie_account"}

    asyncio.run(_table_columns(conn, "postgresql", "friend"))
    assert "table_name = :table" in conn.sql
    assert "table_schema = current_schema()" in conn.sql
    assert conn.params == {"table": "friend"}

    # sqlite路径走PRAGMA，不受本修复影响（用真库冒烟：建表后列名可探出）
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "cols.db")
    sconn = sqlite3.connect(db_path)
    sconn.execute("CREATE TABLE cookie_account (id INTEGER PRIMARY KEY, uid INTEGER)")
    sconn.commit()
    sconn.close()

    async def probe():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.connect() as conn_:
            cols = await _table_columns(conn_, "sqlite", "cookie_account")
        await engine.dispose()
        return cols

    assert asyncio.run(probe()) == {"id", "uid"}


async def test_schema_version_reset_to_v1(tmp_path):
    """旧库v2~v6版本号被清退；新库初始化v1并应用v7；v7不被v1归一误删"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from blivepost.database.migrations import run_migrations

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    try:
        # 新库：初始化v1，应用v7（毫秒时间戳迁移）
        n = await run_migrations(engine)
        assert n == 1
        async with engine.begin() as conn:
            v = (await conn.execute(
                text("SELECT MAX(version) FROM schema_version"))).scalar()
        assert v == 7

        # 已应用的v7不重复执行
        assert await run_migrations(engine) == 0

        # 模拟旧库：写入v6版本号 → 清退为仅v1+v7（v7保留，不重复应用）
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT OR REPLACE INTO schema_version(version) VALUES (6)"))
        await run_migrations(engine)
        async with engine.begin() as conn:
            versions = [r[0] for r in (await conn.execute(
                text("SELECT version FROM schema_version"))).all()]
        assert versions == [1, 7]
    finally:
        await engine.dispose()


async def test_v7_time_columns_to_ms(tmp_path):
    """v7数据迁移：主库+房间/动态子库的text时间值转为13位毫秒（幂等）"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from blivepost.database.migrations import run_migrations

    main_db = tmp_path / "legacy.db"
    sconn = sqlite3.connect(main_db)
    # 模拟旧主库：DATETIME列存text时间
    sconn.execute(
        "CREATE TABLE cookie_account (id INTEGER PRIMARY KEY, uid INTEGER, "
        "created_at DATETIME, updated_at DATETIME)")
    sconn.execute(
        "INSERT INTO cookie_account (uid, created_at, updated_at) VALUES "
        "(1, '2026-11-01 08:30:00.000000', '2026-11-02 09:00:00.500000')")
    # 模拟旧房间子库与动态子库（含TEXT声明列的created_at）
    (tmp_path / "rooms" / "legacy").mkdir(parents=True)
    rconn = sqlite3.connect(tmp_path / "rooms" / "legacy" / "room_9.db")
    rconn.execute(
        "CREATE TABLE danmaku (id INTEGER PRIMARY KEY, room_id INTEGER, "
        "timestamp DATETIME)")
    rconn.execute(
        "INSERT INTO danmaku (room_id, timestamp) VALUES (9, '2026-11-01 12:00:00.000000')")
    rconn.commit()
    rconn.close()
    (tmp_path / "dynamics").mkdir()
    dconn = sqlite3.connect(tmp_path / "dynamics" / "up_7.db")
    dconn.execute(
        "CREATE TABLE dynamic_seen (dynamic_id TEXT PRIMARY KEY, up_uid INTEGER, "
        "created_at TEXT)")
    dconn.execute(
        "INSERT INTO dynamic_seen VALUES ('d1', 7, '2026-11-01T12:00:00')")
    dconn.commit()
    dconn.close()
    sconn.commit()
    sconn.close()

    engine = create_async_engine(f"sqlite+aiosqlite:///{main_db}")
    try:
        await run_migrations(engine)  # v7应用于主库+子库
        async with engine.begin() as conn:
            v = (await conn.execute(
                text("SELECT MAX(version) FROM schema_version"))).scalar()
        assert v == 7

        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "SELECT created_at, updated_at FROM cookie_account"))).one()
        # strftime('%s')为秒 → ×1000（毫秒）
        assert row[0] == int(datetime(2026, 11, 1, 8, 30).timestamp()) * 1000
        assert row[1] == int(datetime(2026, 11, 2, 9, 0).timestamp()) * 1000
    finally:
        await engine.dispose()

    # 子库值已转换（毫秒整数），且重跑安全（幂等不二次×1000）
    rconn = sqlite3.connect(tmp_path / "rooms" / "legacy" / "room_9.db")
    ts = rconn.execute("SELECT timestamp FROM danmaku").fetchone()[0]
    rconn.close()
    assert ts == int(datetime(2026, 11, 1, 12, 0).timestamp()) * 1000
    dconn = sqlite3.connect(tmp_path / "dynamics" / "up_7.db")
    created = dconn.execute("SELECT created_at FROM dynamic_seen").fetchone()[0]
    dconn.close()
    # dynamic_seen.created_at为TEXT亲和列：数值正确（数字串形式存储）
    assert int(created) == int(datetime(2026, 11, 1, 12, 0).timestamp()) * 1000

    # 重跑迁移：已是integer的行跳过，值不变
    engine = create_async_engine(f"sqlite+aiosqlite:///{main_db}")
    try:
        from blivepost.database.migrations import _migrate_time_columns_to_ms
        async with engine.begin() as conn:
            await _migrate_time_columns_to_ms(conn, "sqlite")
        async with engine.begin() as conn:
            row = (await conn.execute(text(
                "SELECT created_at FROM cookie_account"))).one()
        assert row[0] == int(datetime(2026, 11, 1, 8, 30).timestamp()) * 1000
    finally:
        await engine.dispose()
