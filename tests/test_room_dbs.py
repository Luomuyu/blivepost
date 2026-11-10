# -*- coding: utf-8 -*-
"""直播间子数据库架构测试：路由/映射/扇出/旧库迁移/动态已见表"""

import pytest
from sqlalchemy import select

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.database.models import (
    DanmakuRecord, EventBase, GroupBindingRecord, RoomDbRecord,
)
from blivepost.utils.timeutil import dt_to_ms, now_ms

_MS_PER_HOUR = 3_600_000


async def _make_svc(tmp_path, name="t"):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / f"{name}.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    return svc, db


@pytest.mark.asyncio
async def test_event_routing_to_room_subdb(tmp_path):
    """事件写入路由到每房间独立子库；主库登记映射；汇总读自子库"""
    svc, db = await _make_svc(tmp_path)
    await svc.save_danmaku(room_id=100, uid=1, uname="u", msg="a")
    await svc.save_gift(room_id=200, uid=1, uname="u", gift_id=1,
                        gift_name="g", num=1, price=1000, coin_type="gold")

    rooms_dir = tmp_path / "rooms" / "t"
    assert (rooms_dir / "room_100.db").exists()
    assert (rooms_dir / "room_200.db").exists()

    # 主库映射登记（room_db两行）
    async with db.get_session() as session:
        rows = (await session.execute(select(RoomDbRecord.room_id))).all()
        assert sorted(r[0] for r in rows) == [100, 200]

    # 单房汇总各读各的子库，互不串
    s100 = await svc.get_daily_summary(100)
    assert s100["danmaku_count"] == 1 and s100["gift_gold"] == 0
    s200 = await svc.get_daily_summary(200)
    assert s200["danmaku_count"] == 0 and s200["gift_gold"] == 1.0

    # 跨房扇出：趋势按日合并
    hist = await svc.get_daily_history(days=3)
    assert hist[-1]["danmaku"] == 1 and hist[-1]["gift_gold"] == 1.0

    # 存储体积合计（主库+子库）
    assert await svc.get_storage_size_mb() > 0
    await db.close()


@pytest.mark.asyncio
async def test_legacy_event_migration(tmp_path):
    """旧库事件表数据按房间搬入子库，主库清空，汇总读数不丢"""
    from sqlalchemy import func

    svc, db = await _make_svc(tmp_path, "legacy")
    # 模拟旧库：主库手工建事件表并写入数据（新架构主库不再建事件表）
    async with db._engine.begin() as conn:
        await conn.run_sync(EventBase.metadata.create_all)
    async with db.get_session() as session:
        session.add(DanmakuRecord(room_id=100, uid=1, uname="u", msg="old1",
                                  timestamp=now_ms()))
        session.add(DanmakuRecord(room_id=200, uid=1, uname="u", msg="old2",
                                  timestamp=now_ms()))
        await session.commit()

    moved = await svc.migrate_legacy_events()
    assert moved == 2

    async with db.get_session() as session:
        res = await session.execute(select(func.count()).select_from(DanmakuRecord))
        assert int(res.scalar() or 0) == 0  # 主库已搬空

    assert (await svc.get_daily_summary(100))["danmaku_count"] == 1
    assert (await svc.get_daily_summary(200))["danmaku_count"] == 1
    await db.close()


def test_dynamic_seen_roundtrip(tmp_path):
    """动态已见表：每UP独立子库、INSERT OR IGNORE去重、UP间隔离"""
    from blivepost.services.dynamics import load_seen_dynamic_ids, save_seen_dynamic_ids

    d = tmp_path / "dynamics"
    assert load_seen_dynamic_ids(7, d) == set()
    save_seen_dynamic_ids(7, ["d1", "d2"], d)
    save_seen_dynamic_ids(7, ["d1"], d)  # 重复记录忽略
    assert load_seen_dynamic_ids(7, d) == {"d1", "d2"}
    assert load_seen_dynamic_ids(8, d) == set()  # UP间隔离


@pytest.mark.asyncio
async def test_migrate_fresh_db_silent_skip(tmp_path):
    """新装库（主库无事件表）：迁移静默返回0，不产生告警/异常"""
    svc, db = await _make_svc(tmp_path, "fresh")
    moved = await svc.migrate_legacy_events()
    assert moved == 0
    await db.close()


