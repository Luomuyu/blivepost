# -*- coding: utf-8 -*-
"""
轻量Schema迁移器

schema_version表记录已应用版本，按版本号升序执行增量变更。两类迁移：
  - MIGRATIONS：ADD COLUMN形式DDL列表，执行前按列名探测跳过已存在的列
    （新库由create_all建出完整结构，迁移自动跳过）
  - MIGRATION_HANDLERS：函数型迁移（改列类型/重建表等DDL机制覆盖不了的
    变更），接收(conn, db_type)自行处理
跨SQLite/MySQL/PostgreSQL可用。
"""

from typing import Any, Dict, List, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..utils.logger import get_logger


logger = get_logger("migrations")

# (版本号, 描述, 表名, [待确保的列DDL...])
# DDL须为"ADD COLUMN"形式；执行前按列名探测跳过已存在的列
MIGRATIONS: List[Tuple[int, str, str, List[str]]] = [
    (2, "cookie_account.use_count", "cookie_account",
     ["ALTER TABLE cookie_account ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0"]),
    (3, "friend.union_openid", "friend",
     ["ALTER TABLE friend ADD COLUMN union_openid VARCHAR(64)"]),
    (4, "bot_group.群信息四列(简介/分类/标签/成员数)", "bot_group",
     ["ALTER TABLE bot_group ADD COLUMN group_desc TEXT",
      "ALTER TABLE bot_group ADD COLUMN category VARCHAR(100)",
      "ALTER TABLE bot_group ADD COLUMN tags TEXT",
      "ALTER TABLE bot_group ADD COLUMN member_count INTEGER"]),
]

# 函数型迁移：版本号 -> (描述, 处理函数(conn, db_type))
MIGRATION_HANDLERS: Dict[int, Tuple[str, Any]] = {}

SCHEMA_VERSION_TABLE = "schema_version"


def migration_handler(version: int, desc: str):
    """函数型迁移注册装饰器"""
    def _deco(fn):
        MIGRATION_HANDLERS[version] = (desc, fn)
        return fn
    return _deco


async def _table_columns(conn, db_type: str, table: str) -> set:
    """获取表的列名集合"""
    if db_type == "sqlite":
        res = await conn.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in res}
    res = await conn.execute(
        text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
        {"table": table},
    )
    return {row[0] for row in res}


def _extract_column_name(alter_sql: str) -> str:
    """从ADD COLUMN语句提取列名（第三/四个词）"""
    parts = alter_sql.split()
    # ALTER TABLE <table> ADD COLUMN <col> ...
    idx = parts.index("COLUMN") + 1 if "COLUMN" in parts else 4
    return parts[idx].strip('"`')


async def run_migrations(engine: AsyncEngine) -> int:
    """
    执行增量迁移

    Args:
        engine: 异步引擎

    Returns:
        int: 应用的迁移数量
    """
    db_type = engine.dialect.name
    applied = 0

    async with engine.begin() as conn:
        # 版本表
        if db_type == "sqlite":
            await conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                "version INTEGER PRIMARY KEY, applied_at TIMESTAMP)"
            ))
        else:
            await conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                "version INT PRIMARY KEY, applied_at DATETIME)"
            ))

        res = await conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}"))
        current = res.scalar() or 0

        ddl_map = {v: (desc, table, ddls) for v, desc, table, ddls in MIGRATIONS}
        for version in sorted(set(ddl_map) | set(MIGRATION_HANDLERS)):
            if version <= current:
                continue

            if version in MIGRATION_HANDLERS:
                desc, handler = MIGRATION_HANDLERS[version]
                await handler(conn, db_type)
                logger.info(f"迁移v{version}: 已应用 {desc}")
                await conn.execute(
                    text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (:v)"),
                    {"v": version},
                )
                applied += 1
                continue

            desc, table, ddls = ddl_map[version]
            columns = await _table_columns(conn, db_type, table) if table else set()
            for ddl in ddls:
                col = _extract_column_name(ddl)
                if col in columns:
                    logger.info(f"迁移v{version}: 列{table}.{col}已存在，跳过")
                    continue
                await conn.execute(text(ddl))
                columns.add(col)
                logger.info(f"迁移v{version}: 已应用 {ddl[:60]}")

            await conn.execute(
                text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (:v)"),
                {"v": version},
            )
            applied += 1

    if applied:
        logger.info(f"Schema迁移完成，应用了 {applied} 个版本")
    return applied


@migration_handler(5, "group_binding.monitored BOOLEAN→INTEGER")
async def _migrate_v5_monitored_integer(conn, db_type: str) -> None:
    """
    group_binding.monitored列类型更正：BOOLEAN → INTEGER（与其余0/1标志列一致）

    - MySQL: MODIFY直接改类型
    - PostgreSQL: ALTER COLUMN TYPE USING布尔转整型
    - SQLite: 不支持改列类型，按模型结构重建表并搬移数据（保留唯一约束
      uq_binding_group_room与两个索引）
    """
    if db_type == "mysql":
        await conn.execute(text(
            "ALTER TABLE group_binding MODIFY monitored INTEGER NOT NULL DEFAULT 0"))
    elif db_type == "postgresql":
        await conn.execute(text(
            "ALTER TABLE group_binding ALTER COLUMN monitored TYPE INTEGER "
            "USING (monitored::int)"))
        await conn.execute(text(
            "ALTER TABLE group_binding ALTER COLUMN monitored SET DEFAULT 0"))
    else:  # sqlite：重建表更换列声明
        await conn.execute(text(
            "CREATE TABLE group_binding_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_openid VARCHAR(64) NOT NULL, room_id BIGINT NOT NULL, "
            "uid BIGINT NOT NULL DEFAULT 0, monitored INTEGER NOT NULL DEFAULT 0, "
            "created_at TIMESTAMP NOT NULL, "
            "CONSTRAINT uq_binding_group_room UNIQUE (group_openid, room_id))"))
        await conn.execute(text(
            "INSERT INTO group_binding_new (id, group_openid, room_id, uid, "
            "monitored, created_at) SELECT id, group_openid, room_id, uid, "
            "monitored, created_at FROM group_binding"))
        await conn.execute(text("DROP TABLE group_binding"))
        await conn.execute(text("ALTER TABLE group_binding_new RENAME TO group_binding"))
        await conn.execute(text(
            "CREATE INDEX ix_group_binding_group_openid ON group_binding (group_openid)"))
        await conn.execute(text(
            "CREATE INDEX ix_group_binding_room_id ON group_binding (room_id)"))
