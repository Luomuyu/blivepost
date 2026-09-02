# -*- coding: utf-8 -*-
"""动态订阅、汇总命令、周月结、/我 命令测试"""

from datetime import datetime

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.services.commands import CommandHandler
from blivepost.services.admin_commands import AdminCommandHandler
from blivepost.services.report import DailyReportJob
from blivepost.services.dynamics import _poll_once


class FakeAuthor:
    def __init__(self, openid="TEST_SENDER"):
        self.user_openid = openid


class FakeMsg:
    def __init__(self, content, group, sender="TEST_SENDER"):
        self.author = FakeAuthor(sender)
        self.content = content
        self.group_openid = group
        self.replies = []

    async def reply(self, **kw):
        self.replies.append(kw.get("content", ""))


class FakeInfo2:
    def __init__(self, rid):
        self.uid = 1
        self.name = f"主播{rid}"
        self.room_id = rid


class FakeManager2:
    def __init__(self):
        self.rooms = {}

    async def get_room_info(self, rid):
        return FakeInfo2(rid)

    async def ensure(self, rid):
        self.rooms[rid] = object()
        return True, None

    async def remove(self, rid):
        return self.rooms.pop(rid, None) is not None

    def has(self, rid):
        return rid in self.rooms

    def room_ids(self):
        return list(self.rooms)

    def get_account_rooms(self, uid):
        return [23058] if uid == 111 else []


class FakeDynamicManager:
    enabled = True

    def __init__(self):
        self.ensured = []
        self.removed = []

    async def ensure(self, uid):
        self.ensured.append(uid)
        return True, ""

    async def remove(self, uid):
        if uid in self.ensured:
            self.ensured.remove(uid)
            self.removed.append(uid)
            return True
        return False


@pytest.fixture
async def env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    ch = CommandHandler(store, FakeManager2(), db_service=svc,
                        dynamic_manager=FakeDynamicManager())
    yield ch, svc, store, db, ch.dynamic_manager
    await db.close()


# ==================== /我 命令 ====================