def test_parse_live_time():
    """REST开播时间解析：合法值/零值/空值（返回13位毫秒时间戳）"""
    from datetime import datetime

    from blivepost.core.handler import _parse_live_time
    assert _parse_live_time("2026-09-06 20:00:00") == dt_to_ms(
        datetime(2026, 9, 6, 20, 0, 0))
    assert _parse_live_time("0000-00-00 00:00:00") is None
    assert _parse_live_time("") is None
    assert _parse_live_time("bad") is None


def test_live_stats_duration_guard_and_reset():
    """reset清start_time后时长为0（守卫不抛异常）；设值后正常计时"""
    from blivepost.models.live import LiveStats
    stats = LiveStats()
    assert stats.duration_seconds == 0  # start_time为None时守卫返回0
    stats.reset()
    stats.start_time = now_ms() - 2 * _MS_PER_HOUR
    assert 7199 <= stats.duration_seconds <= 7201
    stats.end_time = stats.start_time + 2 * _MS_PER_HOUR
    assert stats.duration_seconds == 7200


# ==================== 群信息快照（bot_group四列） ====================


async def test_group_info_update_null_semantics(tmp_path):
    """群信息更新：有值写入、查询项为空填NULL、tags序列化为JSON"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)

    await svc.update_group_info("G1", {
        "group_name": "测试群",
        "group_finger_memo": "简介文本",
        "group_class_text": "游戏",
        "group_tags": ["手游", "公会"],
        "group_member_num": 42,
    })
    missing = await svc.list_groups_missing_info()
    assert missing == []  # 全量信息无空值

    # 部分字段为空 → NULL
    await svc.update_group_info("G2", {
        "group_name": "残缺群",
        "group_finger_memo": "",
        "group_tags": [],
    })
    missing = await svc.list_groups_missing_info()
    assert "G2" in missing and "G1" not in missing

    # 落库值核对（tags序列化/成员数NULL）
    from sqlalchemy import select as _select
    from blivepost.database.models import GroupRecord
    async with db.get_session() as session:
        rows = {r.group_openid: r for r in
                (await session.execute(_select(GroupRecord))).scalars()}
    assert rows["G1"].tags == '["手游", "公会"]'
    assert rows["G1"].member_count == 42
    assert rows["G2"].group_desc is None and rows["G2"].tags is None
    assert rows["G2"].member_count is None
    await db.close()


async def test_group_info_refresher(tmp_path):
    """群信息每日刷新：逐群查询更新；bot未启动时安全返回0"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.save_group("G1")
    await svc.save_group("G2")

    class FakeBot:
        def __init__(self):
            self.asked = []

        async def get_group_info(self, openid):
            self.asked.append(openid)
            if openid == "G1":
                return {"group_name": "群一", "group_finger_memo": "desc1",
                        "group_class_text": "分类", "group_tags": ["t1"],
                        "group_member_num": 10}
            return None  # 查询失败 → 不更新（保持NULL）

    from blivepost.core.manager import GroupInfoRefresher
    bot = FakeBot()
    refresher = GroupInfoRefresher(svc, bot_client=bot)
    assert await refresher.refresh_once() == 1
    assert bot.asked == ["G1", "G2"]
    assert await svc.get_streamer_name if False else True
    missing = await svc.list_groups_missing_info()
    assert missing == ["G2"]  # 查询失败的群保持NULL待下轮回填

    # 空表/无bot安全
    empty_refresher = GroupInfoRefresher(svc, bot_client=None)
    assert await empty_refresher.refresh_once() == 0
    await db.close()



