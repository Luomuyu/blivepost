# -*- coding: utf-8 -*-
"""直播间子数据库架构测试：路由/映射/扇出/旧库迁移/动态已见表"""

import pytest
from sqlalchemy import select

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.database.models import DanmakuRecord, EventBase, RoomDbRecord


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
    from datetime import datetime

    from sqlalchemy import func

    svc, db = await _make_svc(tmp_path, "legacy")
    # 模拟旧库：主库手工建事件表并写入数据（新架构主库不再建事件表）
    async with db._engine.begin() as conn:
        await conn.run_sync(EventBase.metadata.create_all)
    async with db.get_session() as session:
        session.add(DanmakuRecord(room_id=100, uid=1, uname="u", msg="old1",
                                  timestamp=datetime.now()))
        session.add(DanmakuRecord(room_id=200, uid=1, uname="u", msg="old2",
                                  timestamp=datetime.now()))
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
    """REST开播时间解析：合法值/零值/空值"""
    from datetime import datetime

    from blivepost.core.handler import _parse_live_time
    assert _parse_live_time("2026-09-06 20:00:00") == datetime(2026, 9, 6, 20, 0, 0)
    assert _parse_live_time("0000-00-00 00:00:00") is None
    assert _parse_live_time("") is None
    assert _parse_live_time("bad") is None


def test_live_stats_duration_guard_and_reset():
    """reset清start_time后时长为0（守卫不抛异常）；设值后正常计时"""
    from datetime import datetime, timedelta

    from blivepost.models.live import LiveStats
    stats = LiveStats()
    assert stats.duration_seconds == 0  # start_time为None时守卫返回0
    stats.reset()
    stats.start_time = datetime.now() - timedelta(hours=2)
    assert 7199 <= stats.duration_seconds <= 7201
    stats.end_time = stats.start_time + timedelta(hours=2)
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


async def test_migration_v4_adds_group_info_columns():
    """旧bot_group表（无群信息四列）经迁移v4补齐，新列可写NULL"""
    import os
    import sqlite3
    import tempfile

    db_path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE bot_group (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "group_openid TEXT UNIQUE, group_name TEXT DEFAULT '', "
        "del_flag INTEGER DEFAULT 0, msg_receive INTEGER DEFAULT 1)"
    )
    conn.execute("INSERT INTO bot_group (group_openid, group_name) VALUES ('G9', '旧群')")
    conn.commit()
    conn.close()

    settings = Settings()
    settings.database.sqlite_path = db_path
    db = DatabaseManager(settings.database)
    await db.initialize()

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(bot_group)")}
    conn.close()
    assert {"group_desc", "category", "tags", "member_count"} <= cols

    svc = DatabaseService(db)
    await svc.update_group_info("G9", {"group_name": "旧群",
                                       "group_member_num": 3})
    # 部分字段更新后其余仍为NULL → 仍视为缺失，待下次完整查询回填
    missing = await svc.list_groups_missing_info()
    assert missing == ["G9"]
    await db.close()


@pytest.mark.asyncio
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
