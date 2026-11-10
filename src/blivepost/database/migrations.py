# -*- coding: utf-8 -*-
"""
轻量Schema迁移器

schema_version表记录已应用版本，按版本号升序执行增量变更。

v2~v6迁移已于2026-11-10清退（数据库结构由models.py的create_all建出，
旧库升级一次性完成后版本表已重置为v1）——今后新表靠create_all自动建、
改列类型等DDL覆盖不了的变更注册新版本号的MIGRATIONS或MIGRATION_HANDLERS。

v7（第十六批）：全库时间列存储从DateTime(naive)改为13位毫秒Unix时间戳
（BIGINT，与时区无关）。见下方_migrate_time_columns_to_ms。
"""

from pathlib import Path
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

# 全库时间列名清单（v7迁移范围；今后新增时间列请同步此处）
TIME_COLUMNS = frozenset({
    "timestamp", "created_at", "updated_at", "last_used_at",
    "sessdata_expires_at", "start_time", "end_time",
})


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


# ========== v7：时间列 DateTime(naive) → 13位毫秒时间戳 ==========

def _convert_sqlite_file(db_path: Path) -> int:
    """直接用sqlite3转换单个子库文件的时间列值（房间/动态子库用）

    SQLite不强制列类型，只UPDATE值即可：text(旧datetime序列化)→毫秒整数，
    已是integer/NULL的行被WHERE typeof()过滤跳过，天然幂等。

    'utc'修饰符：strftime默认把时间串当UTC解析，而旧值是datetime.now()
    写入的机器本地naive时间——'utc'按机器时区换算回UTC，是写入的忠实逆运算。

    失败时抛异常（而非吞掉跳过）：子库被锁/损坏时若静默跳过，v7版本号
    仍会被记录、永不重试，该库残留text时间值导致统计静默错乱。v7幂等，
    中断后下次启动重跑对已转换部分无副作用。
    """
    import sqlite3
    converted = 0
    conn = sqlite3.connect(str(db_path), timeout=15)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            for col in cols & TIME_COLUMNS:
                # GLOB '*[!0-9]*'：仅转含非数字字符的datetime文本——TEXT亲和
                # 列（dynamic_seen.created_at）转出的纯数字串也是text，须跳过
                cur = conn.execute(
                    f'UPDATE "{table}" SET "{col}" = '
                    f'CAST(strftime(\'%s\', "{col}", \'utc\') AS INTEGER) * 1000 '
                    f'WHERE typeof("{col}") = \'text\' AND "{col}" GLOB \'*[!0-9]*\'')
                converted += cur.rowcount or 0
        conn.commit()
    except Exception as e:
        raise RuntimeError(
            f"子库时间列迁移失败({db_path})：{e}。请确认无残留进程占用该库后"
            f"重启重试（v7幂等，已转换部分不受影响）") from e
    finally:
        conn.close()
    return converted


async def _sqlite_main_db_path(conn) -> str:
    """当前连接的主库文件路径（内存库返回空串）"""
    res = await conn.execute(text("PRAGMA database_list"))
    for row in res:
        # (seq, name, file)：name='main'的file列即库文件路径
        if row[1] == "main":
            return row[2] or ""
    return ""


async def _migrate_sqlite_main(conn) -> int:
    """主库（sqlite）时间列值转换：经当前事务连接执行，与版本表写入同事务"""
    tables = [r[0] for r in (await conn.execute(text(
        "SELECT name FROM sqlite_master WHERE type='table'")))]
    converted = 0
    for table in tables:
        res = await conn.execute(text(f'PRAGMA table_info("{table}")'))
        cols = {row[1] for row in res}
        for col in cols & TIME_COLUMNS:
            cur = await conn.execute(text(
                f'UPDATE "{table}" SET "{col}" = '
                f'CAST(strftime(\'%s\', "{col}", \'utc\') AS INTEGER) * 1000 '
                f'WHERE typeof("{col}") = \'text\' AND "{col}" GLOB \'*[!0-9]*\''))
            converted += cur.rowcount or 0
    return converted


def _legacy_tz_offset_seconds() -> int:
    """旧naive时间值的解释时区偏移（Setting.timezone，与SQLite路径'utc'
    修饰符同义——旧值由运行机datetime.now()写入）。

    MySQL的UNIX_TIMESTAMP按会话time_zone解释naive值、PG的EXTRACT(EPOCH)
    一律按UTC解释：远程DB会话时区≠写入机时区时会整体偏移，故转换前显式
    设置会话时区/附加偏移。注：固定偏移对历史夏令时时段不精确（典型
    部署中国无夏令时，无影响）。
    """
    from datetime import datetime
    from ..utils.timeutil import get_default_timezone
    off = datetime.now(get_default_timezone()).utcoffset()
    return int(off.total_seconds()) if off else 0


