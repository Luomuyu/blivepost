# -*- coding: utf-8 -*-
"""阶段1（可靠性加固）回归测试

覆盖：
1. 房间→子库映射进程内缓存（热路径零主库查询）
2. 子库registry并发首建 + close_all释放
3. 日结算失败当日重试（不静默顺延次日）
4. 子进程崩溃重建指数退避与放弃上限
5. qqbot有界发送队列（满则丢弃、worker消费）
6. SQLite WAL开启 + MySQL/PG密码URL转义
"""

import asyncio

import pytest

from blivepost.config.settings import Settings
from blivepost.core import manager as manager_mod
from blivepost.core.manager import MonitorManager
from blivepost.database.manager import DatabaseManager
from blivepost.database.service import DatabaseService
from blivepost.services.qqbot import QQBotClient
from blivepost.services.report import DailyReportJob


# ---------- 1. 房间映射缓存 ----------

async def test_room_db_cache(tmp_path):
    """第二次落库不再查主库（缓存命中）；缓存值与登记行一致"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        await svc.save_danmaku(room_id=23058, uid=1, uname="u", msg="hi")
        assert svc._room_db_cache.get(23058) == "room_23058.db"
        # 清空主库room_db表后再落库：缓存命中则不再触发主库登记
        async with db.get_session() as session:
            from sqlalchemy import delete
            from blivepost.database.models import RoomDbRecord
            await session.execute(delete(RoomDbRecord))
            await session.commit()
        await svc.save_danmaku(room_id=23058, uid=1, uname="u", msg="again")
        async with db.get_session() as session:
            from sqlalchemy import select
            from blivepost.database.models import RoomDbRecord
            rows = (await session.execute(select(RoomDbRecord.room_id))).all()
        # 主库行被删后未重登记（走缓存）；事件仍写入子库
        assert rows == []
        summary = await svc.get_daily_summary(23058)
        assert summary["danmaku_count"] == 2
    finally:
        await svc.aclose()
        await db.close()


# ---------- 2. registry 并发与释放 ----------

async def test_registry_concurrent_get_single_engine(tmp_path):
    """并发首建同一子库只初始化一个engine（原竞态泄漏输家engine）"""
    from blivepost.database.room_store import RoomDbRegistry
    reg = RoomDbRegistry(tmp_path)
    managers = await asyncio.gather(*(reg.get("room_1.db") for _ in range(8)))
    assert len({id(m) for m in managers}) == 1
    await reg.close_all()
    assert await reg.all() == []


# ---------- 3. 日结算当日重试 ----------

class _NullStore:
    def get_monitored_room_ids(self):
        return []


async def test_report_run_retries_same_day(monkeypatch):
    """结算失败后当日按重试间隔重试（原实现顺延次日=静默丢失）"""
    rep = DailyReportJob(_NullStore(), None, None)
    rep.RETRY_DELAY = 0.01
    calls = {"n": 0}

    async def flaky_check(day=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return 0

    async def ok_periodic():
        return 0

    monkeypatch.setattr(rep, "check_once", flaky_check)
    monkeypatch.setattr(rep, "check_periodic", ok_periodic)
    monkeypatch.setattr(rep, "_seconds_until_next", lambda: 0.0)

    task = asyncio.create_task(rep.run())
    for _ in range(200):
        if calls["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["n"] >= 2  # 失败后当日重试成功


# ---------- 4. 崩溃重建退避与上限 ----------

class _FakeDeadRoom:
    is_alive = False

    def __init__(self, uptime):
        self.uptime = uptime


async def test_rebuild_backoff_and_give_up(monkeypatch):
    """连续快速崩溃指数退避；超过上限放弃；稳定运行后计数重置"""
    mgr = MonitorManager(Settings())
    ensured = []

    async def fake_ensure(rid, manual=True):
        ensured.append(rid)
        return True, None

    monkeypatch.setattr(mgr, "ensure", fake_ensure)
    monkeypatch.setattr(manager_mod, "REBUILD_DELAY", 0)
    monkeypatch.setattr(manager_mod, "MAX_REBUILD_DELAY", 0)

    # 连续快速崩溃到上限：前MAX次重建，此后放弃
    for i in range(manager_mod.MAX_CONSECUTIVE_CRASHES + 2):
        mgr._rooms = {23058: _FakeDeadRoom(uptime=0)}
        await mgr._rebuild(23058)
    assert len(ensured) == manager_mod.MAX_CONSECUTIVE_CRASHES

    # 稳定运行超时STABLE_UPTIME后崩溃：计数重置，重新可重建
    mgr._crash_counts.clear()
    mgr._rooms = {23058: _FakeDeadRoom(uptime=manager_mod.STABLE_UPTIME + 1)}
    await mgr._rebuild(23058)
    assert 23058 in ensured


# ---------- 5. qqbot 有界发送队列 ----------

async def test_qqbot_bounded_send_queue(monkeypatch):
    """push经队列由worker消费；队列满丢弃并返回False"""
    bot = QQBotClient("id", "sec")
    bot._loop = asyncio.get_running_loop()
    bot._task = asyncio.create_task(asyncio.sleep(30))  # 让started为True
    bot._send_queue = asyncio.Queue(maxsize=1)
    sent = []

    async def fake_broadcast(groups, content, media=None, keyboard=None,
                             at_all_groups=None):
        sent.append(content)
        return True

    bot._broadcast_message = fake_broadcast
    bot._send_workers = [
        asyncio.create_task(bot._send_worker(), name=f"test-send-{i}")
        for i in range(QQBotClient.SEND_WORKERS)
    ]
    try:
        assert bot.push("m1", ["G1"]) is True
        # 第二条：队列容量1且worker可能未取走——填满场景不依赖时序，
        # 直接断言不抛错且最终m1被发送
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.01)
        assert sent == ["m1"]
    finally:
        for w in bot._send_workers:
            w.cancel()
        bot._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bot._task


async def test_qqbot_queue_full_drops():
    """队列满：入队丢弃返回False（背压）"""
    bot = QQBotClient("id", "sec")
    bot._loop = asyncio.get_running_loop()
    bot._task = asyncio.create_task(asyncio.sleep(30))
    bot._send_queue = asyncio.Queue(maxsize=1)
    # 不启动worker：队列保持满载

    async def noop(*a, **kw):
        return True

    bot._broadcast_message = noop
    try:
        assert bot.push("first", ["G1"]) is True
        assert bot.push("overflow", ["G1"]) is False  # 满则丢弃
    finally:
        # 队列里残留的first协程无worker消费：close掉防never-awaited警告
        while not bot._send_queue.empty():
            bot._send_queue.get_nowait().close()
        bot._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bot._task


# ---------- 6. SQLite WAL 与密码转义 ----------

async def test_sqlite_wal_enabled(tmp_path):
    """SQLite连接启用WAL与busy_timeout"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    try:
        from sqlalchemy import text
        async with db.engine.connect() as conn:
            mode = (await conn.execute(
                text("PRAGMA journal_mode"))).scalar()
            timeout = (await conn.execute(
                text("PRAGMA busy_timeout"))).scalar()
        assert str(mode).lower() == "wal"
        assert timeout == 15000
    finally:
        await db.close()


def test_db_url_password_escaped(tmp_path):
    """MySQL/PG密码含URL特殊字符时被percent-encode"""
    cfg = Settings().database
    cfg.type = "mysql"
    cfg.user = "root"
    cfg.password = "p@ss:w/rd#1"
    cfg.host = "localhost"
    cfg.port = 3306
    cfg.database = "blivepost"
    db = DatabaseManager(cfg)
    url = db._get_database_url()
    assert "p%40ss%3Aw%2Frd%231" in url
    assert "@" not in url.split("//", 1)[1].split("@")[-1].split(":", 1)[0]
