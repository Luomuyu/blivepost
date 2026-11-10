# -*- coding: utf-8 -*-
"""推送服务测试（CQ转换、绑定路由、富媒体）"""

import asyncio

import pytest

from blivepost.config.settings import Settings
from blivepost.models.live import LiveRoomInfo
from blivepost.services.push import PushService, convert_cq_message
from blivepost.services.bindings import BindingStore


class FakeBot:
    def __init__(self, media=False):
        self.calls = []
        self.media_calls = []
        self.support_media = media

    def push(self, content, groups):
        self.calls.append((content, list(groups)))
        return True

    def push_media(self, image_url, content, groups):
        self.media_calls.append((image_url, content, list(groups)))
        return True


class TextOnlyBot:
    """不支持富媒体的机器人（无push_media方法）"""

    def __init__(self):
        self.calls = []

    def push(self, content, groups):
        self.calls.append((content, list(groups)))
        return True


def make_info(room_id=23058, cover="http://cover.jpg"):
    return LiveRoomInfo(uid=1, name="主播", title="标题", cover=cover,
                        room_id=room_id, online="1")


def test_convert_cq_at_all_removed():
    assert convert_cq_message("[CQ:at,qq=all] hi") == "hi"


def test_convert_cq_image_to_url():
    text = convert_cq_message("看[CQ:image,cache=0,file=http://x.png]")
    assert "http://x.png" in text
    assert "[CQ:" not in text


def test_convert_cq_mixed():
    text = convert_cq_message("[CQ:at,qq=all] hi[CQ:image,cache=0,file=http://x.png]")
    assert text == "hi\nhttp://x.png"


def test_convert_cq_plain_untouched():
    assert convert_cq_message("普通消息") == "普通消息"


async def test_resolve_groups_from_bindings(tmp_path):
    """推送目标 = 绑定列表中监控状态的群"""
    from blivepost.database import DatabaseManager, DatabaseService

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()

    await store.bind("GRP_A", 23058, 1)
    await store.bind("GRP_B", 23058, 1)
    await store.bind("GRP_A", 99, 2)
    await store.set_monitored("GRP_A", 23058, True)

    ps = PushService(settings)
    ps.binding_store = store
    assert ps._resolve_groups(23058) == ["GRP_A"]
    assert ps._resolve_groups(99) == []
    assert ps._resolve_groups(None) == []
    await db.close()


async def test_send_message_routes_by_room(tmp_path):
    from blivepost.database import DatabaseManager, DatabaseService

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    await store.bind("GRP_A", 23058, 1)
    await store.set_monitored("GRP_A", 23058, True)

    ps = PushService(settings)
    ps.binding_store = store
    bot = FakeBot()
    ps.bot_client = bot

    assert ps.push_anchor("23058", "奖品", "弹幕", "要求") is True
    assert "天选" in bot.calls[0][0] and bot.calls[0][1] == ["GRP_A"]
    # 未绑定的房间不发送
    assert ps.push_anchor("777", "奖品", "弹幕", "要求") is False
    assert len(bot.calls) == 1
    await db.close()


async def test_push_live_start_media_with_fallback(tmp_path):
    """有封面且机器人支持富媒体时走媒体推送"""
    from blivepost.database import DatabaseManager, DatabaseService

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    await store.bind("GRP_A", 23058, 1)
    await store.set_monitored("GRP_A", 23058, True)

    ps = PushService(settings)
    ps.binding_store = store

    bot = FakeBot(media=True)
    ps.bot_client = bot
    ps.push_live_start(make_info(cover="http://cover.jpg"))
    assert len(bot.media_calls) == 1
    assert bot.media_calls[0][0] == "http://cover.jpg"
    assert "开播" in bot.media_calls[0][1]

    # 不支持富媒体的机器人回退文本（含封面URL）
    bot2 = TextOnlyBot()
    ps.bot_client = bot2
    ps.push_live_start(make_info(cover="http://cover.jpg"))
    assert len(bot2.calls) == 1
    assert "http://cover.jpg" in bot2.calls[0][0]

    # 无封面走纯文本
    bot3 = FakeBot(media=True)
    ps.bot_client = bot3
    ps.push_live_start(make_info(cover=""))
    assert len(bot3.media_calls) == 0
    assert len(bot3.calls) == 1
    await db.close()


# ==================== 开播推送按钮 ====================


