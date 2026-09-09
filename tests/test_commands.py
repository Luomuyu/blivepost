# -*- coding: utf-8 -*-
"""群命令测试"""

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.services.commands import CommandHandler


class FakeInfo:
    def __init__(self, rid, real=None):
        self.uid = rid * 10 + 1
        self.name = f"主播{rid}"
        self.room_id = real or rid


class FakeManager:
    def __init__(self, short_map=None):
        self.rooms = set()
        self.info_map = short_map or {}

    async def get_room_info(self, rid):
        return FakeInfo(rid, self.info_map.get(rid))

    async def ensure(self, rid):
        created = rid not in self.rooms
        self.rooms.add(rid)
        return created, FakeInfo(rid) if created else None

    async def remove(self, rid):
        if rid in self.rooms:
            self.rooms.discard(rid)
            return True
        return False

    def has(self, rid):
        return rid in self.rooms

    def room_ids(self):
        return list(self.rooms)


class FakeMsg:
    def __init__(self, content, group, sender="TEST_SENDER"):
        self.author = type("A", (), {"user_openid": sender})()
        self.content = content
        self.group_openid = group

    async def reply(self, **kw):
        pass


@pytest.fixture
async def handler(tmp_path, monkeypatch):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()

    class FakeAPI:
        """拦截监控登记时的主播快照查询（避免真实网络）"""

        def __init__(self):
            pass

        def get_status_info_by_uids(self, uids):
            return {int(u): {"uname": f"主播{u}", "room_id": int(u)} for u in uids}

    import blivepost.services.bilibili as bili
    monkeypatch.setattr(bili, "BilibiliAPI", FakeAPI)

    yield CommandHandler(store, FakeManager(short_map={2345: 23058})), store
    await db.close()


async def test_help_command(handler):
    ch, _ = handler
    r = await ch.handle(FakeMsg("/帮助", "G"))
    assert "指令" in r
    assert await ch.handle(FakeMsg("/指令", "G")) == r


async def test_non_command_ignored(handler):
    ch, _ = handler
    assert await ch.handle(FakeMsg("你好呀", "G")) is None
    assert await ch.handle(FakeMsg("监控 23058", "G")) is None  # 无斜杠不触发
    assert await ch.handle(FakeMsg("/监控 abc", "G")) is None


async def test_monitor_normalizes_short_id(handler):
    """/监控直接登记房间并添加本群为推送目标，短号归一化"""
    ch, store = handler
    r = await ch.handle(FakeMsg("/监控 2345", "G"))
    assert "23058" in r and "归一化" in r
    assert ch.monitor_manager.rooms == {23058}
    assert store.get_binding("G", 23058) is not None
    assert store.get_binding("G", 2345) is None


async def test_unmonitor_room_not_seen(handler):
    """/取消监控未知房间：归一化后移除（无推送群）仍正常回复"""
    ch, _ = handler
    r = await ch.handle(FakeMsg("/取消监控 23058", "G"))
    assert "已取消监控" in r


async def test_monitor_adds_push_target_per_group(handler):
    """多群监控同一房间：各自持推送目标，单进程共享"""
    ch, store = handler
    r1 = await ch.handle(FakeMsg("/监控 23058", "G1"))
    r2 = await ch.handle(FakeMsg("/监控 23058", "G2"))
    assert "已开始监控" in r1 and "已开始监控" in r2
    assert ch.monitor_manager.rooms == {23058}
    assert sorted(store.get_monitored_groups(23058)) == ["G1", "G2"]


async def test_monitor_via_short_id(handler):
    ch, _ = handler
    r = await ch.handle(FakeMsg("/监控 2345", "G"))
    assert "已开始监控" in r
    assert ch.monitor_manager.rooms == {23058}


async def test_unmonitor_last_group_stops_process(handler):
    ch, store = handler
    await ch.handle(FakeMsg("/监控 23058", "G"))
    r = await ch.handle(FakeMsg("/取消监控 23058", "G"))
    assert "监控已停止" in r
    assert 23058 not in ch.monitor_manager.rooms
    assert store.get_binding("G", 23058) is None  # 推送目标已移除


async def test_unmonitor_keeps_process_while_others_monitor(handler):
    ch, _ = handler
    await ch.handle(FakeMsg("/监控 23058", "G1"))
    await ch.handle(FakeMsg("/监控 23058", "G2"))
    r = await ch.handle(FakeMsg("/取消监控 23058", "G1"))
    assert "监控已停止" not in r
    assert 23058 in ch.monitor_manager.rooms


async def test_status_and_my_bindings(handler):
    ch, _ = handler
    assert "没有监控中" in await ch.handle(FakeMsg("/状态", "G"))
    await ch.handle(FakeMsg("/监控 23058", "G"))
    status = await ch.handle(FakeMsg("/状态", "G"))
    assert "23058" in status and "监控运行中" in status
    bindings = await ch.handle(FakeMsg("/绑定列表", "G"))
    assert "23058" in bindings and "监控中" in bindings
