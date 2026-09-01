# -*- coding: utf-8 -*-
"""管理员私聊命令与账号轮转测试"""

import asyncio

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.cookie_pool import CookiePool
from blivepost.services.admin_commands import AdminCommandHandler
from blivepost.core.manager import PushQueueConsumer
from blivepost.services.push import PushService


class FakeAuthor:
    def __init__(self, openid):
        self.user_openid = openid


class FakeC2CMessage:
    def __init__(self, content, openid, mid="M1"):
        self.content = content
        self.author = FakeAuthor(openid)
        self.id = mid
        self.replies = []

    async def reply(self, **kw):
        self.replies.append(kw.get("content", ""))


class FakeBot:
    def __init__(self):
        self.c2c_texts = []
        self.images = []

    async def send_c2c_message(self, openid, content, msg_id=None):
        self.c2c_texts.append((openid, content))
        return True

    async def send_c2c_image(self, openid, image_bytes, msg_id=None, filename="image.png"):
        self.images.append((openid, len(image_bytes)))
        return True

    async def notify_admins(self, admins, content):
        for a in admins:
            self.c2c_texts.append((a, content))
        return len(admins)


@pytest.fixture
async def env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    bot = FakeBot()
    handler = AdminCommandHandler(pool, ["ADMIN_OPENID"], bot_client=bot)
    yield handler, pool, bot, db
    await db.close()


async def test_login_permission_denied(env):
    handler, _, bot, _ = env
    msg = FakeC2CMessage("/登录", "NORMAL_USER")
    await handler.handle(msg)
    assert msg.replies == ["无权限：仅管理员可执行此操作"]
    assert handler._login_task is None  # 未启动登录流程


async def test_login_unknown_command_ignored(env):
    handler, _, _, _ = env
    msg = FakeC2CMessage("你好", "ADMIN_OPENID")
    await handler.handle(msg)
    assert msg.replies == []


async def test_login_admin_starts_flow(env):
    handler, pool, bot, _ = env
    msg = FakeC2CMessage("/登录", "ADMIN_OPENID")
    await handler.handle(msg)
    # 已启动后台登录任务
    assert handler.busy
    assert any("二维码" in r for r in msg.replies)
    # 停掉后台任务（playwright未安装，任务会以异常结束或正在运行）
    handler._login_task.cancel()
    try:
        await handler._login_task
    except (asyncio.CancelledError, Exception):
        pass


async def test_login_concurrent_blocked(env):
    handler, _, _, _ = env
    handler._login_task = asyncio.create_task(asyncio.sleep(60))
    msg = FakeC2CMessage("/登录", "ADMIN_OPENID")
    await handler.handle(msg)
    assert any("已有登录流程" in r for r in msg.replies)
    handler._login_task.cancel()


async def test_login_finish_success_writes_pool(env):
    handler, pool, bot, _ = env
    from blivepost.services.bili_login import LoginResult
    result = LoginResult(
        ok=True, uid=111, nickname="账号A",
        cookie_string="SESSDATA=aaa; bili_jct=t1", sessdata="aaa", bili_jct="t1",
        cookies=[{"name": "SESSDATA", "value": "aaa"}],
    )
    await handler._finish_login(result, "ADMIN_OPENID")
    acc = pool.get_account(111)
    assert acc is not None and acc.status == "active" and acc.nickname == "账号A"
    assert any("登录成功" in t for _, t in bot.c2c_texts)


async def test_login_finish_timeout(env):
    handler, pool, bot, _ = env
    from blivepost.services.bili_login import LoginResult
    await handler._finish_login(LoginResult(timeout=True, error="扫码超时"), "ADMIN_OPENID")
    assert pool.size() == 0
    assert any("超时" in t for _, t in bot.c2c_texts)


class FakeRoomManager:
    def __init__(self):
        self.cookie_uid = 111
        self.removed = []
        self.ensured = []
        self.suspended = []

    def get_cookie_uid(self, rid):
        return self.cookie_uid

    async def remove(self, rid):
        self.removed.append(rid)
        return True

    async def ensure(self, rid):
        self.ensured.append(rid)
        return True, None

    async def suspend(self, rid, reason=""):
        self.suspended.append((rid, reason))
        return True


async def test_consumer_not_login_rotates_account(tmp_path):
    """凭证失效：标记失效+私聊管理员+换号重建"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    await pool.upsert(111, "A", "SESSDATA=a")
    await pool.upsert(222, "B", "SESSDATA=b")

    bot = FakeBot()
    ps = PushService(settings)
    ps.bot_client = bot
    fm = FakeRoomManager()
    consumer = PushQueueConsumer(None, ps, None, monitor_manager=fm,
                                 cookie_pool=pool, admin_openids=["ADMIN1"])

    await consumer._handle_not_login(23058)

    # 失效账号被标记
    assert pool.get_account(111).status == "expired"
    # 管理员收到私聊提醒
    assert any("已失效" in t for _, t in bot.c2c_texts)
    # 换号重建
    assert fm.removed == [23058] and fm.ensured == [23058]
    await db.close()


async def test_consumer_not_login_suspends_when_pool_empty(tmp_path):
    """账号池无可用账号：暂停监控并私聊提醒"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    await pool.upsert(111, "A", "SESSDATA=a")

    bot = FakeBot()
    ps = PushService(settings)
    ps.bot_client = bot
    fm = FakeRoomManager()
    consumer = PushQueueConsumer(None, ps, None, monitor_manager=fm,
                                 cookie_pool=pool, admin_openids=["ADMIN1"])

    await consumer._handle_not_login(23058)
    assert fm.suspended == [(23058, "登录凭证失效且账号池无可用账号")]
    assert any("无可用账号" in t for _, t in bot.c2c_texts)
    await db.close()