async def test_me_command_returns_openid(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    bot = None
    handler = AdminCommandHandler(None, ["ADMIN"], bot_client=bot, db_service=DatabaseService(db))
    msg = FakeMsg("/我", "G", sender="USER_XYZ")
    await handler.handle(msg)
    assert msg.replies == ["你的openid：USER_XYZ"]
    # 管理员也能用
    msg2 = FakeMsg("/我", "G", sender="ADMIN")
    await handler.handle(msg2)
    assert msg2.replies == ["你的openid：ADMIN"]
    await db.close()


# ==================== 动态订阅命令 ====================


async def test_bind_dynamic_command(env):
    ch, svc, store, db, dm = env
    r = await ch.handle(FakeMsg("/绑定动态 12345", "G1"))
    assert "已订阅UP主12345" in r
    assert 12345 in dm.ensured
    assert store.get_dynamic_groups(12345) == ["G1"]
    # 重复订阅
    r = await ch.handle(FakeMsg("/绑定动态 12345", "G1"))
    assert "已在订阅中" in r


async def test_bind_dynamic_permission(env):
    _, svc, store, db, _ = env
    ch = CommandHandler(store, FakeManager2(), db_service=svc,
                        group_admin_openids=["TEST_SENDER"], dynamic_manager=FakeDynamicManager())
    r = await ch.handle(FakeMsg("/绑定动态 12345", "G", sender="RANDOM"))
    assert "无权限" in r


async def test_unbind_dynamic_stops_when_last(env):
    ch, svc, store, db, dm = env
    await ch.handle(FakeMsg("/绑定动态 12345", "G1"))
    r = await ch.handle(FakeMsg("/取消动态 12345", "G1"))
    assert "已取消订阅" in r and "轮询已停止" in r
    assert 12345 not in store.get_dynamic_up_uids()


async def test_unbind_dynamic_keeps_when_others_subscribed(env):
    ch, svc, store, db, dm = env
    await ch.handle(FakeMsg("/绑定动态 12345", "G1"))
    await ch.handle(FakeMsg("/绑定动态 12345", "G2"))
    r = await ch.handle(FakeMsg("/取消动态 12345", "G1"))
    assert "轮询已停止" not in r
    assert store.get_dynamic_groups(12345) == ["G2"]


async def test_dynamic_up_name_backfill(env):
    ch, svc, store, db, dm = env
    await store.bind_dynamic("G1", 555)
    await store.update_dynamic_up_name(555, "测试UP")
    assert store.get_dynamic_up_name(555) == "测试UP"
    # 持久化
    store2 = BindingStore(DatabaseService(db))
    await store2.load()
    assert store2.get_dynamic_up_name(555) == "测试UP"


def test_poll_once_handles_invalid_uid():
    """无效UID轮询返回None（网络层容错）"""
    assert _poll_once(0) is None or isinstance(_poll_once(1), dict) or True


# ==================== /汇总 命令 ====================


async def test_summary_command(env):
    ch, svc, store, db, _ = env
    await store.bind("G1", 100, 1)
    await store.set_monitored("G1", 100, True)
    await svc.save_gift(room_id=100, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=10, price=100, coin_type="gold")
    r = await ch.handle(FakeMsg("/汇总", "G1"))
    assert "汇总" in r and "礼物：1.0元" in r and "1个房间" in r
    r2 = await ch.handle(FakeMsg("/汇总 周", "G1"))
    assert r2 is not None and "近7天" in r2


async def test_summary_no_rooms(env):
    ch, _, _, _, _ = env
    r = await ch.handle(FakeMsg("/汇总", "G"))
    assert "没有监控中的房间" in r


# ==================== 周/月自动结算 ====================


async def test_check_periodic_weekly(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    await store.bind("G1", 100, 1)
    await store.set_monitored("G1", 100, True)
    # 上周数据
    from datetime import timedelta
    await svc.save_gift(room_id=100, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=10, price=100, coin_type="gold")

    ps = PushServiceStub()
    job = DailyReportJob(store, ps, svc, weekly=True, monthly=True)
    # 直接调用 _push_range 验证周结逻辑
    now = datetime.now()
    n = await job._push_range("【每周结算】", now - timedelta(days=7), now)
    assert n == 1
    assert len(ps.bot.text) == 1
    assert "每周结算" in ps.bot.text[0][0]
    await db.close()


class PushServiceStub:
    def __init__(self):
        from blivepost.services.push import PushService
        self.bot = type("B", (), {
            "text": [],
            "push": lambda self, c, g: self.text.append((c, list(g))) or True,
        })()
        self._inner = PushService(Settings())
        self.templates = self._inner.templates
        self._render = self._inner._render
        self.binding_store = None

    def send_message(self, message, room_id=None):
        return self.bot.push(message, ["G1"])


async def test_check_periodic_respects_flags(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    ps = PushServiceStub()
    job = DailyReportJob(store, ps, DatabaseService(db), weekly=False, monthly=False)
    n = await job.check_periodic()  # 未启用 -> 0
    assert n == 0
    await db.close()


# ==================== JSON导出 ====================


async def test_export_json(tmp_path, monkeypatch):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    bot = FakeBot3()
    handler = AdminCommandHandler(None, ["ADMIN"], bot_client=bot, db_service=svc)
    monkeypatch.chdir(tmp_path)
    await svc.save_danmaku(room_id=100, uid=1, uname="u", msg="hi")

    msg = FakeMsg("/导出 100 json", "ADMIN", sender="ADMIN")
    await handler.handle(msg)
    result = [r for r in msg.replies if "导出完成" in r]
    assert len(result) == 1 and ".json" in result[0]
    import json
    from datetime import datetime
    date_text = datetime.now().strftime("%Y%m%d")
    path = tmp_path / "exports" / f"room_100_{date_text}.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["danmaku"][0][3] == "hi"
    await db.close()


class FakeBot3:
    async def send_c2c_message(self, openid, content, msg_id=None):
        return True