async def _migrate_mysql_or_pg(conn, db_type: str) -> int:
    """主库（MySQL/PostgreSQL）时间列转换：值换算 + 列类型改BIGINT

    幂等依据：information_schema的data_type——已是bigint的列（转换过/
    新建的库）跳过。MySQL的MODIFY会丢列注释（历史列无服务端默认值，
    仅python侧default，无损）；转换经UNIX_TIMESTAMP/EXTRACT按秒×1000。

    MySQL必须"加新列→回填→删旧列→改名"三步：直接向DATETIME列UPDATE
    13位整数在严格模式下报1292、非严格模式静默清零毁数据。三步在部分
    失败后重跑也幂等（新列存在则跳过ADD、旧列已删则跳过DROP）。
    """
    schema_cond = "table_schema = DATABASE()" if db_type == "mysql" \
        else "table_schema = current_schema()"
    res = await conn.execute(text(
        "SELECT table_name, column_name, data_type, is_nullable "
        f"FROM information_schema.columns WHERE {schema_cond}"))
    targets = [
        (r[0], r[1], r[3]) for r in res
        if r[1] in TIME_COLUMNS and r[2] in ("datetime", "timestamp",
                                             "timestamp without time zone")
    ]
    converted = 0
    if targets and db_type == "mysql":
        # 会话时区对齐写入侧（UNIX_TIMESTAMP按会话时区解释naive值）
        from ..utils.timeutil import get_default_timezone
        off = _legacy_tz_offset_seconds()
        tz_name = str(get_default_timezone())
        try:
            await conn.execute(text(
                "SET SESSION time_zone = :tz"), {"tz": tz_name})
        except Exception:
            # 未加载时区表的老库只接受固定偏移（±HH:MM）
            sign = "-" if off < 0 else "+"
            off_abs = abs(off)
            await conn.execute(text(
                f"SET SESSION time_zone = '{sign}{off_abs // 3600:02d}:"
                f"{off_abs % 3600 // 60:02d}'"))
    for table, col, nullable in targets:
        if db_type == "mysql":
            ms_col = f"{col}_ms"
            cols_now = await _table_columns(conn, "mysql", table)
            if ms_col not in cols_now:
                await conn.execute(text(
                    f"ALTER TABLE `{table}` ADD COLUMN `{ms_col}` BIGINT NULL"))
            await conn.execute(text(
                f"UPDATE `{table}` SET `{ms_col}` = "
                f"CAST(UNIX_TIMESTAMP(`{col}`) * 1000 AS SIGNED) "
                f"WHERE `{col}` IS NOT NULL"))
            if col in cols_now:
                await conn.execute(text(
                    f"ALTER TABLE `{table}` DROP COLUMN `{col}`"))
            await conn.execute(text(
                f"ALTER TABLE `{table}` CHANGE COLUMN `{ms_col}` `{col}` BIGINT "
                f"{'NOT NULL' if nullable == 'NO' else 'NULL'}"))
        else:
            # PG的EXTRACT(EPOCH FROM naive)按UTC解释：补写入侧偏移秒
            await conn.execute(text(
                f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE BIGINT '
                f'USING ((EXTRACT(EPOCH FROM "{col}") + :off) * 1000)::BIGINT'),
                {"off": _legacy_tz_offset_seconds()})
        converted += 1
        logger.info(f"迁移v7: {table}.{col} 已转为毫秒BIGINT")
    return converted


async def _migrate_sub_dbs(main_db_path: str) -> int:
    """房间子库（rooms/目录）与动态子库（dynamics/目录）的值转换

    子库路径需动态发现且不走SQLAlchemy（独立sqlite文件，直接sqlite3打开）：
    - rooms：主库同级的rooms/目录（<dir>/rooms/<主库stem>/room_*.db，含历史布局）
    - dynamics：主库同级dynamics/与工作目录data/dynamics（两种布局均覆盖）
    """
    converted = 0
    files: List[Path] = []
    if main_db_path:
        main = Path(main_db_path)
        rooms_dir = main.parent / "rooms"
        if rooms_dir.is_dir():
            files.extend(rooms_dir.rglob("*.db"))
        for d in (main.parent / "dynamics", Path("data") / "dynamics"):
            if d.is_dir():
                files.extend(d.glob("*.db"))
    for path in sorted(set(files)):
        n = _convert_sqlite_file(path)
        if n:
            logger.info(f"迁移v7: 子库{path.name}转换{n}行时间列")
        converted += n
    return converted


@migration_handler(7, "全库时间列从DateTime(naive)改为13位毫秒Unix时间戳")
async def _migrate_time_columns_to_ms(conn, db_type: str) -> None:
    """v7：主库+全部子库的时间列值统一为13位毫秒时间戳（幂等）"""
    if db_type == "sqlite":
        main_path = await _sqlite_main_db_path(conn)
        n = await _migrate_sqlite_main(conn)
        n += await _migrate_sub_dbs(main_path)
    else:
        n = await _migrate_mysql_or_pg(conn, db_type)
    if n:
        logger.info(f"迁移v7: 时间列毫秒化共转换{n}行/列")


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

        # 旧库v2~v6已清退：版本表中的遗留版本号删除（v7及以后的新迁移
        # 版本号必须保留，否则每次启动都会重复应用）；幂等——新库无行时插入v1
        res = await conn.execute(text(
            f"SELECT COUNT(*) FROM {SCHEMA_VERSION_TABLE} "
            "WHERE version BETWEEN 2 AND 6"))
        if (res.scalar() or 0):
            await conn.execute(text(
                f"DELETE FROM {SCHEMA_VERSION_TABLE} "
                "WHERE version BETWEEN 2 AND 6"))
            logger.info("迁移: 已清退遗留版本号v2~v6")
        res = await conn.execute(text(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}"))
        if (res.scalar() or 0) < 1:
            await conn.execute(
                text(f"INSERT INTO {SCHEMA_VERSION_TABLE} (version) VALUES (1)"))
            logger.info("迁移: 版本表初始化v1")

        # 增量迁移：按版本号升序应用未记录的版本（v7+；已记录的跳过——
        # v7起版本号在库内长期保留，重跑仅应用新增版本）
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