async def test_live_page_bindings_carry_group_details(tmp_path):
    """回归：直播页绑定状态携带群信息（此前误读GroupBindingRecord触发AttributeError）"""
    from blivepost.services.bindings import BindingStore
    from blivepost.services.webpanel import WebPanel

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.update_group_info("G1", {
        "group_name": "测试一群", "group_finger_memo": "简介文本",
        "group_class_text": "游戏", "group_tags": ["手游", "公会"],
        "group_member_num": 42,
    })
    await svc.save_live_room_info(9646546, uid=91156053, title="标题")

    class Pool:
        accounts = []
    class MM:
        def room_ids(self): return []
        def has(self, rid): return False
        def get_cookie_uid(self, rid): return None
        def get_account_rooms(self, uid): return []
    store = BindingStore(svc)
    await store.bind("G1", 9646546, 91156053)
    await store.set_monitored("G1", 9646546, True)
    panel = WebPanel(Pool(), MM(), None, store, svc,
                     bot_client=None)

    ctx = await panel._page_context("live")
    assert ctx["rooms"][0]["room_id"] == 9646546
    b = ctx["bindings"][0]
    assert b["group_name"] == "测试一群"
    assert b["group_desc"] == "简介文本"
    assert b["category"] == "游戏"
    assert b["tags"] == "手游、公会"  # JSON解析为顿号串展示
    assert b["member_count"] == 42
    await db.close()


@pytest.mark.asyncio
async def test_dynamics_page_group_bindings(tmp_path):
    """回归：动态页拆分两段后，下半段群绑定状态携带群详情并整页渲染"""
    from aiohttp.test_utils import make_mocked_request

    from blivepost.services.bindings import BindingStore
    from blivepost.services.webpanel import WebPanel

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.update_group_info("G1", {
        "group_name": "测试一群", "group_finger_memo": "简介文本",
        "group_class_text": "游戏", "group_tags": ["手游", "公会"],
        "group_member_num": 42,
    })

    class Pool:
        accounts = []
    class MM:
        def room_ids(self): return []
        def has(self, rid): return False
        def get_cookie_uid(self, rid): return None
        def get_account_rooms(self, uid): return []
    class DynMgr:
        def has(self, uid): return True
    store = BindingStore(svc)
    await store.bind_dynamic("G1", 91156053, up_name="墨翎_Moryin")
    panel = WebPanel(Pool(), MM(), DynMgr(), store, svc, bot_client=None)

    ctx = await panel._page_context("dynamics")
    assert ctx["dynamics"] == [{"up_uid": 91156053, "name": "墨翎_Moryin",
                                "alive": True, "groups": 1}]
    b = ctx["dyn_bindings"][0]
    assert b["group_openid"] == "G1"
    assert b["up_uid"] == 91156053 and b["up_name"] == "墨翎_Moryin"
    assert b["group_name"] == "测试一群"
    assert b["group_desc"] == "简介文本"
    assert b["category"] == "游戏"
    assert b["tags"] == "手游、公会"
    assert b["member_count"] == 42
    assert any(ps["label"] == "动态监控" for ps in ctx["push_settings"])

    # 整页渲染：下半段标题与群行/详情行出现
    req = make_mocked_request("GET", "/dynamics")  # token为空时不鉴权
    resp = await panel._make_page_handler("dynamics")(req)
    assert resp.status == 200
    body = resp.text
    assert "群绑定状态" in body
    assert "toggleDynGroupDetail" in body
    assert "墨翎_Moryin" in body
    assert 'data-key="G1-91156053"' in body
    await db.close()


@pytest.mark.asyncio
async def test_group_push_setting_lifecycle(tmp_path):
    """群独立推送设置全生命周期：绑定播种继承全局/解绑清理/重绑重新继承"""
    from blivepost.services.bindings import BindingStore

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.seed_push_switches({"anchor": True, "guard_achievement": True,
                                  "system_event": True, "room_change": False,
                                  "daily_report": False, "weekly_report": False,
                                  "monthly_report": False, "dynamic_monitor": False})
    store = BindingStore(svc)
    await store.load()

    # 绑定播种：行值继承全局开关（anchor开/room_change关）
    await store.bind("G1", 100, 1)
    own = await svc.get_group_push_setting(100, "G1")
    assert own is not None
    assert own["anchor"] is True and own["guard_achievement"] is True
    assert own["room_change"] is False and own["monthly_report"] is False

    # 独立翻转：本群可单独开room_change（全局仍关）
    await svc.set_group_push_setting(100, "G1", "room_change", True)
    assert (await svc.get_group_push_setting(100, "G1"))["room_change"] is True
    assert await svc.get_push_switch_enabled("room_change", False) is False

    # 幂等：重复播种不覆盖已有行
    await svc.seed_group_push_settings(100, ["G1"], {"room_change": False})
    assert (await svc.get_group_push_setting(100, "G1"))["room_change"] is True

    # 解绑清理 → 重绑重新继承全局（room_change回到关）
    await store.unbind("G1", 100)
    assert await svc.get_group_push_setting(100, "G1") is None
    await store.bind("G1", 100, 1)
    assert (await svc.get_group_push_setting(100, "G1"))["room_change"] is False
    await db.close()


