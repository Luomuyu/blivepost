# -*- coding: utf-8 -*-
"""推送服务测试（CQ转换、绑定路由、富媒体）"""

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