def test_live_enter_keyboard_structure():
    """按钮结构对齐官方v2群消息接口文档"""
    from blivepost.services.push import live_enter_keyboard
    kb = live_enter_keyboard(9646546)
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["render_data"]["label"] == "进入直播间"
    assert btn["render_data"]["visited_label"] == "跳转中…"
    assert btn["render_data"]["style"] == 1  # 蓝色线框（官方style取值：0灰/1蓝线框）
    assert btn["action"]["permission"]["type"] == 2  # 所有人可点击
    assert btn["action"]["type"] == 0  # 跳转按钮
    assert btn["action"]["data"] == "https://live.bilibili.com/9646546"
    assert "unsupport_tips" in btn["action"]
    # 非本文档字段不出现（clicked_data/at_bot_show来自其他版本文档）
    assert "clicked_data" not in btn["action"]
    assert "at_bot_show" not in btn["action"]
    assert "visited_color" not in btn["render_data"]


class KeyboardBot:
    """支持keyboard参数的机器人（记录收到的keyboard）"""

    def __init__(self, media=False):
        self.kb_calls = []
        self.media_kb_calls = []
        self.support_media = media

    def push(self, content, groups, keyboard=None):
        self.kb_calls.append(keyboard)
        return True

    def push_media(self, image_url, content, groups, keyboard=None):
        self.media_kb_calls.append(keyboard)
        return True


class PushMessageBot:
    """官方bot形态：push_message统一入口（Markdown优先降级链在bot层）"""

    def __init__(self):
        self.calls = []

    def push_message(self, content, groups, keyboard=None, media=None):
        self.calls.append({"content": content, "media": media,
                           "groups": list(groups), "keyboard": keyboard})
        return True


class MarkdownBot:
    """仅支持Markdown推送的自定义bot（无push_message）"""

    def __init__(self):
        self.md_calls = []
        self.media_calls = []

    def push_markdown(self, content, groups, keyboard=None):
        self.md_calls.append((content, keyboard))
        return True

    def push_media(self, image_url, content, groups):
        self.media_calls.append((image_url, content))
        return True

    def push(self, content, groups):
        return True


async def _live_start_env(tmp_path, bot):
    from blivepost.database import DatabaseManager, DatabaseService

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.bind("G1", 9646546, 1)
    await store.set_monitored("G1", 9646546, True)
    ps = PushService(settings)
    ps.binding_store = store
    ps.bot_client = bot
    return ps, db


