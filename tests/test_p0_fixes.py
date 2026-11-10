# -*- coding: utf-8 -*-
"""P0修复回归测试（第七批审查修复）

覆盖五个已确认缺陷的回归防护：
1. 消费者动态/开播提醒推送不得 await 同步的 bot.broadcast（原TypeError必抛）
2. /绑定 /解绑 已移除：正则不得再捕获（原落入else被当作"取消监控"反向执行）
3. 退群清理：房间仍被其他群监控时不得停止监控任务
4. BLIVEPOST_WEB_HOST 环境变量覆盖（原ENV_MAP缺键，Docker端口映射失效）
5. main_async 启动早期异常时 finally 不因 maintainer 未定义崩溃（预清名单）
6. 退群清理扩展（第九轮审查遗留项）：动态订阅解绑与轮询进程按需停止、
   @全体配额记录清理
"""

import asyncio

from blivepost.config import apply_env_overrides
from blivepost.config.settings import Settings
from blivepost.services.commands import _CMD_RE
from blivepost.services.contacts import ContactEventHandler
from blivepost.services.push import PushService


class SyncBroadcastBot:
    """与真实QQBotClient一致的桩：broadcast为同步方法（返回bool）

    旧测试桩把broadcast写成async导致await失配bug无法暴露——
    本桩刻意保持同步签名作回归防线。
    """

    def __init__(self):
        self.broadcast_calls = []
        self.media_calls = []

    def push(self, content, groups, keyboard=None, media=None):
        return True

    def push_media(self, image_url, content, groups, keyboard=None):
        self.media_calls.append((image_url, content, list(groups)))
        return True

    def broadcast(self, groups, content, keyboard=None, at_all_groups=None):
        self.broadcast_calls.append((list(groups), content))
        return True


def _make_consumer(bot):
    from blivepost.core.manager import PushQueueConsumer

    class Store:
        def get_dynamic_groups(self, up_uid):
            return ["G1", "G2"]

        def get_dynamic_up_name(self, up_uid):
            return "UP甲"

    ps = PushService(Settings())
    ps.bot_client = bot
    return PushQueueConsumer(None, ps, None, binding_store=Store())


def test_consumer_dynamic_text_uses_sync_broadcast():
    """纯文本动态推送：同步broadcast不得被await（原TypeError致100%失败）"""
    bot = SyncBroadcastBot()
    consumer = _make_consumer(bot)
    asyncio.run(consumer._handle_dynamic({
        "type": "dynamic", "up_uid": 111, "dynamic_id": "900",
        "author": "UP甲", "pub_ts": 1700000000, "image": "",
    }))
    assert len(bot.broadcast_calls) == 1
    assert bot.broadcast_calls[0][0] == ["G1", "G2"]
    assert "UP甲" in bot.broadcast_calls[0][1]


def test_consumer_up_live_uses_sync_broadcast():
    """UP开播提醒：同步broadcast不得被await"""
    bot = SyncBroadcastBot()
    consumer = _make_consumer(bot)
    asyncio.run(consumer._handle_up_live({
        "type": "up_live", "up_uid": 111, "title": "标题", "room_id": 23058,
    }))
    assert len(bot.broadcast_calls) == 1
    assert "23058" in bot.broadcast_calls[0][1]


def test_cmd_re_no_removed_bind_commands():
    """/绑定 /解绑 指令已移除：正则不得匹配（原落入else被当作取消监控执行）"""
    assert _CMD_RE.search("/绑定 23058") is None
    assert _CMD_RE.search("/解绑 23058") is None
    assert _CMD_RE.search("／绑定：23058") is None
    # 存留指令不受影响
    assert _CMD_RE.search("/监控 23058").group(1) == "监控"
    assert _CMD_RE.search("/取消监控 23058").group(1) == "取消监控"
    assert _CMD_RE.search("/绑定动态 111").group(1) == "绑定动态"
    assert _CMD_RE.search("/收入 23058 周").group(1) == "收入"


class _BindingStoreStub:
    def __init__(self, room_groups=None, dynamic_ups=None):
        # room_id -> 该房间仍处于监控绑定状态的群列表
        self._room_groups = room_groups or {}
        # up_uid -> 仍订阅该UP主的群列表
        self._dynamic_ups = dynamic_ups or {}
        self.unbind_calls = []
        self.unbind_dynamic_calls = []

    def get_group_bindings(self, group_openid):
        from types import SimpleNamespace
        return [SimpleNamespace(room_id=rid)
                for rid, groups in self._room_groups.items() if group_openid in groups]

    def get_monitored_groups(self, room_id):
        return self._room_groups.get(room_id, [])

    async def unbind(self, group_openid, room_id):
        self.unbind_calls.append((group_openid, room_id))
        groups = self._room_groups.get(room_id)
        if group_openid in groups:
            groups.remove(group_openid)

    def get_group_dynamic_ups(self, group_openid):
        return sorted(uid for uid, groups in self._dynamic_ups.items()
                      if group_openid in groups)

    def get_dynamic_groups(self, up_uid):
        return list(self._dynamic_ups.get(up_uid, []))

    async def unbind_dynamic(self, group_openid, up_uid):
        self.unbind_dynamic_calls.append((group_openid, up_uid))
        groups = self._dynamic_ups.get(up_uid)
        if groups and group_openid in groups:
            groups.remove(group_openid)
            return True
        return False


