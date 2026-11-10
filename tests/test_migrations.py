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


def test_migration_upgrades_legacy_db():
    """旧库（缺use_count列）经迁移后补齐"""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "legacy.db")

    # 模拟v1旧库：无use_count列
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE cookie_account (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "uid INTEGER UNIQUE, nickname TEXT DEFAULT '', cookie_string TEXT, "
        "sessdata TEXT, bili_jct TEXT, ac_time_value TEXT, status TEXT DEFAULT 'active')"
    )
    conn.execute("INSERT INTO cookie_account (uid, nickname, cookie_string) VALUES (1, 'A', 'S=x')")
    conn.commit()
    conn.close()

    settings = Settings()
    settings.database.sqlite_path = db_path

    async def run():
        db = DatabaseManager(settings.database)
        await db.initialize()
        return db

    db = asyncio.run(run())
    try:
        # use_count列已补齐，数据保留
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(cookie_account)")}
        rows = list(conn.execute("SELECT uid, use_count FROM cookie_account"))
        conn.close()
        assert "use_count" in cols
        assert rows == [(1, 0)]
    finally:
        asyncio.run(db.close())


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


def test_migration_upgrades_legacy_friend_table():
    """旧friend表（无union_openid列）经迁移补齐，且新库迁移跳过"""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "legacy.db")

    # 模拟v2旧库：friend表无union_openid列
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE friend (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "openid TEXT UNIQUE, nickname TEXT DEFAULT '', del_flag INTEGER DEFAULT 0, "
        "msg_receive INTEGER DEFAULT 1)"
    )
    conn.execute("INSERT INTO friend (openid, nickname) VALUES ('O1', '好友甲')")
    conn.commit()
    conn.close()

    settings = Settings()
    settings.database.sqlite_path = db_path

    async def run():
        db = DatabaseManager(settings.database)
        await db.initialize()
        svc = DatabaseService(db)
        friends = await svc.load_friends()
        return db, friends

    db, friends = asyncio.run(run())
    try:
        conn = sqlite3.connect(db_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(friend)")}
        rows = list(conn.execute("SELECT openid, nickname, union_openid FROM friend"))
        conn.close()
        assert "union_openid" in cols
        assert rows == [("O1", "好友甲", None)]
    finally:
        asyncio.run(db.close())


def test_migration_v5_monitored_boolean_to_integer():
    """旧group_binding表（monitored BOOLEAN）经v5重建为INTEGER，数据保留"""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "legacy.db")

    # 模拟旧库：monitored为BOOLEAN声明
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE group_binding (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "group_openid VARCHAR(64) NOT NULL, room_id BIGINT NOT NULL, "
        "uid BIGINT NOT NULL DEFAULT 0, monitored BOOLEAN NOT NULL DEFAULT 0, "
        "created_at TIMESTAMP NOT NULL, "
        "CONSTRAINT uq_binding_group_room UNIQUE (group_openid, room_id))"
    )
    conn.execute(
        "INSERT INTO group_binding (group_openid, room_id, uid, monitored, created_at) "
        "VALUES ('G1', 100, 9, 1, datetime('now'))")
    conn.commit()
    conn.close()

    settings = Settings()
    settings.database.sqlite_path = db_path

    async def run():
        db = DatabaseManager(settings.database)
        await db.initialize()
        svc = DatabaseService(db)
        # 迁移后绑定关系可用：更新监控状态走INTEGER列
        await svc.set_binding_monitored("G1", 100, False)
        return db

    db = asyncio.run(run())
    try:
        conn = sqlite3.connect(db_path)
        info = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(group_binding)")}
        rows = list(conn.execute(
            "SELECT group_openid, room_id, monitored FROM group_binding"))
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(group_binding)")}
        conn.close()

        assert info["monitored"] == "INTEGER"  # 类型已更正
        assert rows == [("G1", 100, 0)]  # 数据保留且set_monitored(False)生效
        assert any("group_binding" in ix for ix in indexes)  # 索引已重建
    finally:
        db.close()


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