async def test_push_live_start_official_bot_uses_ladder(tmp_path):
    """官方bot：开播推送走push_message统一入口并携带按钮（协议链接）"""
    bot = PushMessageBot()
    ps, db = await _live_start_env(tmp_path, bot)
    assert ps.push_live_start(make_info(room_id=9646546)) is True
    call = bot.calls[0]
    btn = call["keyboard"]["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["data"] == "https://live.bilibili.com/9646546"
    assert btn["render_data"]["style"] == 1  # 蓝色线框
    assert call["media"] == "http://cover.jpg"  # 封面交由bot层嵌Markdown
    await db.close()


async def test_push_live_start_markdown_bot(tmp_path):
    """自定义bot支持push_markdown：按钮经Markdown链路传递，封面嵌图免模板"""
    bot = MarkdownBot()
    ps, db = await _live_start_env(tmp_path, bot)
    assert ps.push_live_start(make_info(room_id=9646546)) is True
    assert len(bot.md_calls) == 1
    md_content, kb = bot.md_calls[0]
    btn = kb["content"]["rows"][0]["buttons"][0]
    assert btn["action"]["data"] == "https://live.bilibili.com/9646546"
    # 原生图片语法嵌封面（官方文档：![文字 #宽px #高px](URL)）
    assert "![封面 #640px #360px](http://cover.jpg)" in md_content
    assert bot.media_calls == []  # Markdown成功不降级富媒体
    await db.close()


async def test_push_live_start_keyboard_bot_falls_back_media(tmp_path):
    """bot无push_live_start/push_markdown：走push_media路径且keyboard透传——
    官方bot经push→markdown降级链渲染按钮（真机2026-11-10反馈：不透传则
    按钮永不渲染）；纯富媒体bot忽略keyboard无副作用"""
    bot = KeyboardBot(media=True)
    ps, db = await _live_start_env(tmp_path, bot)
    assert ps.push_live_start(make_info(room_id=9646546)) is True
    assert len(bot.media_kb_calls) == 1
    assert bot.media_kb_calls[0] is not None  # keyboard透传给bot层
    assert "rows" in bot.media_kb_calls[0]["content"]
    await db.close()


async def test_push_keyboard_custom_bot_compat(tmp_path):
    """不支持keyboard参数的自定义bot仍正常工作（旧签名回退）"""
    from blivepost.database import DatabaseManager, DatabaseService

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.bind("G1", 23058, 1)
    await store.set_monitored("G1", 23058, True)
    bot = FakeBot()  # 旧签名：push(content, groups)
    ps = PushService(settings)
    ps.binding_store = store
    ps.bot_client = bot
    assert ps.send_message("hello", room_id=23058, keyboard={"content": {"rows": []}}) is True
    assert bot.calls and bot.calls[0][1] == ["G1"]
    await db.close()


# ==================== 心跳失败看门狗 ====================


def test_heartbeat_watchdog_stops_client_after_threshold():
    """连续心跳失败达阈值→stop()客户端（join返回→watcher重建）；成功则计数清零"""
    import asyncio
    import sys

    from pathlib import Path

    import pytest

    vendor_blivedm = Path(__file__).resolve().parents[1] / "_vendor" / "blivedm"
    if not vendor_blivedm.exists():
        pytest.skip("vendor blivedm未下载（运行时自动下载）")
    if str(vendor_blivedm.parent) not in sys.path:
        sys.path.insert(0, str(vendor_blivedm.parent))

    from blivepost.vendor import patch as vendor_patch

    vendor_patch.apply_patch()  # 幂等
    import blivedm.clients.ws_base as ws_base

    assert getattr(ws_base.WebSocketClientBase, "_heartbeat_watchdog_patched", False)

    class FakeWS:
        closed = False

        async def send_bytes(self, data):
            raise ConnectionResetError("Cannot write to closing transport")

    class FakeClient:
        room_id = 1
        _websocket = FakeWS()
        _heartbeat_fail_count = 0
        stopped = False

        def _make_packet(self, data, op):
            return b""

        def stop(self):
            self.stopped = True

    fake = FakeClient()
    hb = ws_base.WebSocketClientBase._send_heartbeat

    async def run():
        await hb(fake)  # 第1次失败
        assert fake.stopped is False and fake._heartbeat_fail_count == 1
        await hb(fake)  # 第2次失败
        assert fake.stopped is False and fake._heartbeat_fail_count == 2
        await hb(fake)  # 第3次失败 → 达阈值
        assert fake.stopped is True and fake._websocket is None

    asyncio.run(run())

    # 成功后计数清零
    class OkClient(FakeClient):
        class _WS:
            closed = False

            async def send_bytes(self, data):
                return None

    ok = OkClient()
    ok._websocket = ok._WS()

    async def run_ok():
        await hb(ok)
        await hb(ok)

    asyncio.run(run_ok())
    assert ok._heartbeat_fail_count == 0 and ok.stopped is False


# ==================== Markdown优先降级链 ====================


async def test_broadcast_message_ladder_markdown_first_text_fallback():
    """降级链：Markdown优先→文字兜底（图片URL保留在文本），富媒体不再参与"""
    from blivepost.services.qqbot import QQBotClient

    c = object.__new__(QQBotClient)
    calls = []

    async def md(group, content, keyboard=None):
        calls.append(("md", content))
        return False

    async def media(group, image_url, content, keyboard=None):
        calls.append(("media", image_url))
        return True

    async def text(group, content, keyboard=None):
        calls.append(("text", content))
        return True

    c.send_group_markdown = md
    c.send_group_media = media
    c.send_group_message = text

    sent = await c._broadcast_message(["G1"], "开播啦", media="http://img.jpg")
    assert sent is True
    assert [k for k, _ in calls] == ["md", "text"]  # 富媒体中间层已移除
    assert "![封面 #640px #360px](http://img.jpg)" in calls[0][1]  # Markdown原生嵌图
    assert "图片：http://img.jpg" in calls[1][1]  # 文字兜底保留图片URL


async def test_broadcast_message_all_fail():
    """Markdown与文字均失败时返回False（不抛异常）"""
    from blivepost.services.qqbot import QQBotClient

    c = object.__new__(QQBotClient)

    async def fail(*a, **k):
        return False

    c.send_group_markdown = fail
    c.send_group_media = fail
    c.send_group_message = fail
    sent = await c._broadcast_message(["G1"], "hi", media="http://img.jpg")
    assert sent is False


# ==================== 推送上下文并发隔离（ContextVar） ====================


class StaticBindingStore:
    """固定映射的绑定存储桩（并发测试用，免DB）"""

    def __init__(self, mapping):
        self._mapping = mapping

    def get_monitored_groups(self, room_id):
        return list(self._mapping.get(int(room_id), []))


async def test_groups_context_isolated_between_concurrent_tasks():
    """两个协程并发进入不同_groups上下文、交错await：各自解析的目标群互不串扰"""
    ps = PushService(Settings())
    ps.binding_store = StaticBindingStore({23058: ["G1", "G2", "G3"]})

    entered = []
    both_inside = asyncio.Event()

    async def worker(allowed):
        with ps._groups(allowed):
            seen_inside = (ps._groups_override, ps._resolve_groups(23058))
            entered.append(allowed)
            if len(entered) == 2:
                both_inside.set()
            await both_inside.wait()  # 两个协程都已进入各自上下文后再解析
            return seen_inside, (ps._groups_override, ps._resolve_groups(23058))

    (in1, after1), (in2, after2) = await asyncio.gather(
        worker(["G1"]), worker(["G2"])
    )
    assert in1 == (["G1"], ["G1"]) and after1 == (["G1"], ["G1"])
    assert in2 == (["G2"], ["G2"]) and after2 == (["G2"], ["G2"])
    # 退出上下文后恢复（无覆盖=按绑定解析全量）
    assert ps._groups_override is None
    assert ps._resolve_groups(23058) == ["G1", "G2", "G3"]


async def test_switch_context_isolated_between_concurrent_tasks():
    """两个协程并发进入不同_switch上下文：开关tag互不覆盖，退出后恢复"""
    ps = PushService(Settings())

    entered = []
    both_inside = asyncio.Event()

    async def worker(name):
        with ps._switch(name):
            seen_inside = ps._switch_tag
            entered.append(name)
            if len(entered) == 2:
                both_inside.set()
            await both_inside.wait()  # 两个协程都已进入各自上下文后再读取
            return seen_inside, ps._switch_tag

    (in1, after1), (in2, after2) = await asyncio.gather(
        worker("anchor"), worker("system_event")
    )
    assert in1 == "anchor" and after1 == "anchor"
    assert in2 == "system_event" and after2 == "system_event"
    # 退出上下文后恢复
    assert ps._switch_tag is None


def test_push_context_token_reset_restores_previous_value():
    """token恢复：嵌套退出内层后还原外层值（而非清空）；异常退出同样恢复"""
    ps = PushService(Settings())

    with ps._groups(["G1"]):
        with ps._groups(["G2"]):
            assert ps._groups_override == ["G2"]
        assert ps._groups_override == ["G1"]
    assert ps._groups_override is None

    with ps._switch("anchor"):
        with pytest.raises(ValueError):
            with ps._switch("system_event"):
                raise ValueError("boom")
        assert ps._switch_tag == "anchor"
    assert ps._switch_tag is None


# ==================== 开播/下播消息形态（第八批后调整） ====================

def test_live_end_text_has_no_url_line():
    """下播消息：全部形态移除"直播地址："行"""
    from blivepost.models.live import LiveStats
    ps = PushService(Settings())
    info = make_info()
    stats = LiveStats(like_count=10, danmaku_count=5, gold_count=1,
                      ship_count=0, sc_count=0)
    text = ps._live_end_text(info, stats)
    assert "直播地址" not in text
    assert "直播时长" in text  # 其余行不受影响


def test_live_start_text_keeps_url_line():
    """开播正文保留"直播地址："行（富媒体/文本降级仍用它，无按钮可点）"""
    ps = PushService(Settings())
    text = ps._live_start_text(make_info())
    assert "直播地址：https://live.bilibili.com/23058" in text


def test_live_markdown_strips_url_line():
    """Markdown开播消息移除"直播地址："行（跳转入口仅保留按钮）；其余行保留"""
    from blivepost.services.push import live_markdown_content
    content = ("[主播]的直播间开播啦！！！\n标题：标题\n房间号：23058\n"
               "直播地址：https://live.bilibili.com/23058\n推送时间：现在")
    md = live_markdown_content(content, "http://cover.jpg")
    assert "直播地址" not in md
    assert "标题：标题" in md and "推送时间" in md
    assert "![封面 #640px #360px](http://cover.jpg)" in md
    # 无图markdown同样移除该行
    md_noimg = live_markdown_content(content, None)
    assert "直播地址" not in md_noimg and "开播啦" in md_noimg
    # 非直播地址行不受影响（如动态消息的"链接："行）
    other = live_markdown_content("UP主动态\n链接：https://www.bilibili.com/opus/1", None)
    assert "链接：" in other


def test_live_enter_keyboard_uses_https_url():
    """「进入直播间」按钮使用https链接（QQ不支持bilibili://拉起客户端）"""
    from blivepost.services.push import live_enter_keyboard
    button = (live_enter_keyboard(23058)["content"]["rows"][0]["buttons"][0])
    assert button["action"]["data"] == "https://live.bilibili.com/23058"
    assert "bilibili://" not in button["action"]["data"]


def test_official_bot_markdown_stripped_text_fallback_keeps_url():
    """官方bot链路：Markdown消息无"直播地址"，降级文本保留"""
    from blivepost.services.qqbot import QQBotClient
    from blivepost.services.push import live_markdown_content

    content = "开播啦\n直播地址：https://live.bilibili.com/23058"
    md = live_markdown_content(content, None)
    assert "直播地址" not in md
    # 降级文本使用原始content（见 _broadcast_message 实现）
    import inspect
    src = inspect.getsource(QQBotClient._broadcast_message)
    assert "group_content" in src  # 文本降级用原始正文而非markdown加工版