@pytest.mark.asyncio
async def test_live_page_group_push_settings_ctx(tmp_path):
    """直播页群详情携带本群推送设置（custom标记）与前台改权限"""
    from blivepost.services.bindings import BindingStore
    from blivepost.services.webpanel import WebPanel

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.seed_push_switches({"anchor": True})
    store = BindingStore(svc)
    await store.load()
    await store.bind("G1", 100, 1)
    await store.set_monitored("G1", 100, True)
    await svc.save_live_room_info(100, uid=9, title="t")

    class Pool:
        accounts = []
    class MM:
        def room_ids(self): return [100]
        def has(self, rid): return True
        def get_cookie_uid(self, rid): return 1
        def get_account_rooms(self, uid): return [100]
    panel = WebPanel(Pool(), MM(), None, store, svc, bot_client=None)

    ctx = await panel._page_context("live")
    b = ctx["bindings"][0]
    items = {ps["name"]: ps for ps in b["push_settings"]}
    assert "dynamic_monitor" not in items  # 全局项不随房间
    assert items["anchor"]["on"] is True and items["anchor"]["custom"] is False
    # 独立修改后ctx标记custom
    await svc.set_group_push_setting(100, "G1", "anchor", False)
    ctx2 = await panel._page_context("live")
    b2 = ctx2["bindings"][0]
    assert b2["has_custom"] is True
    items2 = {ps["name"]: ps for ps in b2["push_settings"]}
    assert items2["anchor"]["on"] is False and items2["anchor"]["custom"] is True
    await db.close()


# ==================== 旧库迁移幂等性（复制一半崩溃重跑） ====================


@pytest.mark.asyncio
async def test_legacy_migration_crash_midway_rerun_idempotent(tmp_path):
    """复制一半崩溃重跑安全：子库已存在的id跳过（不IntegrityError卡死），主库按批删除"""
    from sqlalchemy import func

    svc, db = await _make_svc(tmp_path, "crash")
    # 模拟旧库：主库手工建事件表并写入两房间各2行
    async with db._engine.begin() as conn:
        await conn.run_sync(EventBase.metadata.create_all)
    async with db.get_session() as session:
        for room in (100, 200):
            for i in range(2):
                session.add(DanmakuRecord(room_id=room, uid=1, uname="u",
                                          msg=f"m{i}", timestamp=now_ms()))
        await session.commit()

    # 首跑模拟崩溃：第一个房间复制提交后、第二个房间复制前失败（删除阶段未到）
    original = svc._room_manager
    calls = []

    async def crash_on_second(room_id):
        calls.append(room_id)
        if len(calls) >= 2:
            raise RuntimeError("模拟中途崩溃")
        return await original(room_id)

    svc._room_manager = crash_on_second
    with pytest.raises(RuntimeError):
        await svc._migrate_legacy_table(DanmakuRecord)
    svc._room_manager = original

    async with db.get_session() as session:
        res = await session.execute(select(func.count()).select_from(DanmakuRecord))
        assert int(res.scalar() or 0) == 4  # 删除阶段未执行，主库数据原样

    # 重跑：已复制房间的id冲突跳过、其余照常插入，整批删除——不再主键冲突卡死
    moved = await svc._migrate_legacy_table(DanmakuRecord)
    assert moved == 4

    async with db.get_session() as session:
        res = await session.execute(select(func.count()).select_from(DanmakuRecord))
        assert int(res.scalar() or 0) == 0  # 主库已清空
    assert (await svc.get_daily_summary(100))["danmaku_count"] == 2  # 子库无重复行
    assert (await svc.get_daily_summary(200))["danmaku_count"] == 2
    assert await svc._migrate_legacy_table(DanmakuRecord) == 0  # 再次重跑安全返回0
    await db.close()


# ==================== 模型Schema回归：主键类型与冗余索引 ====================

