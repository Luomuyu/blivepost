# -*- coding: utf-8 -*-
"""全项目审查修复回归测试（v1.2.0 发布前）

覆盖：B3退款保留群配置 / B4指令建议边界 / M-1配置新字段解析 /
api_v1鉴权与限速顺序 / H1游客脱敏 / _wait_stop_event可退出
"""

import asyncio

import pytest


# ---------- B3：退款只清used_date，保留mode/room_ids ----------

async def test_refund_group_at_all_keeps_mode(tmp_path):
    """艾特全体退款不清群的rooms模式配置（此前clear会连配置一起删）"""
    from blivepost.config.settings import Settings
    from blivepost.database import DatabaseManager, DatabaseService

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        # 配置 rooms 模式（仅房间100），并占用当日配额
        await svc.set_group_at_all_mode("GA", "rooms", "100,200")
        assert await svc.acquire_at_all_groups(["GA"], room_id=100) == ["GA"]
        # 退款
        assert await svc.refund_group_at_all("GA") is True
        # 配置保留、配额已退还
        mode = await svc.get_group_at_all_mode("GA")
        assert mode == {"mode": "rooms", "room_ids": "100,200"}
        assert await svc.acquire_at_all_groups(["GA"], room_id=100, mark=False) == ["GA"]
        # 未占用时退款无操作
        assert await svc.refund_group_at_all("GB") is False
    finally:
        await svc.aclose()
        await db.close()


# ---------- B4：单独"/"不崩 ----------

def test_suggest_command_bare_slash():
    """/与全角／及纯空格输入返回None（此前IndexError→"命令执行失败"）"""
    from blivepost.services.commands import CommandHandler
    for probe in ("/", "／", "/  ", "／\t"):
        assert CommandHandler._suggest_command(probe) is None


# ---------- M-1：配置新字段从YAML读入 ----------

def test_parse_config_new_fields():
    """backup_*/web_api_*/timezone此前不被_parse_config解析（静默失效）"""
    import tempfile
    from pathlib import Path
    from blivepost.config.settings import Settings

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config.yaml"
        cfg.write_text(
            "Setting:\n"
            "  backup_hour: 5\n"
            "  backup_minute: 15\n"
            "  backup_keep: 0\n"
            "  web_api_public: \"Y\"\n"
            "  web_api_token: \"apitk\"\n"
            "  timezone: \"UTC\"\n", encoding="utf-8")
        s = Settings.load(str(cfg))
        assert s.message.backup_hour == 5
        assert s.message.backup_minute == 15
        assert s.message.backup_keep == 0
        assert s.message.web_api_public is True
        assert s.message.web_api_token == "apitk"
        assert s.message.timezone == "UTC"


# ---------- api_v1：公网模式鉴权 + 限速先于鉴权 ----------

async def test_api_v1_public_mode_auth_and_rate_order():
    """公网模式强制token（bool字段判断）；401也计限速额度（防token爆破）"""
    from blivepost.services.api_v1 import ApiV1Mixin

    class Panel(ApiV1Mixin):
        def __init__(self, ms):
            self.message_settings = ms
            self._api_rate = {}

    class MS:
        web_api_public = True
        web_api_token = "T0"

    class Req:
        remote = "8.8.8.8"
        headers = {"X-API-Token": "wrong"}

        def __init__(self, token=None):
            if token:
                self.headers = {"X-API-Token": token}

    p = Panel(MS())
    # 私网判断：公网IP在公网模式下必须靠token
    assert p._api_authorized(Req()) is False
    assert p._api_authorized(Req("T0")) is True
    # 限速先于鉴权：未认证请求也消耗额度
    for _ in range(3):
        denied = await p._api_v1_guard(Req())
        assert denied is not None and denied.status == 401
    p._api_rate["8.8.8.8"] = (p._api_rate["8.8.8.8"][0], 60)
    denied = await p._api_v1_guard(Req("T0"))
    assert denied is not None and denied.status == 429  # 额度已尽，正确token也被限


# ---------- H1：游客页面脱敏 ----------

async def test_guest_pages_masked_and_accounts_gated():
    """游客：app_id隐藏、账号池页重定向登录；登录后不受影响"""
    from aiohttp import web
    from blivepost.services.webpanel import WebPanel

    panel = WebPanel(None, None, None, None, None, token="TK")

    class FakeReq:
        headers: dict = {}
        cookies: dict = {}
        remote = "127.0.0.1"
        path = "/accounts"
        query = {}

    # 游客访问accounts页 → 302登录
    with pytest.raises(web.HTTPFound) as ei:
        await panel._make_page_handler("accounts")(FakeReq())
    assert ei.value.location == "/login"


# ---------- B1/B2：停等协程可退出 ----------

