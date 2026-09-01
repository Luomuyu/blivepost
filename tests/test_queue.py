# -*- coding: utf-8 -*-
"""子进程队列桥与主进程消费者测试"""

import asyncio
import queue

from blivepost.config.settings import Settings
from blivepost.core.manager import QueuePushService, QueueDbBridge, PushQueueConsumer
from blivepost.models.live import LiveStats
from blivepost.services.push import PushService


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


class FakeDb:
    def __init__(self):
        self.saved = []

    async def save_danmaku(self, **kw):
        self.saved.append(("save_danmaku", kw))

    async def save_gift(self, **kw):
        self.saved.append(("save_gift", kw))


class FakeRoomInfo:
    def __init__(self):
        self.uid = 1
        self.name = "主播"
        self.title = "标题"
        self.cover = "http://cover.jpg"
        self.room_id = 23058
        self.online = "1"


def test_queue_push_service_forwards_with_media(tmp_path):
    """子进程推送服务：普通消息无media，开播/下播消息携带封面"""
    q = queue.Queue()
    settings = Settings()
    ps = QueuePushService(settings, q, room_id=23058)

    ps.push_anchor("23058", "奖品", "弹幕", "要求")
    item = q.get_nowait()
    assert item["type"] == "push" and item["room_id"] == 23058
    assert item["media"] is None and "天选" in item["content"]

    ps.push_live_start(FakeRoomInfo())
    item = q.get_nowait()
    assert item["media"] == "http://cover.jpg"
    assert "开播" in item["content"]

    ps.push_live_end(FakeRoomInfo(), LiveStats())
    item = q.get_nowait()
    assert item["media"] == "http://cover.jpg"
    assert "下播" in item["content"]


def test_queue_db_bridge():
    """落库桥：save_*方法写入队列"""
    q = queue.Queue()
    bridge = QueueDbBridge(q)

    async def run():
        await bridge.save_danmaku(room_id=1, msg="hi")
        await bridge.save_gift(room_id=1, gift_name="g")

    asyncio.run(run())
    a = q.get_nowait()
    assert a["type"] == "db" and a["method"] == "save_danmaku" and a["kwargs"]["msg"] == "hi"
    b = q.get_nowait()
    assert b["method"] == "save_gift"
    try:
        bridge.not_a_save
        assert False
    except AttributeError:
        pass


async def test_push_queue_consumer(tmp_path):
    """消费者：push走推送路由（优先媒体），db集中落库"""
    q = queue.Queue()
    settings = Settings()
    ps = PushService(settings)
    bot = RecordingBot()
    ps.bot_client = bot
    fake_db = FakeDb()

    consumer = PushQueueConsumer(q, ps, fake_db)

    q.put({"type": "push", "room_id": 23058, "content": "文本", "media": None})
    q.put({"type": "push", "room_id": 23058, "content": "开播", "media": "http://c.jpg"})
    q.put({"type": "db", "method": "save_danmaku", "kwargs": {"room_id": 1, "msg": "hi"}})
    q.put({"type": "db", "method": "save_gift", "kwargs": {"room_id": 1}})
    q.put({"type": "unknown"})
    q.put(None)  # 哨兵

    task = asyncio.create_task(consumer.run())
    await asyncio.wait_for(task, timeout=5)

    # 无绑定时不推送
    assert bot.text == [] and bot.media == []
    assert fake_db.saved == [("save_danmaku", {"room_id": 1, "msg": "hi"}),
                             ("save_gift", {"room_id": 1})]


async def test_consumer_stop():
    q = queue.Queue()
    consumer = PushQueueConsumer(q, PushService(Settings()), None)
    task = asyncio.create_task(consumer.run())
    await asyncio.sleep(0.05)
    await consumer.stop()
    # 停止标志生效：等待轮询间隙（1秒）退出
    await asyncio.wait_for(task, timeout=5)
    assert task.done()


async def test_consumer_routes_to_bound_groups(tmp_path):
    """有绑定时消费者按房间路由文本与媒体推送"""
    from blivepost.database import DatabaseManager, DatabaseService
    from blivepost.services.bindings import BindingStore

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
    bot = RecordingBot()
    ps.bot_client = bot

    q = queue.Queue()
    q.put({"type": "push", "room_id": 23058, "content": "文本", "media": None})
    q.put({"type": "push", "room_id": 23058, "content": "开播", "media": "http://c.jpg"})
    q.put(None)
    consumer = PushQueueConsumer(q, ps, None)
    await asyncio.wait_for(consumer.run(), timeout=5)

    assert bot.text == [("文本", ["GRP_A"])]
    assert bot.media == [("http://c.jpg", "开播", ["GRP_A"])]
    await db.close()


async def test_consumer_dispatches_not_login(tmp_path):
    """队列分发：not_login消息进入_handle_not_login（轮转逻辑见test_admin_commands）"""
    settings = Settings()
    ps = PushService(settings)
    ps.bot_client = RecordingBot()

    class FakeManager:
        def get_cookie_uid(self, rid):
            return 111

        async def remove(self, rid):
            return True

        async def ensure(self, rid):
            return True, None

        async def suspend(self, rid, reason=""):
            return True

    called = []
    consumer = PushQueueConsumer(queue.Queue(), ps, None,
                                 monitor_manager=FakeManager(),
                                 cookie_pool=None, admin_openids=[])

    async def fake_handle(rid):
        called.append(rid)

    consumer._handle_not_login = fake_handle

    q = consumer._queue
    q.put({"type": "not_login", "room_id": 23058})
    q.put(None)
    await asyncio.wait_for(consumer.run(), timeout=5)
    assert called == [23058]


def test_queue_push_service_notify_not_login():
    """子进程推送服务：notify_not_login 写入队列"""
    q = queue.Queue()
    settings = Settings()
    ps = QueuePushService(settings, q, room_id=23058)
    assert ps.notify_not_login(23058) is True
    item = q.get_nowait()
    assert item == {"type": "not_login", "room_id": 23058}
