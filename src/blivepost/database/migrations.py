# -*- coding: utf-8 -*-
"""
轻量Schema迁移器

schema_version表记录已应用版本，MIGRATIONS按版本号升序执行增量变更。
每条迁移在执行前自动检查目标列是否已存在（新库由create_all建出完整
结构，迁移自动跳过），跨SQLite/MySQL/PostgreSQL可用。
"""

from typing import List, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..utils.logger import get_logger


logger = get_logger("migrations")

# (版本号, 描述, 表名, [待确保的列DDL...])
# DDL须为"ADD COLUMN"形式；执行前按列名探测跳过已存在的列
MIGRATIONS: List[Tuple[int, str, str, List[str]]] = [
    (2, "cookie_account.use_count", "cookie_account",
     ["ALTER TABLE cookie_account ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0"]),
]

SCHEMA_VERSION_TABLE = "schema_version"


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

        for version, desc, table, ddls in MIGRATIONS:
            if version <= current:
                continue

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