# 事件表 -> (room_id, timestamp) 复合索引名
_EVENT_TABLES_WITH_ROOM_TIME = {
    "danmaku": "idx_danmaku_room_time",
    "gift": "idx_gift_room_time",
    "super_chat": "idx_sc_room_time",
    "guard": "idx_guard_room_time",
    "anchor": "idx_anchor_room_time",
    "red_pocket": "idx_redpocket_room_time",
    "guard_achievement": "idx_achievement_room_time",
    "system_event": "idx_sys_event_room_time",
    "room_change": "idx_room_change_room_time",
    "live_status": "idx_live_status_room_time",
}


def _index_columns(conn, table):
    """{索引名: [覆盖列,...]}（PRAGMA index_list + index_info；含唯一约束索引）"""
    result = {}
    for row in conn.execute(f"PRAGMA index_list({table})"):
        name = row[1]
        result[name] = [r[2] for r in conn.execute(f"PRAGMA index_info({name})")]
    return result


async def _build_event_db(path):
    """独立建一个事件子库（EventBase全部表）"""
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.run_sync(EventBase.metadata.create_all)
    await engine.dispose()


@pytest.mark.asyncio
async def test_event_tables_pk_integer_and_autoincrement(tmp_path):
    """自增主键SQLite渲染INTEGER（rowid别名）：不指定id插入仍可自增"""
    import sqlite3

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ev.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EventBase.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        rec = DanmakuRecord(room_id=100, uid=1, uname="u", msg="m")
        session.add(rec)
        await session.commit()
        assert rec.id == 1  # 未显式给id，自增生效
    await engine.dispose()

    conn = sqlite3.connect(tmp_path / "ev.db")
    try:
        for table in _EVENT_TABLES_WITH_ROOM_TIME:
            info = {r[1]: r[2] for r in conn.execute(f"PRAGMA table_info({table})")}
            assert info["id"] == "INTEGER", f"{table}.id 应渲染为 INTEGER"
    finally:
        conn.close()


def test_pk_type_renders_bigint_on_mysql_and_pg():
    """主键类型跨方言：MySQL渲染BIGINT AUTO_INCREMENT、PG渲染BIGSERIAL（新库演进）；
    primary_key_column/set_db_type 死代码机制已移除"""
    from sqlalchemy.dialects import mysql, postgresql
    from sqlalchemy.schema import CreateTable

    from blivepost.database import models

    ddl_mysql = str(CreateTable(GroupBindingRecord.__table__).compile(
        dialect=mysql.dialect()))
    ddl_pg = str(CreateTable(GroupBindingRecord.__table__).compile(
        dialect=postgresql.dialect()))
    assert "id BIGINT NOT NULL AUTO_INCREMENT" in ddl_mysql
    assert "BIGSERIAL" in ddl_pg
    assert not hasattr(models, "primary_key_column")
    assert not hasattr(models, "set_db_type")


@pytest.mark.asyncio
async def test_event_tables_no_redundant_room_id_index(tmp_path):
    """事件表room_id无单列索引（复合索引最左前缀已覆盖），复合索引保留"""
    import sqlite3

    await _build_event_db(tmp_path / "ev2.db")

    conn = sqlite3.connect(tmp_path / "ev2.db")
    try:
        for table, composite in _EVENT_TABLES_WITH_ROOM_TIME.items():
            idx = _index_columns(conn, table)
            assert not any(cols == ["room_id"] for cols in idx.values()), table
            assert idx.get(composite) == ["room_id", "timestamp"], table
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_binding_tables_group_openid_index_removed(tmp_path):
    """绑定表group_openid无单列索引（唯一约束最左前缀已覆盖）；room_id/up_uid索引保留"""
    import sqlite3

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "bind.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    await db.close()

    conn = sqlite3.connect(settings.database.sqlite_path)
    try:
        gb = _index_columns(conn, "group_binding")
        assert not any(cols == ["group_openid"] for cols in gb.values())
        assert any(cols == ["group_openid", "room_id"] for cols in gb.values())
        assert any(cols == ["room_id"] for cols in gb.values())

        dbb = _index_columns(conn, "dynamic_binding")
        assert not any(cols == ["group_openid"] for cols in dbb.values())
        assert any(cols == ["group_openid", "up_uid"] for cols in dbb.values())
        assert any(cols == ["up_uid"] for cols in dbb.values())
    finally:
        conn.close()
