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
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    # 播种全局推送开关（推送设置指令"跟随全局"判定的基准）
    await svc.seed_push_switches({
        "anchor": True, "guard_achievement": True, "system_event": True,
        "room_change": False, "daily_report": False, "weekly_report": False,
        "monthly_report": False, "dynamic_monitor": False})

    class FakeAPI:
        """拦截监控登记时的主播快照查询（避免真实网络）"""

        def __init__(self):
            pass

        def get_status_info_by_uids(self, uids):
            return {int(u): {"uname": f"主播{u}", "room_id": int(u)} for u in uids}

    import blivepost.services.bilibili as bili
    monkeypatch.setattr(bili, "BilibiliAPI", FakeAPI)

    yield CommandHandler(store, FakeManager(short_map={2345: 23058}),
                         db_service=svc), store
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


async def test_push_setting_overview(handler):
    """/推送设置无房间号：本群全部绑定房间的开关概览（徽标+修改提示）"""
    ch, _ = handler
    assert "没有推送中" in await ch.handle(FakeMsg("/推送设置", "G"))
    await ch.handle(FakeMsg("/监控 23058", "G"))
    r = await ch.handle(FakeMsg("/推送设置", "G"))
    assert "本群推送设置概览" in r
    assert "房间23058" in r
    assert "天选：开" in r and "房变：关" in r  # 绑定播种继承全局
    assert "修改" in r  # 提示如何修改


async def test_push_setting_detail(handler):
    """/推送设置 房间号：7项开关逐项明细（标注跟随全局）"""
    ch, _ = handler
    await ch.handle(FakeMsg("/监控 23058", "G"))
    r = await ch.handle(FakeMsg("/推送设置 23058", "G"))
    assert "房间23058本群推送设置" in r
    assert "天选时刻：开（跟随全局）" in r
    assert "房间信息：关（跟随全局）" in r
    assert "每月结算：关（跟随全局）" in r


async def test_push_setting_toggle_and_reset(handler):
    """单项开/关切换落库生效（不动全局）；恢复清除独立设置回落全局"""
    ch, _ = handler
    db = ch.db_service
    await ch.handle(FakeMsg("/监控 23058", "G"))

    r = await ch.handle(FakeMsg("/推送设置 23058 天选 关", "G"))
    assert "已设为关" in r
    own = await db.get_group_push_setting(23058, "G")
    assert own["anchor"] is False
    assert await db.get_push_switch_enabled("anchor", True) is True  # 全局不受影响
    detail = await ch.handle(FakeMsg("/推送设置 23058", "G"))
    assert "天选时刻：关" in detail
    assert "天选时刻：关（跟随全局）" not in detail  # 已偏离全局=独立
    assert "独立" in await ch.handle(FakeMsg("/推送设置", "G"))  # 概览标记独立

    r = await ch.handle(FakeMsg("/推送设置 23058 大航海 开", "G"))
    assert "已设为开" in r
    assert (await db.get_group_push_setting(23058, "G"))["guard_achievement"] is True

    r = await ch.handle(FakeMsg("/推送设置 23058 恢复", "G"))
    assert "恢复跟随全局" in r
    assert await db.get_group_push_setting(23058, "G") is None  # 独立行已清除
    detail = await ch.handle(FakeMsg("/推送设置 23058", "G"))
    assert "天选时刻：开（跟随全局）" in detail

    # 恢复后再单项设置：先按全局播种，其余开关不被默认置关
    await ch.handle(FakeMsg("/推送设置 23058 日报 开", "G"))
    own = await db.get_group_push_setting(23058, "G")
    assert own["daily_report"] is True and own["anchor"] is True


async def test_push_setting_invalid_switch_and_missing_state(handler):
    """非法开关名给出可用列表；缺开/关参数提示用法"""
    ch, _ = handler
    await ch.handle(FakeMsg("/监控 23058", "G"))
    r = await ch.handle(FakeMsg("/推送设置 23058 未知项 开", "G"))
    assert "未知开关" in r and "天选" in r and "房间信息" in r
    r = await ch.handle(FakeMsg("/推送设置 23058 天选", "G"))
    assert "请指定开或关" in r


async def test_push_setting_unbound_room(handler):
    """房间号必须是本群已绑定房间；短号归一化后再判定"""
    ch, _ = handler
    await ch.handle(FakeMsg("/监控 23058", "G"))
    r = await ch.handle(FakeMsg("/推送设置 99999", "G"))
    assert "不是本群绑定" in r
    r = await ch.handle(FakeMsg("/推送设置 2345", "G"))  # 短号→23058已绑定
    assert "房间23058本群推送设置" in r


async def test_push_setting_permission_denied(handler):
    """推送设置与控制指令同一权限门槛（名单未配置不限制）"""
    ch, store = handler
    gated = CommandHandler(store, FakeManager(), db_service=ch.db_service,
                           group_admin_openids=["ADMIN"])
    assert "无权限" in await gated.handle(FakeMsg("/推送设置", "G"))
    assert "无权限" in await gated.handle(FakeMsg("/推送设置 23058 天选 开", "G"))
    r = await gated.handle(FakeMsg("/推送设置", "G", sender="ADMIN"))
    assert "无权限" not in r  # 名单内放行


async def test_push_setting_regex_forms(handler):
    """正则匹配：带/不带房间号、全角斜杠、全名与开启动词均可；无斜杠不触发"""
    ch, _ = handler
    await ch.handle(FakeMsg("/监控 23058", "G"))
    assert await ch.handle(FakeMsg("推送设置 23058", "G")) is None  # 无斜杠不触发
    r = await ch.handle(FakeMsg("／推送设置 23058", "G"))  # 全角斜杠
    assert "房间23058本群推送设置" in r
    r = await ch.handle(FakeMsg("/推送设置 23058 天选时刻 开启", "G"))
    assert "已设为开" in r
