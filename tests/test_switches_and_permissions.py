# -*- coding: utf-8 -*-
"""推送开关、群权限、日期查询、每日结算测试"""

import asyncio
from datetime import datetime, timedelta

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.bindings import BindingStore
from blivepost.services.commands import CommandHandler
from blivepost.services.push import PushService
from blivepost.services.report import DailyReportJob


class FakeAuthor:
    def __init__(self, openid="TEST_SENDER"):
        self.user_openid = openid


class FakeMsg:
    def __init__(self, content, group, sender="TEST_SENDER"):
        self.author = FakeAuthor(sender)
        self.content = content
        self.group_openid = group

    async def reply(self, **kw):
        pass


class FakeBindingStore:
    """推送路由桩：任意房间都路由到G1"""

    def get_monitored_groups(self, room_id):
        return ["G1"]


class RecordingBot:
    def __init__(self):
        self.text = []

    def push(self, content, groups):
        self.text.append((content, list(groups)))
        return True


# ==================== 推送开关 ====================


class FakeInfo:
    def __init__(self):
        self.uid = 1
        self.name = "主播"
        self.room_id = 23058
        self.online = "1"


def make_settings(**switches):
    s = Settings()
    s.message.anchor = switches.get("anchor", False)
    s.message.guard_achievement = switches.get("guard_achievement", True)
    s.message.system_event = switches.get("system_event", True)
    s.message.room_change = switches.get("room_change", False)
    return s


class Ctx:
    """最小handler桩：直接实例化BaseEventHandler测试开关分支"""

    def __init__(self, settings, bot):
        from blivepost.core.handler import BaseEventHandler
        from blivepost.models.live import LiveStats, TempData
        ps = PushService(settings)
        ps.bot_client = bot
        ps.binding_store = FakeBindingStore()
        self.handler = BaseEventHandler(
            settings, ps, LiveStats(), TempData(), "23058", None
        )


class Info:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_switches_parsed_from_settings():
    s = Settings()
    assert s.message.guard_achievement is True
    assert s.message.system_event is True
    assert s.message.room_change is False
    assert s.message.daily_report is False
    assert s.message.anchor is False


class AchievementInfo:
    anchor_guard_achieve_level = 3
    first_line_content = "累计<%100天%>"
    second_line_content = "舰队<%壮大%>"


def test_guard_achievement_switch():
    bot = RecordingBot()

    h_on = Ctx(make_settings(guard_achievement=True), bot).handler
    h_on._on_guard_achievement(None, AchievementInfo())
    assert len(bot.text) == 1
    assert "大航海成就" in bot.text[0][0]

    # 开关关闭：不推送
    bot2 = RecordingBot()
    h_off = Ctx(make_settings(guard_achievement=False), bot2).handler
    h_off._on_guard_achievement(None, AchievementInfo())
    assert len(bot2.text) == 0


def test_system_event_switch():
    bot = RecordingBot()

    ps = PushService(make_settings(system_event=True))
    ps.bot_client = bot
    ps.binding_store = FakeBindingStore()
    ps.push_cut_off("23058", "x")
    ps.push_warning("23058", "y")
    ps.push_room_block("23058", "用户", 1)
    ps.push_admin_change("23058", 2, revoked=False)
    ps.push_admin_change("23058", 3, revoked=True)
    assert len(bot.text) == 5
    assert "被禁言" in bot.text[2][0]
    assert "被设为房管" in bot.text[3][0]
    assert "撤销房管" in bot.text[4][0]


def test_new_push_methods_exist():
    ps = PushService(Settings())
    assert hasattr(ps, "push_room_block")
    assert hasattr(ps, "push_admin_change")
    assert hasattr(ps, "push_room_change")