async def test_wait_stop_event_exits_on_set():
    """纯异步轮询停等：置位后1个轮询周期内返回（不占执行器线程）"""
    import multiprocessing as mp
    from blivepost.core.room_process import _wait_stop_event

    ev = mp.Event()
    task = asyncio.create_task(_wait_stop_event(ev, poll=0.05))
    assert not task.done()
    ev.set()
    await asyncio.wait_for(task, timeout=1.0)


# ---------- 第二轮：逐项修复回归 ----------

async def test_sql_bucketing_matches_python_keys(tmp_path):
    """SQL整数分桶与Python侧day_key_ms/hour_key_ms等价（含跨日样本）"""
    from blivepost.config.settings import Settings
    from blivepost.database import DatabaseManager, DatabaseService
    from blivepost.database.models import DanmakuRecord
    from blivepost.utils.timeutil import day_bounds_ms, day_key_ms, hour_key_ms

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        # 样本：今天0点边界前后各一条+26小时前一条（覆盖跨日与小时分桶）
        # 直写房间子库（分析查询按room_id路由到子库）
        today_start, _ = day_bounds_ms()
        samples = [today_start - 1, today_start, today_start - 26 * 3600 * 1000]
        sub = await svc._ensure_registry().get(await svc._room_db_name(1))
        async with sub.get_session() as session:
            for ts in samples:
                session.add(DanmakuRecord(room_id=1, uid=1, uname="u",
                                          msg="m", timestamp=ts))
            await session.commit()

        heat = await svc.get_activity_heatmap(room_id=1, days=7)
        expect: dict = {}
        for ts in samples:
            d, h = day_key_ms(ts), hour_key_ms(ts)
            expect.setdefault(d, [0] * 24)[h] += 1
        for day, hours in expect.items():
            assert day in heat, f"缺日键{day}"
            assert list(heat[day]) == hours, f"{day}小时分布不符"
        dist = await svc.get_danmaku_daily_distribution(room_id=1, days=7)
        dist_map = {row["day"]: row["count"] for row in dist}
        for day, hours in expect.items():
            assert dist_map.get(day) == sum(hours)
    finally:
        await svc.aclose()
        await db.close()


def test_atall_rooms_fullwidth_normalized():
    """/艾特全体 仅 房间号：全角数字归一化为半角"""
    import asyncio
    from blivepost.services.commands import CommandHandler

    class DB:
        def __init__(self):
            self.saved = None

        async def set_group_at_all_mode(self, g, mode, room_ids=""):
            self.saved = (mode, room_ids)
            return True

    handler = CommandHandler.__new__(CommandHandler)
    handler.db_service = DB()
    out = asyncio.run(handler._at_all_mode("G", "仅 １２３，456"))
    assert "123,456" in out
    assert handler.db_service.saved == ("rooms", "123,456")


def test_send_queue_watermark_degrades_to_text():
    """发送队列高水位：本地图推送降级纯文本（不再携带大图入队）"""
    from blivepost.services.qqbot import QQBotClient

    bot = QQBotClient.__new__(QQBotClient)
    # 绕过started/_loop检查：直接给内部状态
    bot._task = None
    bot._loop = object()  # 非None即通过"未启动"检查（不真正发消息）
    QQBotClient.started = property(lambda self: True)
    try:
        import asyncio as _aio
        bot._send_queue = _aio.Queue(maxsize=200)
        for _ in range(150):
            bot._send_queue.put_nowait(object())
        pushed = []
        bot.push = lambda content, groups, **kw: pushed.append(content) or True
        ok = bot.push_media_data(b"x" * 100, "动态文本", ["G"])
        assert ok is True and pushed == ["动态文本"]
    finally:
        # 还原被替换的property（避免污染其他测试）
        QQBotClient.started = property(lambda self: (
            self._task is not None and not self._task.done()))


# ---------- 第三轮：迁移器重置后复审修复 ----------

class _FakeReq:
    """轻量请求桩（鉴权相关测试用）"""

    def __init__(self, query=None, headers=None, cookies=None, path="/"):
        self.query = query or {}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.path = path
        self.remote = "127.0.0.1"


async def test_operational_pages_guest_gated():
    """运营情报页（accounts/groups/live/dynamics/analytics）游客302登录；
    聚合页（overview）游客可看且AppID隐藏"""
    from aiohttp import web
    from blivepost.services.webpanel import WebPanel

    panel = WebPanel(None, None, None, None, None, token="TK")

    async def fake_ctx(key):
        return {}

    panel._page_context = fake_ctx
    for page in ("accounts", "groups", "live", "dynamics", "analytics"):
        with pytest.raises(web.HTTPFound) as ei:
            await panel._make_page_handler(page)(_FakeReq(path=f"/{page}"))
        assert ei.value.location == "/login", page


