# -*- coding: utf-8 -*-
"""周月查询、池预警、账号亲和、CSV导出测试"""

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.services.commands import CommandHandler
from blivepost.services.cookie_pool import CookiePool
from blivepost.services.admin_commands import AdminCommandHandler


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
    async def get_room_info(self, rid):
        return FakeInfo2(rid)

    async def ensure(self, rid):
        return True, None

    async def remove(self, rid):
        return True

    def has(self, rid):
        return True

    def room_ids(self):
        return []


@pytest.fixture
async def env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    ch = CommandHandler(store, FakeManager2(), db_service=svc)
    yield ch, svc, store, db
    await db.close()


async def test_week_month_query(env):
    ch, svc, _, _ = env
    await svc.save_gift(room_id=23058, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=10, price=100, coin_type="gold")
    r = await ch.handle(FakeMsg("/收入 23058 周", "G"))
    assert "礼物：1.0元" in r and "~" in r  # 范围文本
    r = await ch.handle(FakeMsg("/收入 23058 月", "G"))
    assert "礼物：1.0元" in r


async def test_summary_range_empty(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    from datetime import datetime, timedelta
    end = datetime.now()
    r = await svc.get_summary_range(1, end - timedelta(days=7), end)
    assert r["gift_gold"] == 0.0 and "~" in r["day"]
    await db.close()


# ==================== 池耗尽预警 ====================


@pytest.fixture
async def pool(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    p = CookiePool(DatabaseService(db), warn_threshold=1)
    await p.load()
    yield p, db
    await db.close()


async def test_pool_warn_threshold(pool):
    p, _ = pool
    await p.upsert(1, "A", "c")
    await p.upsert(2, "B", "c")
    assert p.warn_if_needed() is None  # 2 > 1

    await p.report_invalid(1, "x")
    w = p.warn_if_needed()  # 剩1 <= 阈值 -> 提醒
    assert w is not None and "仅剩 1" in w
    assert p.warn_if_needed() is None  # 同水位不重复

    await p.report_invalid(2, "x")
    w = p.warn_if_needed()  # 剩0
    assert "耗尽" in w

    await p.upsert(3, "C", "c")
    assert p.warn_if_needed() is None  # 恢复水位重置


# ==================== 房间账号亲和 ====================


class AffinityPool:
    """最小池桩：记录acquire/acquire_specific调用"""

    def __init__(self):
        self.accounts = {1: "A", 2: "B"}
        self.specific_calls = []
        self.lru_calls = 0

    async def acquire_specific(self, uid):
        self.specific_calls.append(uid)
        if uid in self.accounts:
            return type("A", (), {"uid": uid, "cookie_string": f"c{uid}"})()
        return None

    async def acquire(self):
        self.lru_calls += 1
        if not self.accounts:
            return None
        return type("A", (), {"uid": 2, "cookie_string": "c2"})()


async def test_room_affinity_prefers_previous():
    from blivepost.core.manager import MonitorManager
    from blivepost.config.settings import Settings

    m = MonitorManager(Settings(), cookie_pool=AffinityPool())
    m._room_account[23058] = 1

    acc = await m._pick_account(23058)
    assert acc.uid == 1
    assert m._room_account[23058] == 1

    # 亲和账号失效 -> LRU换新并更新映射
    m._room_account[23058] = 99
    acc = await m._pick_account(23058)
    assert acc.uid == 2 and m._room_account[23058] == 2

    # 池空 -> 清除映射
    m.cookie_pool.lru_calls = 0
    m.cookie_pool.accounts.clear()
    acc = await m._pick_account(999)
    assert acc is None


# ==================== CSV导出 ====================


class FakeBot2:
    def __init__(self):
        self.replies = []

    async def send_c2c_message(self, openid, content, msg_id=None):
        self.replies.append(content)
        return True


async def test_export_command(tmp_path, monkeypatch):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    pool = CookiePool(svc)
    await pool.load()
    monkeypatch.chdir(tmp_path)  # exports目录写到临时目录

    bot = FakeBot2()
    handler = AdminCommandHandler(pool, ["ADMIN"], bot_client=bot, db_service=svc)
    await svc.save_gift(room_id=23058, uid=1, uname="u", gift_id=1, gift_name="小花",
                        num=1, price=100, coin_type="gold")

    msg = FakeMsg("/导出 23058", "ADMIN", sender="ADMIN")
    await handler.handle(msg)
    # "正在导出"与结果都经message.reply
    assert any("正在导出" in r for r in msg.replies)
    result = [r for r in msg.replies if "导出完成" in r]
    assert len(result) == 1 and "1条记录" in result[0]
    from datetime import datetime
    date_text = datetime.now().strftime("%Y%m%d")
    path = tmp_path / "exports" / f"room_23058_{date_text}.csv"
    assert path.exists()
    content = path.read_text(encoding="utf-8-sig")
    assert "小花" in content and "super_chat (0条)" in content
    await db.close()


async def test_export_command_empty_day(tmp_path, monkeypatch):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    pool = CookiePool(svc)
    await pool.load()
    monkeypatch.chdir(tmp_path)
    bot = FakeBot2()
    handler = AdminCommandHandler(pool, ["ADMIN"], bot_client=bot, db_service=svc)

    msg = FakeMsg("/导出 23058 20250101", "ADMIN", sender="ADMIN")
    await handler.handle(msg)
    assert any("无任何记录" in r for r in msg.replies)
    assert not (tmp_path / "exports" / "room_23058_20250101.csv").exists()
    await db.close()


def test_group_openids_removed():
    """group_openids配置已移除，群控制由openids/main_openids合并名单控制"""
    s = Settings()
    assert not hasattr(s.admin, "group_openids")
    assert hasattr(s.admin, "openids") and hasattr(s.admin, "main_openids")
