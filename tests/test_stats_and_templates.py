# -*- coding: utf-8 -*-
"""统计查询命令与模板测试"""

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.services.commands import CommandHandler
from blivepost.services.push import PushService


class FakeInfo:
    def __init__(self, rid, real=None):
        self.uid = 1
        self.name = f"主播{rid}"
        self.room_id = real or rid


class FakeManager:
    def __init__(self, short_map=None):
        self.rooms = set()
        self.info_map = short_map or {}

    async def get_room_info(self, rid):
        if rid >= 99999999:
            raise ValueError("房间不存在")
        return FakeInfo(rid, self.info_map.get(rid))

    async def ensure(self, rid):
        self.rooms.add(rid)
        return True, FakeInfo(rid)

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
async def env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    service = DatabaseService(db)
    store = BindingStore(service)
    await store.load()
    ch = CommandHandler(store, FakeManager(short_map={2345: 23058}), db_service=service)
    yield ch, service, settings
    await db.close()


async def seed_records(service: DatabaseService):
    """写入今天的测试数据：房间23058"""
    await service.save_danmaku(room_id=23058, uid=1, uname="u1", msg="hi")
    await service.save_danmaku(room_id=23058, uid=2, uname="u2", msg="yo")
    await service.save_gift(room_id=23058, uid=1, uname="u1", gift_id=1,
                            gift_name="小花", num=10, price=100, coin_type="gold")   # 1000瓜子=1元
    await service.save_gift(room_id=23058, uid=1, uname="u1", gift_id=1,
                            gift_name="小花", num=1, price=100, coin_type="silver")  # 银瓜子不计
    await service.save_guard(room_id=23058, uid=3, uname="u3", guard_level=3,
                             gift_name="舰长", price=138000, num=1)                  # 138元
    await service.save_super_chat(room_id=23058, uid=4, uname="u4", price=50, message="SC fifty")
    await service.save_super_chat(room_id=23058, uid=5, uname="u5", price=30, message="SC thirty")
    await service.save_red_pocket(room_id=23058, sender_uid=6, sender_name="u6", total_price=20)


async def test_income_command(env):
    ch, service, _ = env
    await seed_records(service)
    r = await ch.handle(FakeMsg("/收入 23058", "G"))
    assert "弹幕：2条" in r
    assert "礼物：1.00元（1次）" in r
    assert "上舰：138.00元（1次）" in r
    assert "SC：80.00元（2次）" in r
    assert "红包：20电池" in r


async def test_income_via_short_id(env):
    ch, service, _ = env
    await seed_records(service)
    r = await ch.handle(FakeMsg("/收入 2345", "G"))
    assert "直播间23058" in r


async def test_sc_board_command(env):
    ch, service, _ = env
    await seed_records(service)
    r = await ch.handle(FakeMsg("/SC榜 23058", "G"))
    lines = r.split("\n")
    assert "SC排行（今日）" in r
    assert lines[1].startswith("1. u4 ¥50")
    assert lines[2].startswith("2. u5 ¥30")
    # 小写也可用
    r2 = await ch.handle(FakeMsg("/sc榜 23058", "G"))
    assert "SC排行" in r2


async def test_sc_board_empty(env):
    ch, _, _ = env
    r = await ch.handle(FakeMsg("/SC榜 23058", "G"))
    assert "当日暂无SuperChat" in r


async def test_income_unknown_room(env):
    ch, _, _ = env
    r = await ch.handle(FakeMsg("/收入 99999999", "G"))
    assert "不存在或查询失败" in r


def make_info(room_id=23058, cover=""):
    from blivepost.models.live import LiveRoomInfo
    return LiveRoomInfo(uid=1, name="主播", title="标题", cover=cover,
                        room_id=room_id, online="1")


class RecordingBot:
    def __init__(self):
        self.text = []
        self.media = []

    def push(self, content, groups):
        self.text.append((content, list(groups)))
        return True

    def push_media(self, image_url, content, groups):
        self.media.append((image_url, content, list(groups)))
        return True


async def test_custom_templates_render(tmp_path):
    """自定义模板生效且占位符正确替换"""
    settings = Settings()
    settings.templates.live_start = "GO!{name}开播 房间{room_id}"
    settings.templates.anchor = "天选:{award} 要求:{require}"
    settings.templates.cut_off = "切断:{reason}"

    ps = PushService(settings)
    bot = RecordingBot()
    ps.bot_client = bot

    from blivepost.database import DatabaseManager, DatabaseService
    from blivepost.services.bindings import BindingStore
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    await store.bind("G1", 23058, 1)
    await store.set_monitored("G1", 23058, True)
    ps.binding_store = store

    ps.push_live_start(make_info(cover="http://c.jpg"))
    assert bot.media[0][1] == "GO!主播开播 房间23058"

    ps.push_anchor("23058", "奖品A", "关注", "粉丝牌")
    assert bot.text[0][0] == "天选:奖品A 要求:粉丝牌"

    ps.push_cut_off("23058", "违规")
    assert bot.text[1][0] == "切断:违规"
    await db.close()


async def test_bad_template_falls_back(tmp_path):
    """模板含未知占位符时回退默认文案"""
    settings = Settings()
    settings.templates.cut_off = "{不存在的字段}{reason}"

    ps = PushService(settings)
    bot = RecordingBot()
    ps.bot_client = bot

    from blivepost.database import DatabaseManager, DatabaseService
    from blivepost.services.bindings import BindingStore
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    await store.bind("G1", 23058, 1)
    await store.set_monitored("G1", 23058, True)
    ps.binding_store = store

    ps.push_cut_off("23058", "违规")
    assert bot.text[0][0] == "System：23058直播被切断，原因：违规"
    await db.close()


async def test_removed_push_methods_gone():
    """弹幕/礼物/舰长/SC/红包推送已移除（仅入库不推送）"""
    ps = PushService(Settings())
    for name in ("push_danmaku", "push_emoji_danmaku", "push_gift",
                 "push_super_chat", "push_ship", "push_red_pocket"):
        assert not hasattr(ps, name)
    # 开播/下播/天选/成就/切断/警告推送保留
    for name in ("push_live_start", "push_live_end", "push_anchor",
                 "push_guard_achievement", "push_cut_off", "push_warning"):
        assert hasattr(ps, name)
