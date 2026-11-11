# -*- coding: utf-8 -*-
"""
轻量Schema迁移器（2026-11-11重置）

schema_version表记录已应用版本，按版本号升序执行增量变更。

当前状态：**版本表重置为v1**——项目未公开发布、无生产库，数据库结构
由models.py的create_all直接建出（含全部索引），不携带任何历史迁移
（v2~v8已于2026-11-11全部清退，直接改表不写迁移）。启动时版本表中
的一切遗留版本号会被归一为仅v1。

今后用法：新表仍靠create_all自动建；create_all覆盖不了的变更
（改列类型/加索引/数据换算）在MIGRATIONS或MIGRATION_HANDLERS注册
版本号≥2的条目，并保证幂等（部分失败后重跑安全）。
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
    执行增量迁移（启动时调用；当前无注册迁移，仅做版本表归一）

    版本表归一：删除一切版本号行后写入v1——历史迁移已全部清退，
    遗留行（曾应用过v2~v8的库）一律归位为仅v1；新库直接初始化v1。
    数据库实际结构由models.py的create_all保证。

    Args:
        engine: 异步引擎

    Returns:
        int: 应用的迁移数量（当前恒为0）
    """
    db_type = engine.dialect.name
    applied = 0

    async with engine.begin() as conn:
        # 版本表
        if db_type == "sqlite":
            await conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                "version INTEGER PRIMARY KEY, applied_at BIGINT)"
            ))
        else:
            await conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
                "version INT PRIMARY KEY, applied_at BIGINT)"
            ))

        # 版本表归一为仅v1（历史迁移v2~v8已清退，遗留行全部删除）
        res = await conn.execute(text(
            f"SELECT COUNT(*) FROM {SCHEMA_VERSION_TABLE} WHERE version <> 1"))
        if (res.scalar() or 0):
            await conn.execute(text(
                f"DELETE FROM {SCHEMA_VERSION_TABLE} WHERE version <> 1"))
            # 正式发布前不向运维日志暴露"数据库被迁移/修改"——归一属内部
            # 例行动作，降为debug（debug=Y时仍可查）
            logger.debug("版本表归一为v1（清退历史遗留版本号）")
        res = await conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}"))
        if (res.scalar() or 0) < 1:
            await conn.execute(
                text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (1)"))
            logger.debug("版本表初始化v1")

        # 增量迁移：按版本号升序应用未记录的版本（当前注册表为空）
        res = await conn.execute(
            text(f"SELECT version FROM {SCHEMA_VERSION_TABLE}"))
        recorded = {row[0] for row in res}
        ddl_map = {v: (desc, table, ddls) for v, desc, table, ddls in MIGRATIONS}
        for version in sorted(set(ddl_map) | set(MIGRATION_HANDLERS)):
            if version <= 1 or version in recorded:
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