async def test_daily_summary_single_roundtrip_regression(tmp_path):
    """合并单条SQL后的汇总结果回归"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.save_gift(room_id=1, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=10, price=100, coin_type="gold")
    await svc.save_gift(room_id=1, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=1, price=100, coin_type="silver")
    s = await svc.get_daily_summary(1)
    assert s["gift_gold"] == 1.0 and s["gift_count"] == 1  # 银瓜子不计
    await db.close()


# ==================== 群权限 ====================


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
async def perm_env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    yield db, store
    await db.close()


async def test_group_permission_whitelist(perm_env):
    db, store = perm_env
    ch = CommandHandler(store, FakeManager2(), group_admin_openids=["ADMIN_X"])

    # 非白名单：控制指令拒绝并提示openid
    r = await ch.handle(FakeMsg("/监控 23058", "G", sender="RANDOM"))
    assert "无权限" in r and "RANDOM" in r and "Admin.openids" in r
    # 白名单内：放行
    r = await ch.handle(FakeMsg("/监控 23058", "G", sender="ADMIN_X"))
    assert "已开始监控" in r
    # 查询指令不受限
    r = await ch.handle(FakeMsg("/收入 23058", "G", sender="RANDOM"))
    assert "统计功能不可用" in r  # db未传，走到业务逻辑说明权限放行


async def test_group_permission_empty_allows_all(perm_env):
    db, store = perm_env
    ch = CommandHandler(store, FakeManager2(), group_admin_openids=[])
    r = await ch.handle(FakeMsg("/监控 23058", "G", sender="ANYONE"))
    assert "已开始监控" in r


# ==================== 日期参数查询 ====================


async def test_income_with_date_param(perm_env):
    db, store = perm_env
    svc = DatabaseService(db)
    yesterday = datetime.now() - timedelta(days=1)
    await svc.save_gift(room_id=23058, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=10, price=100, coin_type="gold")
    ch = CommandHandler(store, FakeManager2(), db_service=svc)

    # 默认今天（有数据）
    r = await ch.handle(FakeMsg("/收入 23058", "G"))
    assert "礼物：1.0元" in r
    # 昨天日期（无数据）
    ymd = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    r = await ch.handle(FakeMsg(f"/收入 23058 {ymd}", "G"))
    assert "礼物：0.0元（0次）" in r and yesterday.strftime("%Y-%m-%d") in r
    # 非法日期：正则整体不匹配，命令被忽略
    r = await ch.handle(FakeMsg("/收入 23058 bad", "G"))
    assert r is None


# ==================== 每日结算 ====================


async def test_daily_report_pushes_to_monitored_rooms(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    store = BindingStore(svc)
    await store.load()
    await store.bind("G1", 100, 1)
    await store.set_monitored("G1", 100, True)
    await store.bind("G2", 200, 2)  # 绑定但未监控 -> 不推送
    # 房间100写入当日数据（无数据房间会被跳过）
    await svc.save_gift(room_id=100, uid=9, uname="u", gift_id=1, gift_name="g",
                        num=1, price=1000, coin_type="gold")

    ps = PushService(settings)
    bot = RecordingBot()
    ps.bot_client = bot
    ps.binding_store = store
    job = DailyReportJob(store, ps, svc)

    n = await job.check_once(day=datetime.now())  # 数据写的是今天
    assert n == 1
    assert len(bot.text) == 1
    assert "每日结算" in bot.text[0][0] and "直播间100" in bot.text[0][0]
    assert bot.text[0][1] == ["G1"]

    # 无数据房间跳过：清空数据后房间100不再推送
    s2 = Settings()
    s2.database.sqlite_path = str(tmp_path / "t2.db")
    db2 = DatabaseManager(s2.database)
    await db2.initialize()
    store2 = BindingStore(DatabaseService(db2))
    await store2.load()
    await store2.bind("G1", 100, 1)
    await store2.set_monitored("G1", 100, True)
    job2 = DailyReportJob(store2, ps, DatabaseService(db2))
    n = await job2.check_once(day=datetime.now())
    assert n == 0
    await db2.close()
    await db.close()


async def test_daily_report_no_monitored_rooms(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    store = BindingStore(DatabaseService(db))
    await store.load()
    job = DailyReportJob(store, PushService(settings), DatabaseService(db))
    assert await job.check_once() == 0
    await db.close()


# ==================== playwright chromium 自动安装 ====================


def test_ensure_chromium_skipped_without_playwright(monkeypatch):
    from blivepost.services import bili_login as bl

    class BlockingImporter:
        def find_module(self, name, path=None):
            return None

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "playwright":
            raise ImportError("No module named 'playwright'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    ok = asyncio.run(bl.ensure_playwright_chromium())
    assert ok is False


def test_vendor_cdn_chain_updated():
    """jsdelivr已废弃源不再出现，包含官方与镜像回退"""
    from blivepost.vendor.manager import CDN_TEMPLATES, DOWNLOAD_TIMEOUT
    joined = " ".join(CDN_TEMPLATES)
    assert "jsdelivr" not in joined
    assert "codeload.github.com" in joined
    assert "github.com" in joined
    assert any("gh-proxy" in t or "ghproxy" in t for t in CDN_TEMPLATES)
    assert DOWNLOAD_TIMEOUT <= 15  # 快速失败换下一源
