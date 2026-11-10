# -*- coding: utf-8 -*-
"""Schema迁移器测试：旧库升级 + 新库幂等跳过"""

import asyncio
import os
import sqlite3
import tempfile

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
    """旧库v2~v6版本号被重置为v1；新库初始化v1；后续迁移从v2起"""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from blivepost.database.migrations import run_migrations

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    try:
        # 新库：初始化v1
        n = await run_migrations(engine)
        assert n == 0  # 无迁移可应用
        async with engine.begin() as conn:
            v = (await conn.execute(
                text("SELECT MAX(version) FROM schema_version"))).scalar()
        assert v == 1

        # 模拟旧库：写入v6版本号 → 重置为v1
        async with engine.begin() as conn:
            await conn.execute(text(
                "INSERT OR REPLACE INTO schema_version(version) VALUES (6)"))
        await run_migrations(engine)
        async with engine.begin() as conn:
            v = (await conn.execute(
                text("SELECT MAX(version) FROM schema_version"))).scalar()
        assert v == 1
    finally:
        await engine.dispose()
