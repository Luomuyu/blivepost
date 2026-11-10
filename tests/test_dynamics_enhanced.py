# -*- coding: utf-8 -*-
"""动态增强：摘要/图片提取、直播联动、开关测试；
另含命令小项回归（推送设置漏开关名引导/异常通用回显）"""

import asyncio

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.services.commands import CommandHandler
from blivepost.services.dynamics import DynamicManager


def test_dynamic_monitor_default_off():
    """动态监控默认关闭"""
    s = Settings()
    assert s.message.dynamic_monitor is False


def _fake_feed_response(items):
    class FakeResp:
        def json(self):
            return {"code": 0, "data": {"items": items}}

    return FakeResp()


def test_poll_once_extracts_summary_and_image(monkeypatch):
    """轮询提取摘要与首图"""
    from blivepost.services import dynamics as dyn

    items = [{
        "id_str": "900",
        "modules": {
            "module_author": {"name": "UP甲", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": {"text": "这是一条动态内容"},
                "major": {"draw": {"items": [{"src": "http://img1.png"},
                                             {"src": "http://img2.png"}]}},
            },
        },
    }]
    monkeypatch.setattr(dyn.requests, "get", lambda *a, **kw: _fake_feed_response(items))
    data = dyn._requests_fetch(111)
    infos = dyn._parse_feed_data(data, 111)
    assert isinstance(infos, list) and len(infos) == 1
    info = infos[0]
    assert info["dynamic_id"] == "900"
    assert info["summary"] == "这是一条动态内容"
    assert info["image"] == "http://img1.png"  # 仅取首图
    assert info["author"] == "UP甲"


def test_poll_once_video_cover_fallback(monkeypatch):
    """无图片动态时取投稿视频封面"""
    from blivepost.services import dynamics as dyn

    items = [{
        "id_str": "901",
        "modules": {
            "module_author": {"name": "UP乙", "pub_ts": 1700000001},
            "module_dynamic": {
                "desc": {"text": "投稿了新视频"},
                "major": {"archive": {"cover": "http://cover.jpg"}},
            },
        },
    }]
    monkeypatch.setattr(dyn.requests, "get", lambda *a, **kw: _fake_feed_response(items))
    infos = dyn._parse_feed_data(dyn._requests_fetch(222), 222)
    assert isinstance(infos, list) and len(infos) == 1
    assert infos[0]["image"] == "http://cover.jpg"
    assert infos[0]["summary"] == "投稿了新视频"


def test_fetch_live_states_parse(monkeypatch):
    """批量直播状态接口解析"""
    from blivepost.services import dynamics as dyn

    class FakeResp:
        def json(self):
            return {"code": 0, "data": {"111": {"live_status": 1, "room_id": 777, "title": "T"}}}

    monkeypatch.setattr(dyn.requests, "post", lambda *a, **kw: FakeResp())
    states = dyn._fetch_live_states([111])
    assert states[111]["live"] is True and states[111]["room_id"] == 777

    class OffResp:
        def json(self):
            return {"code": 0, "data": {"111": {"live_status": 0}}}

    monkeypatch.setattr(dyn.requests, "post", lambda *a, **kw: OffResp())
    assert dyn._fetch_live_states([111])[111]["live"] is False


def test_dynamic_manager_disabled_short_circuits(tmp_path):
    """开关关闭时ensure不创建进程"""
    m = DynamicManager(Settings(), push_queue=None, enabled=False)

    async def run():
        return await m.ensure(12345)

    created, _ = asyncio.run(run())
    assert created is False
    assert m.up_uids() == []


class FakeAuthor:
    def __init__(self, openid="TEST_SENDER"):
        self.user_openid = openid


class FakeMsg:
    def __init__(self, content, group, sender="TEST_SENDER"):
        self.author = FakeAuthor(sender)
        self.content = content
        self.group_openid = group

    async def reply(self, **kw):
        self.replies.append(kw.get("content", ""))


class FakeInfo2:
    def __init__(self, rid):
        self.uid = 1
        self.name = f"主播{rid}"
        self.room_id = rid


class FakeManager2:
    async def get_room_info(self, rid):
        return FakeInfo2(rid)

    async def ensure(self, rid):
        return True, None

    async def remove(self, rid):
        return True