class _MonitorManagerStub:
    def __init__(self):
        self.removed = []

    async def remove(self, room_id):
        self.removed.append(room_id)
        return True


class _DynamicManagerStub:
    def __init__(self):
        self.removed = []

    async def remove(self, up_uid):
        self.removed.append(up_uid)
        return True


class _DbServiceStub:
    def __init__(self):
        self.cleared_at_all = []

    async def clear_group_at_all(self, group_openid):
        self.cleared_at_all.append(group_openid)
        return True


class _ContactStoreStub:
    async def save_group(self, group_openid, del_flag=0, receive=1):
        pass


def test_on_group_del_keeps_room_monitored_by_others():
    """退群清理：房间仍被其他群监控时不停止监控任务"""
    store = _BindingStoreStub({23058: ["GA", "GB"]})
    mm = _MonitorManagerStub()
    handler = ContactEventHandler(_ContactStoreStub(), store, mm)
    asyncio.run(handler.on_group_del("GA"))
    assert store.unbind_calls == [("GA", 23058)]
    assert mm.removed == []  # GB仍监控，不得移除


def test_on_group_del_stops_room_when_no_others():
    """退群清理：无其他监控群时停止监控任务"""
    store = _BindingStoreStub({23058: ["GA"]})
    mm = _MonitorManagerStub()
    handler = ContactEventHandler(_ContactStoreStub(), store, mm)
    asyncio.run(handler.on_group_del("GA"))
    assert mm.removed == [23058]


def test_on_group_del_cleans_dynamic_subscription():
    """退群清理：该群动态订阅被解绑，无他群订阅的UP停止轮询进程"""
    store = _BindingStoreStub(dynamic_ups={111: ["GA"], 222: ["GA", "GB"]})
    dm = _DynamicManagerStub()
    handler = ContactEventHandler(_ContactStoreStub(), store, _MonitorManagerStub(),
                                  dynamic_manager=dm)
    asyncio.run(handler.on_group_del("GA"))
    # 该群订阅的全部UP均解绑
    assert sorted(store.unbind_dynamic_calls) == [("GA", 111), ("GA", 222)]
    # 222仍被GB订阅保留轮询；111无他群订阅停止轮询
    assert dm.removed == [111]


def test_on_group_del_keeps_dynamic_polling_when_others_subscribe():
    """退群清理：他群仍订阅同一UP主时保留轮询进程（仅解绑本群）"""
    store = _BindingStoreStub(dynamic_ups={111: ["GA", "GB"]})
    dm = _DynamicManagerStub()
    handler = ContactEventHandler(_ContactStoreStub(), store, _MonitorManagerStub(),
                                  dynamic_manager=dm)
    asyncio.run(handler.on_group_del("GA"))
    assert store.unbind_dynamic_calls == [("GA", 111)]
    assert dm.removed == []  # GB仍订阅，不得停止
    assert store.get_dynamic_groups(111) == ["GB"]


def test_on_group_del_clears_at_all_records():
    """退群清理：该群的@全体配额记录被删除（db_service为空时跳过不报错）"""
    db = _DbServiceStub()
    handler = ContactEventHandler(_ContactStoreStub(), _BindingStoreStub(),
                                  _MonitorManagerStub(), db_service=db)
    asyncio.run(handler.on_group_del("GA"))
    assert db.cleared_at_all == ["GA"]
    # db_service未注入时不抛异常（仅跳过清理）
    handler_no_db = ContactEventHandler(_ContactStoreStub(), _BindingStoreStub(),
                                        _MonitorManagerStub())
    asyncio.run(handler_no_db.on_group_del("GA"))


def test_env_web_host_override():
    """BLIVEPOST_WEB_HOST覆盖message.web_host（Docker容器监听0.0.0.0依赖此键）"""
    settings = Settings()
    assert settings.message.web_host == "127.0.0.1"
    n = apply_env_overrides(settings, {"BLIVEPOST_WEB_HOST": "0.0.0.0"})
    assert n == 1
    assert settings.message.web_host == "0.0.0.0"
