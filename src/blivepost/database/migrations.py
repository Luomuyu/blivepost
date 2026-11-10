# -*- coding: utf-8 -*-
"""
轻量Schema迁移器

schema_version表记录已应用版本，按版本号升序执行增量变更。

v2~v6迁移已于2026-11-10清退（数据库结构由models.py的create_all建出，
旧库升级一次性完成后版本表已重置为v1）——今后新表靠create_all自动建、
改列类型等DDL覆盖不了的变更注册新版本号的MIGRATIONS或MIGRATION_HANDLERS。
"""

from typing import Any, Dict, List, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ..utils.logger import get_logger


logger = get_logger("migrations")

# (版本号, 描述, 表名, [待确保的列DDL...])
# DDL须为"ADD COLUMN"形式；执行前按列名探测跳过已存在的列
MIGRATIONS: List[Tuple[int, str, str, List[str]]] = []

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
    """
    获取表的列名集合

    MySQL/PostgreSQL 必须限定当前库/当前schema：information_schema.columns
    汇聚服务器上全部库的列，不过滤时其他库的同名表会让本库缺列被误判为
    "列已存在"而跳过补列。
    """
    if db_type == "sqlite":
        res = await conn.execute(text(f"PRAGMA table_info({table})"))
        return {row[1] for row in res}
    if db_type == "mysql":
        sql = ("SELECT column_name FROM information_schema.columns "
               "WHERE table_name = :table AND table_schema = DATABASE()")
    else:  # postgresql
        sql = ("SELECT column_name FROM information_schema.columns "
               "WHERE table_name = :table AND table_schema = current_schema()")
    res = await conn.execute(text(sql), {"table": table})
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

        # 旧库v2~v6已清退：重置版本表为v1（幂等——新库无行时插入v1，
        # 旧库已有更高版本号时归一为v1避免遗留版本号阻塞后续新迁移）
        res = await conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}"))
        current = res.scalar() or 0
        if current > 1:
            await conn.execute(text(f"DELETE FROM {SCHEMA_VERSION_TABLE}"))
            await conn.execute(
                text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (1)"))
            logger.info(f"迁移: 版本表从v{current}重置为v1（v2~v6已清退）")
        elif current < 1:
            await conn.execute(
                text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (1)"))
            logger.info("迁移: 版本表初始化v1")

        # 后续新迁移从v2开始注册
        ddl_map = {v: (desc, table, ddls) for v, desc, table, ddls in MIGRATIONS}
        for version in sorted(set(ddl_map) | set(MIGRATION_HANDLERS)):
            if version <= 1:
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
