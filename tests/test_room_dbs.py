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