async def test_bind_dynamic_blocked_when_disabled(tmp_path):
    """开关关闭时/绑定动态回复未启用且不写绑定"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    disabled = DynamicManager(settings, None, enabled=False)
    ch = CommandHandler(store, FakeManager2(), dynamic_manager=disabled)

    msg = FakeMsg("/绑定动态 12345", "G")
    r = await ch.handle(msg)
    assert "动态监控未启用" in r
    assert store.get_dynamic_groups(12345) == []
    await db.close()


async def test_consumer_dynamic_with_summary_and_image(tmp_path):
    """带摘要和图片的动态：富媒体推送且内容含摘要"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    await store.bind_dynamic("G1", 111, "UP甲")

    ps = PushServiceForTest()
    ps.binding_store = store

    from blivepost.core.manager import PushQueueConsumer
    consumer = PushQueueConsumer(None, ps, None, binding_store=store)
    await consumer._handle_dynamic({
        "type": "dynamic", "up_uid": 111, "dynamic_id": "900",
        "author": "UP甲", "pub_ts": 1700000000,
        "summary": "今天直播预告", "image": "http://img.png",
    })

    assert len(ps.bot.media) == 1
    assert ps.bot.media[0][0] == "http://img.png"
    assert "今天直播预告" in ps.bot.media[0][1]
    assert ps.bot.media[0][2] == ["G1"]
    # 昵称回填
    assert store.get_dynamic_up_name(111) == "UP甲"
    await db.close()


class SimpleBot:
    """记录push与push_media调用的机器人桩"""

    def __init__(self):
        self.text = []
        self.media = []

    def push(self, content, groups):
        self.text.append((content, list(groups)))
        return True

    def push_media(self, image_url, content, groups):
        self.media.append((image_url, content, list(groups)))
        return True

    async def broadcast(self, groups, content):
        self.push(content, groups)
        return True


class PushServiceForTest:
    def __init__(self):
        from blivepost.services.push import PushService
        self._inner = PushService(Settings())
        self.templates = self._inner.templates
        self._render = self._inner._render
        self.bot = SimpleBot()
        self.bot_client = self.bot

    async def broadcast(self, groups, content):
        self.bot.push(content, groups)
        return True


# ==================== 命令小项回归（推送设置引导/异常通用回显） ====================


async def _pushset_handler(tmp_path):
    """构造带已绑定房间的命令处理器（推送设置/异常回显测试用）"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    await store.bind("G", 23058, 1)
    await store.set_monitored("G", 23058, True)
    return CommandHandler(store, FakeManager2(), db_service=svc), db


async def test_push_setting_state_word_without_switch_guides_usage(tmp_path):
    """/推送设置 房间号 开（漏开关名，开/关落到arg位置）：返回用法引导
    而非"未知开关：开"；真正的未知开关名仍走原提示"""
    ch, db = await _pushset_handler(tmp_path)
    try:
        for word in ("开", "关", "开启", "关闭"):
            r = await ch.handle(FakeMsg(f"/推送设置 23058 {word}", "G"))
            assert "缺少开关名" in r
            assert "示例：/推送设置 23058 天选 开" in r
            assert "未知开关" not in r
        r = await ch.handle(FakeMsg("/推送设置 23058 未知项 开", "G"))
        assert "未知开关" in r
    finally:
        await db.close()


async def test_command_failure_returns_generic_message(tmp_path, monkeypatch):
    """命令执行异常：群内只回通用文案，内部异常详情不外泄（仅记日志）"""
    ch, db = await _pushset_handler(tmp_path)
    try:
        # 控制类命令内部异常（订阅动态ensure抛错）
        class BrokenDynamicManager:
            enabled = True

            async def ensure(self, uid, baseline_first=False):
                raise RuntimeError("内部错误boom")

        ch.dynamic_manager = BrokenDynamicManager()
        r = await ch.handle(FakeMsg("/绑定动态 12345", "G"))
        assert r == "命令执行失败，请稍后重试或联系管理员"
        assert "boom" not in r  # 内部异常不回显

        # 推送设置内部异常：同样通用文案
        async def broken_push_setting(*a, **kw):
            raise RuntimeError("内部错误boom2")

        monkeypatch.setattr(ch, "_push_setting", broken_push_setting)
        r = await ch.handle(FakeMsg("/推送设置 23058 天选 开", "G"))
        assert r == "命令执行失败，请稍后重试或联系管理员"
        assert "boom" not in r
    finally:
        await db.close()
