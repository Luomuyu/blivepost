# -*- coding: utf-8 -*-
"""开播提醒艾特全体（每群每自然日1次）测试

链路：消费者判定配额（DB登记当日已用）→ push/broadcast 携带 at_all_groups
→ bot层 _broadcast_message 对应群正文前嵌入 <qqbot-at-everyone />。
"""

from blivepost.config.settings import Settings
from blivepost.database.manager import DatabaseManager
from blivepost.database.models import GroupAtAllRecord
from blivepost.database.service import DatabaseService
from blivepost.services.push import PushService
from blivepost.services.qqbot import QQBotClient


# ---------- 1. 配额判定 ----------

async def test_acquire_at_all_quota_per_day(tmp_path):
    """当天首次可艾特；同日再次不可；跨日恢复"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        first = await svc.acquire_at_all_groups(["GA", "GB"])
        assert first == ["GA", "GB"]
        again = await svc.acquire_at_all_groups(["GA", "GB"])
        assert again == []
        mixed = await svc.acquire_at_all_groups(["GA", "GC"])
        assert mixed == ["GC"]
        # 模拟跨日：把GB的登记改为昨天，再次判定应可用
        async with db.get_session() as session:
            rec = await session.get(GroupAtAllRecord, "GB")
            rec.used_date = "2000-01-01"
            await session.commit()
        revived = await svc.acquire_at_all_groups(["GB"])
        assert revived == ["GB"]
        # 空输入/空串
        assert await svc.acquire_at_all_groups([]) == []
        assert await svc.acquire_at_all_groups(["", None]) == []
    finally:
        await svc.aclose()
        await db.close()


# ---------- 2. bot层嵌入 ----------

class _RecordingBotCore(QQBotClient):
    """记录markdown/文本发送内容的桩（绕过真实client）"""

    def __init__(self):
        super().__init__("id", "sec")
        self.md_calls = []
        self.text_calls = []

    async def send_group_markdown(self, group, content, keyboard=None):
        self.md_calls.append((group, content))
        return True

    async def send_group_message(self, group, content, msg_id=None, msg_seq=None):
        self.text_calls.append((group, content))
        return True


async def test_broadcast_message_embeds_at_all_per_group():
    """at_all_groups中的群正文嵌入@全体标签，其余群不受影响"""
    bot = _RecordingBotCore()
    await bot._broadcast_message(["GA", "GB"], "开播啦",
                                 at_all_groups=["GA"])
    md = dict(bot.md_calls)
    assert "<qqbot-at-everyone />" in md["GA"]
    assert "开播啦" in md["GA"]
    assert "<qqbot-at-everyone />" not in md["GB"]


async def test_broadcast_message_text_fallback_keeps_tag():
    """markdown失败降级文本时，标签保留（文本消息同样支持嵌入解析）"""
    bot = _RecordingBotCore()

    async def md_fail(group, content, keyboard=None):
        bot.md_calls.append((group, content))
        return False

    bot.send_group_markdown = md_fail
    await bot._broadcast_message(["GA"], "开播啦", media="http://img",
                                 at_all_groups=["GA"])
    assert bot.text_calls[0][0] == "GA"
    assert bot.text_calls[0][1].startswith("<qqbot-at-everyone />")
    assert "http://img" in bot.text_calls[0][1]


def test_push_entry_accepts_at_all_groups():
    """push/push_media/broadcast 接受 at_all_groups（不启动时安全失败不抛错）"""
    bot = QQBotClient("id", "sec")
    assert bot.push("c", ["G"], at_all_groups=["G"]) is False
    assert bot.broadcast(["G"], "c", at_all_groups=["G"]) is False
    assert bot.push_media("http://i", "c", ["G"], at_all_groups=["G"]) is False


# ---------- 3. 消费者接入 ----------

class _QuotaDb:
    def __init__(self):
        self.calls = []

    async def acquire_at_all_groups(self, groups, mark=True):
        if mark:
            self.calls.append(list(groups))
        return list(groups) if groups else []


class _Store:
    def get_dynamic_groups(self, up_uid):
        return ["G1", "G2"]

    def get_dynamic_up_name(self, up_uid):
        return "UP甲"


class _Ps(PushService):
    def __init__(self):
        super().__init__(Settings())
        self.live_start_calls = []
        self.bot_client = None

    def resolve_push_groups(self, room_id):
        return ["G1"]

    def push_live_start_from_parts(self, content, media, room_id, keyboard,
                                   at_all_groups=None):
        self.live_start_calls.append((room_id, at_all_groups))
        return True


async def test_consumer_live_start_acquires_quota():
    """开播推送：消费者按目标群判定配额并传给 from_parts"""
    from blivepost.core.consumer import PushQueueConsumer

    ps = _Ps()
    db = _QuotaDb()
    consumer = PushQueueConsumer(None, ps, db, binding_store=_Store())
    await consumer._handle({"type": "push_live_start", "room_id": 23058,
                            "content": "开播", "media": None, "keyboard": None})
    assert ps.live_start_calls == [(23058, ["G1"])]
    assert db.calls == [["G1"]]


async def test_consumer_up_live_passes_at_all():
    """UP开播提醒：配额内群经broadcast携带at_all_groups"""
    from blivepost.core.consumer import PushQueueConsumer

    class Bot:
        def __init__(self):
            self.calls = []

        def broadcast(self, groups, content, keyboard=None, at_all_groups=None):
            self.calls.append((list(groups), at_all_groups))
            return True

    ps = _Ps()
    ps.bot_client = Bot()
    db = _QuotaDb()
    consumer = PushQueueConsumer(None, ps, db, binding_store=_Store())
    await consumer._handle_up_live({"up_uid": 111, "title": "T", "room_id": 9})
    assert ps.bot_client.calls == [(["G1", "G2"], ["G1", "G2"])]


async def test_consumer_no_db_no_at_all():
    """无DB（旧部署形态）：不判定配额，at_all为None不影响推送"""
    from blivepost.core.consumer import PushQueueConsumer

    ps = _Ps()
    consumer = PushQueueConsumer(None, ps, None, binding_store=_Store())
    await consumer._handle({"type": "push_live_start", "room_id": 23058,
                            "content": "开播", "media": None, "keyboard": None})
    assert ps.live_start_calls == [(23058, None)]


# ---------- 4. from_parts 传递 ----------

async def test_from_parts_passes_at_all_to_official_bot():
    """官方bot路径：push_media/push 接收at_all_groups"""
    ps = PushService(Settings())

    class RoomStore:
        def get_monitored_groups(self, room_id):
            return ["G1"]

    ps.binding_store = RoomStore()

    class Bot:
        def __init__(self):
            self.media_calls = []
            self.push_calls = []

        def push_media(self, image, content, groups, keyboard=None,
                       at_all_groups=None):
            self.media_calls.append((image, list(groups), at_all_groups))
            return True

        def push(self, content, groups, keyboard=None, media=None,
                 at_all_groups=None):
            self.push_calls.append((content, list(groups), at_all_groups))
            return True

    ps.bot_client = Bot()
    ok = ps.push_live_start_from_parts("开播", "http://c", 23058, None,
                                       at_all_groups=["G1"])
    assert ok is True
    assert ps.bot_client.media_calls[0][2] == ["G1"]

    # 无图路径 → push 直传
    ok2 = ps.push_live_start_from_parts("开播", None, 23058, None,
                                        at_all_groups=["G1"])
    assert ok2 is True
    assert ps.bot_client.push_calls[0][2] == ["G1"]


# ---------- 第九轮审查修复回归 ----------
# 注：@全体配额为平台按群每日1次的硬性限制，正文注入标签无法绕过
# （仅消耗该群当日平台配额），故不做程序侧剥离——曾实现的
# strip_platform_tags 已按该结论回退。


async def test_up_live_filters_receivable_before_quota():
    """UP开播提醒：未开接收的群不推送也不占配额"""
    from blivepost.core.consumer import PushQueueConsumer

    class Store2(_Store):
        def get_dynamic_groups(self, up_uid):
            return ["G1", "GMUTE"]

    class Contacts:
        @staticmethod
        def filter_receivable_groups(groups):
            return [g for g in groups if g != "GMUTE"]

    class Bot2:
        def __init__(self):
            self.calls = []

        def broadcast(self, groups, content, keyboard=None, at_all_groups=None):
            self.calls.append((list(groups), at_all_groups))
            return True

    ps = _Ps()
    ps.bot_client = Bot2()
    ps.contact_store = Contacts()
    db = _QuotaDb()
    consumer = PushQueueConsumer(None, ps, db, binding_store=Store2())
    await consumer._handle_up_live({"up_uid": 1, "title": "T", "room_id": 9})
    # GMUTE被过滤：广播与配额都只含G1
    assert ps.bot_client.calls == [(["G1"], ["G1"])]
    assert db.calls == [["G1"]]