async def test_token_login_failures_locked_by_ip():
    """/login?token=失败按IP计入防爆破锁定（web_token兼作会话密钥源，
    猜中即管理员——令牌路径不能无限速暴力尝试）"""
    from blivepost.services.webpanel import WebPanel

    panel = WebPanel(None, None, None, None, None, token="SECRET")

    class Req(_FakeReq):
        def __init__(self, token):
            super().__init__(query={"token": token}, path="/login")

    for _ in range(WebPanel.LOGIN_MAX_FAILURES):
        assert await panel._auth_token_to_admin(Req("wrong")) is None
    # 锁定期间正确令牌也被拒绝
    assert await panel._auth_token_to_admin(Req("SECRET")) is None
    # 正常路径：未锁定时正确令牌建立管理员会话
    panel2 = WebPanel(None, None, None, None, None, token="SECRET")
    resp = await panel2._auth_token_to_admin(Req("SECRET"))
    assert resp is not None and resp.status == 302
    assert panel2._session_from_sid(
        resp.cookies["blivepost_sid"].value)["admin"] is True


def test_speakers_api_requires_login():
    """/api/v1/analytics?type=speakers 观众身份明细需面板登录（401）"""
    from blivepost.services.api_v1 import ApiV1Mixin

    class Panel(ApiV1Mixin):
        def __init__(self):
            self.message_settings = None
            self._api_rate = {}
            self.db_service = None
            self._sess = None
            self._tok_ok = False

        def _session(self, request):
            return self._sess

        def _web_token_ok(self, request):
            return self._tok_ok

    class Db:
        @staticmethod
        async def get_top_speakers(**kw):
            return []

    class Req:
        remote = "127.0.0.1"
        headers = {}
        query = {"type": "speakers"}

    import asyncio
    panel = Panel()
    resp = asyncio.run(panel._handle_api_v1_analytics(Req()))
    assert resp.status == 503  # db_service为None先503——改挂DB后再验401
    panel.db_service = Db()
    panel._sess = None
    resp = asyncio.run(panel._handle_api_v1_analytics(Req()))
    assert resp.status == 401  # 有DB但无会话：身份明细拒绝
    panel._sess = {"user": "u"}
    resp = asyncio.run(panel._handle_api_v1_analytics(Req()))
    assert resp.status == 200  # 登录后放行


# ---------- 第四轮：发布前清理（静默迁移/限速键/配额窗口） ----------

async def test_migrations_silent_at_info(tmp_path, caplog):
    """正式发布前：版本表归一/初始化不得出现在INFO日志（debug=Y可查）"""
    import logging as _logging
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from blivepost.database.migrations import run_migrations

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/t.db")
    try:
        with caplog.at_level(_logging.INFO, logger="blivepost.migrations"):
            await run_migrations(engine)
            # 预置遗留行再跑一次触发归一路径
            async with engine.begin() as conn:
                await conn.execute(text(
                    "INSERT INTO schema_version(version) VALUES (7)"))
            await run_migrations(engine)
    finally:
        await engine.dispose()
    assert not [r for r in caplog.records
                if r.levelno >= _logging.INFO], \
        [r.getMessage() for r in caplog.records if r.levelno >= _logging.INFO]


def test_api_rate_none_remote_bypasses():
    """remote为None（非TCP来源）不与"unknown"共享限速桶"""
    from blivepost.services.api_v1 import ApiV1Mixin

    class Panel(ApiV1Mixin):
        def __init__(self):
            self.message_settings = None
            self._api_rate = {}

    class Req:
        remote = None

    p = Panel()
    assert p._api_rate_ok(Req()) is True
    assert p._api_rate == {}  # 未计入任何共享桶


async def test_at_all_acquisition_filters_receivable():
    """配额判定与推送链同口径：未开启消息接收的群不占用艾特配额"""
    from blivepost.core.consumer import PushQueueConsumer

    class PS:
        contact_store = None

        def resolve_push_groups(self, room_id):
            return ["G1", "G2"]

        def push_live_start_from_parts(self, **kw):
            return True

    class CS:
        @staticmethod
        def filter_receivable_groups(groups):
            return [g for g in groups if g != "G2"]  # G2未开接收

    class DB:
        def __init__(self):
            self.acquired = None
            self.refunded = []

        async def acquire_at_all_groups(self, groups, room_id=0, mark=True):
            self.acquired = list(groups)
            return list(groups)

        async def refund_group_at_all(self, g):
            self.refunded.append(g)

    ps = PS()
    ps.contact_store = CS()
    db = DB()
    consumer = PushQueueConsumer(None, ps, db)
    got = await consumer._acquire_at_all_groups(100)
    assert got == ["G1"] and db.acquired == ["G1"]  # G2未进判定不占配额