# ==================== 账号池管理命令 ====================


async def test_account_pool_status_command(env):
    handler, pool, bot, _ = env
    await pool.upsert(111, "账号A", "SESSDATA=a")
    msg = FakeC2CMessage("/账号", "ADMIN_OPENID")
    await handler.handle(msg)
    assert len(msg.replies) == 1
    assert "1 个账号" in msg.replies[0] and "账号A" in msg.replies[0]
    assert "可用 1" in msg.replies[0]


async def test_account_pool_status_empty(env):
    handler, _, _, _ = env
    msg = FakeC2CMessage("/账号", "ADMIN_OPENID")
    await handler.handle(msg)
    assert "账号池为空" in msg.replies[0]


async def test_remove_account_command(env):
    handler, pool, _, db = env
    await pool.upsert(111001, "账号A", "SESSDATA=a")
    msg = FakeC2CMessage("/移除账号 111001", "ADMIN_OPENID")
    await handler.handle(msg)
    assert "已从账号池删除" in msg.replies[0]
    assert pool.get_account(111001) is None
    # 数据库也已删除
    from blivepost.database import DatabaseService
    service = DatabaseService(db)
    records = await service.get_cookie_accounts()
    assert all(r.uid != 111 for r in records)


async def test_remove_account_not_found(env):
    handler, _, _, _ = env
    msg = FakeC2CMessage("/移除账号 99999", "ADMIN_OPENID")
    await handler.handle(msg)
    assert "不在账号池中" in msg.replies[0]


async def test_reactivate_account_with_valid_nav(env, monkeypatch):
    handler, pool, _, _ = env
    from blivepost.services import bili_login as bl

    class FakeResp:
        def json(self):
            return {"code": 0, "data": {"isLogin": True, "mid": 111001, "uname": "账号A"}}

    monkeypatch.setattr(bl, "requests", type("M", (), {"get": staticmethod(lambda *a, **kw: FakeResp())}))

    await pool.upsert(111001, "账号A", "SESSDATA=a")
    await pool.report_invalid(111001, "测试失效")
    msg = FakeC2CMessage("/恢复账号 111001", "ADMIN_OPENID")
    await handler.handle(msg)
    assert "已恢复为可用" in msg.replies[0]
    assert pool.get_account(111001).status == "active"


async def test_reactivate_account_with_invalid_nav(env, monkeypatch):
    handler, pool, _, _ = env
    from blivepost.services import bili_login as bl

    class FakeResp:
        def json(self):
            return {"code": -101, "message": "账号未登录"}

    monkeypatch.setattr(bl, "requests", type("M", (), {"get": staticmethod(lambda *a, **kw: FakeResp())}))

    await pool.upsert(111001, "账号A", "SESSDATA=bad")
    await pool.report_invalid(111001, "测试失效")
    msg = FakeC2CMessage("/恢复账号 111001", "ADMIN_OPENID")
    await handler.handle(msg)
    assert "无法通过登录态验证" in msg.replies[0]
    assert pool.get_account(111001).status == "expired"


async def test_admin_commands_denied_for_normal_user(env):
    handler, pool, _, _ = env
    for content in ("/账号", "/移除账号 111001", "/恢复账号 111001", "/登录"):
        msg = FakeC2CMessage(content, "NORMAL")
        await handler.handle(msg)
        assert msg.replies == ["无权限：仅管理员可执行此操作"], content


# ==================== 通知分级 ====================


class LevelBot(FakeBot):
    def __init__(self):
        super().__init__()
        self.notified = []

    async def notify_admins(self, admins, content):
        self.notified.append((list(admins), content))
        return len(admins)


async def test_consumer_sensitive_notice_goes_to_main_admins(tmp_path):
    """敏感通知（凭证失效）发主管理员，普通通知发全部管理员"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    await pool.upsert(111001, "A", "SESSDATA=a")
    await pool.upsert(222, "B", "SESSDATA=b")

    bot = LevelBot()
    ps = PushService(settings)
    ps.bot_client = bot

    class FM:
        def get_cookie_uid(self, rid):
            return 111001
        async def remove(self, rid):
            return True
        async def ensure(self, rid):
            return True, None
        async def suspend(self, rid, reason=""):
            return True

    consumer = PushQueueConsumer(None, ps, None, monitor_manager=FM(),
                                 cookie_pool=pool,
                                 admin_openids=["ADMIN1", "ADMIN2"],
                                 main_admin_openids=["MAIN1"])
    await consumer._handle_not_login(23058)

    kinds = [(admins, "失效" in c) for admins, c in bot.notified]
    # 敏感：失效提醒只发 MAIN1；普通：轮转后无额外通知
    assert (["MAIN1"], True) in kinds
    assert not any(admins == ["ADMIN1", "ADMIN2"] and kind for admins, kind in kinds)
    await db.close()


async def test_consumer_sensitive_falls_back_when_no_main(tmp_path):
    """未配置主管理员时敏感通知回落全部管理员"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    await pool.upsert(111001, "A", "SESSDATA=a")

    bot = LevelBot()
    ps = PushService(settings)
    ps.bot_client = bot

    class FM:
        def get_cookie_uid(self, rid):
            return 111001
        async def remove(self, rid):
            return True
        async def ensure(self, rid):
            return True, None
        async def suspend(self, rid, reason=""):
            return True

    consumer = PushQueueConsumer(None, ps, None, monitor_manager=FM(),
                                 cookie_pool=pool,
                                 admin_openids=["ADMIN1"],
                                 main_admin_openids=[])
    await consumer._handle_not_login(23058)
    assert (["ADMIN1"], True) in [(a, "失效" in c) for a, c in bot.notified]
    await db.close()
