# -*- coding: utf-8 -*-
"""第十二批新功能：use_count、自动重扫、token鉴权、history测试"""

import pytest

from aiohttp import web

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.cookie_pool import CookiePool


@pytest.fixture
async def pool(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    p = CookiePool(DatabaseService(db))
    await p.load()
    yield p, db
    await db.close()


async def test_use_count_increments(pool):
    p, db = pool
    await p.upsert(1, "A", "SESSDATA=a")
    await p.acquire()
    await p.acquire()
    assert p.get_account(1).use_count == 2
    # 持久化验证
    from blivepost.services.cookie_pool import CookiePool as CP
    p2 = CP(DatabaseService(db))
    await p2.load()
    assert p2.get_account(1).use_count == 2


async def test_use_count_on_specific_acquire(pool):
    p, _ = pool
    await p.upsert(1, "A", "SESSDATA=a")
    await p.acquire_specific(1)
    await p.acquire_specific(1)
    assert p.get_account(1).use_count == 2


async def test_use_count_persists_via_save(tmp_path):
    """use_count持久化：db touch自增后重载一致"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    p = CookiePool(DatabaseService(db))
    await p.load()
    await p.upsert(1, "A", "SESSDATA=a")
    await p.acquire()
    await p.acquire()
    p2 = CookiePool(DatabaseService(db))
    await p2.load()
    assert p2.get_account(1).use_count == 2
    await db.close()


# ==================== token鉴权 ====================

_FULL_COLLECT = {
    "now": "t",
    "bot": {"client_started": True, "token_ready": True, "token_expires_in": 3600},
    "me": {"id": "9", "username": "bot", "avatar": "", "share_url": ""},
    "app_id": "100",
    "db_size_mb": 0,
    "accounts": [], "rooms": [], "dynamics": [], "recent": [],
}


def _make_panel(token=""):
    from blivepost.services.webpanel import WebPanel
    return WebPanel(None, None, None, None, None, token=token)


class FakeReq:
    """最小请求桩：query/headers/cookies字典 + 可选表单"""
    def __init__(self, query=None, headers=None, cookies=None, form=None, path="/"):
        self.query = query or {}
        self.headers = headers or {}
        self.cookies = cookies or {}
        self._form = form or {}
        self.path = path
        self.remote = "test"

    async def post(self):
        return self._form


@pytest.mark.asyncio
async def test_guest_readonly_actions_denied():
    """新鉴权模型：页面/只读接口游客可浏览，写动作与后台接口拒绝"""
    panel = _make_panel()
    resp = await panel._make_page_handler("overview")(FakeReq())
    assert resp.status == 200  # 游客可浏览
    resp2 = await panel._handle_history(FakeReq())
    assert resp2.status == 200
    # 写动作：游客401
    resp3 = await panel._action_room_add(FakeReq(form={"room_id": "123"}))
    assert resp3.status == 401
    assert await panel._read_action(FakeReq()) is None
    # 后台动作：游客403
    resp4 = await panel._action_admin_user_add(FakeReq(form={"username": "u", "password": "1234"}))
    assert resp4.status == 403
    # 后台页面：游客跳登录页
    import pytest as _pytest
    with _pytest.raises(web.HTTPFound) as ei:
        await panel._handle_admin_page(FakeReq())
    assert "/login" in ei.value.location


@pytest.mark.asyncio
async def test_token_quick_login_and_pages(monkeypatch, tmp_path):
    """?token=快捷令牌→管理员会话重定向；后续请求携cookie渲染各页（回归断言保留）"""
    panel = _make_panel(token="SECRET")
    class D:
        @staticmethod
        async def get_daily_history(days=30): return []
        @staticmethod
        async def get_daily_history_range(start_day, end_day, room_ids=None): return []
        @staticmethod
        async def get_recent_live_status(limit=10): return []
        @staticmethod
        async def list_room_ids(): return []
        @staticmethod
        async def load_groups(active_only=False):
            class G:
                group_openid = "G1"
                group_name = "测试群"
            return [G()]
        @staticmethod
        async def list_rooms(): return []
        @staticmethod
        async def list_bindings(): return []
    panel.db_service = D()

    async def fake_collect():
        return dict(_FULL_COLLECT)
    panel._collect = fake_collect

    # 令牌登录收窄（v1.2.0）：普通页?token=不再建会话（游客200无cookie）
    resp = await panel._make_page_handler("overview")(FakeReq(query={"token": "SECRET"}))
    assert resp.status == 200
    assert "blivepost_sid" not in resp.cookies

    # /login?token= → 管理员会话（302重定向）
    login_resp = await panel._handle_login_page(
        FakeReq(query={"token": "SECRET"}, path="/login"))
    assert login_resp.status == 302
    sid = login_resp.cookies["blivepost_sid"].value  # cookie经Set-Cookie下发（prepare时应用）
    assert panel._session_from_sid(sid)["admin"] is True

    # 携会话cookie渲染概览页（回归：viewport/机器人详情）
    class CookieReq:
        query = {}
        headers = {}
        cookies = {"blivepost_sid": sid}
    resp_home = await panel._make_page_handler("overview")(CookieReq())
    assert resp_home.status == 200
    body = resp_home.text
    assert "viewport" in body
    assert "bot" in body and "在线" in body
    assert "后台" in body  # 管理员身份显示后台入口

    # 运营情报页（live/groups/dynamics/analytics/accounts）游客302登录；
    # 聚合页（如records）游客照常200
    from aiohttp import web as _web
    try:
        await panel._make_page_handler("live")(FakeReq())
        raise AssertionError("游客访问live页应302登录")
    except _web.HTTPFound as resp_guest:
        assert resp_guest.location == "/login"

    # 直播监控页（含群选项/房间表/删除模式/刷新图标）
    resp2 = await panel._make_page_handler("live")(CookieReq())
    assert resp2.status == 200
    assert "手动刷新" not in resp2.text and 'aria-label="刷新"' in resp2.text
    assert "/web/room/add" in resp2.text and "/web/room/push" in resp2.text
    assert "/web/room/delete" in resp2.text

    # 回归：删除模式必须解除确认/取消按钮的hidden（曾漏移除导致按钮永不出现）
    assert "btn-del-confirm').classList.remove('hidden')" in resp2.text
    assert "btn-del-cancel').classList.remove('hidden')" in resp2.text

    # 动态监控页：添加按钮的处理函数必须存在（回归：enterDynAddMode曾未定义致点击无响应）
    resp_dyn = await panel._make_page_handler("dynamics")(CookieReq())
    assert resp_dyn.status == 200
    assert "function enterDynAddMode" in resp_dyn.text
    assert "btn-dyn-del-confirm').classList.remove('hidden')" in resp_dyn.text
    assert "群绑定状态" in resp_dyn.text and "toggleDynGroupDetail" in resp_dyn.text

    # 趋势局部：12个月按月汇总模式
    class MonthReq:
        query = {"range": "12m"}
        headers = {}
        cookies = {"blivepost_sid": sid}
    resp3 = await panel._handle_trend(MonthReq())
    assert resp3.status == 200
    assert "按月汇总" in resp3.text


def test_token_via_header():
    """X-Web-Token头携带令牌视同管理员（动作接口放行）"""
    panel = _make_panel(token="SECRET")
    req = FakeReq(headers={"X-Web-Token": "SECRET"})
    assert panel._web_token_ok(req) is True
    assert panel._web_token_ok(FakeReq()) is False


async def _fake_collect(seen):
    seen["collected"] = True
    return {}


# ==================== history ====================


async def test_daily_history(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    # 写今天的数据
    await svc.save_danmaku(room_id=1, uid=1, uname="u", msg="hi")
    history = await svc.get_daily_history(days=30)
    assert len(history) == 30
    today = history[-1]
    assert today["danmaku"] == 1
    assert all(h["danmaku"] == 0 for h in history[:-1])
    await db.close()


async def test_daily_history_range(tmp_path):
    """自然日区间查询：含首尾、区间内无数据日补零"""
    from datetime import timedelta
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    from blivepost.utils.timeutil import now_local
    now = now_local()  # 分桶按Setting.timezone，机器本地日键会错位
    await svc.save_danmaku(room_id=1, uid=1, uname="u", msg="today")
    rows = await svc.get_daily_history_range(now.date() - timedelta(days=2), now.date())
    assert len(rows) == 3
    assert rows[-1]["danmaku"] == 1 and rows[-1]["day"] == now.strftime("%Y-%m-%d")
    assert all(r["danmaku"] == 0 for r in rows[:-1])
    # get_daily_history委托到range实现（等价性）
    legacy = await svc.get_daily_history(days=3)
    assert legacy == rows
    await db.close()


def test_aggregate_monthly():
    from blivepost.services.webpanel import _aggregate_monthly
    daily = [
        {"day": "2026-07-31", "danmaku": 1, "gift_gold": 1.5, "ship_gold": 0, "sc_total": 2},
        {"day": "2026-08-01", "danmaku": 3, "gift_gold": 2.0, "ship_gold": 98.0, "sc_total": 1},
        {"day": "2026-08-31", "danmaku": 4, "gift_gold": 0.5, "ship_gold": 2.0, "sc_total": 0},
    ]
    rows = _aggregate_monthly(daily)
    assert [r["day"] for r in rows] == ["2026-07", "2026-08"]
    assert rows[0]["danmaku"] == 1 and rows[0]["gift_gold"] == 1.5
    assert rows[1]["danmaku"] == 7 and rows[1]["ship_gold"] == 100.0


def test_parse_month():
    from blivepost.services.webpanel import _parse_month
    assert _parse_month("2026-08") == (2026, 8)
    assert _parse_month("2025-12") == (2025, 12)
    assert _parse_month("bad") is None
    assert _parse_month("2026-13") is None


# ==================== 自动重扫 ====================


async def test_rescan_restores_recovered_account(tmp_path, monkeypatch):
    """expired账号nav复核通过 → 自动回池并通知管理员"""
    from blivepost.services import bili_login as bl

    class FakeResp:
        def json(self):
            return {"code": 0, "data": {"isLogin": True, "mid": 1, "uname": "A"}}

    monkeypatch.setattr(bl.requests, "get", lambda *a, **kw: FakeResp())

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    await pool.upsert(1, "A", "SESSDATA=a")
    await pool.report_invalid(1, "临时失效")

    from blivepost.core.manager import CookieMaintainer
    m = CookieMaintainer.__new__(CookieMaintainer)
    m.pool = pool
    notified = []
    async def fake_notify(admins, content):
        notified.append((admins, content))
        return len(admins)
    m.bot_client = type("B", (), {"notify_admins": staticmethod(fake_notify)})
    m.admin_openids = ["ADMIN"]
    m.main_admin_openids = []
    m._last_rescan_date = ""

    # 调用_rescan_expired（绕过check_once的6h循环）
    n = await m._rescan_expired()
    assert n == 1
    assert pool.get_account(1).status == "active"
    assert notified and notified[0][0] == ["ADMIN"] and "重扫发现登录态恢复" in notified[0][1]

    # 同一天不重复扫
    n2 = await m._rescan_expired()
    assert n2 == 0


async def test_rescan_skips_still_invalid(tmp_path, monkeypatch):
    from blivepost.services import bili_login as bl

    class FakeResp:
        def json(self):
            return {"code": -101, "message": "账号未登录"}

    monkeypatch.setattr(bl.requests, "get", lambda *a, **kw: FakeResp())

    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    pool = CookiePool(DatabaseService(db))
    await pool.load()
    await pool.upsert(1, "A", "SESSDATA=bad")
    await pool.report_invalid(1, "失效")

    from blivepost.core.manager import CookieMaintainer
    m = CookieMaintainer.__new__(CookieMaintainer)
    m.pool = pool
    m.bot_client = None
    m.admin_openids = []
    m.main_admin_openids = []
    m._last_rescan_date = ""

    n = await m._rescan_expired()
    assert n == 0
    assert pool.get_account(1).status == "expired"
    await db.close()


# ==================== 主播/直播间信息表 ====================


async def test_streamer_and_room_tables(tmp_path):
    """主播/直播间快照表：按uid/room_id upsert、仅有值时覆盖"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.save_streamer_info(91156053, uname="墨翎", room_id=9646546)
    await svc.save_streamer_info(91156053, uname="墨翎2")  # 仅更新有值字段
    assert await svc.get_streamer_name(91156053) == "墨翎2"
    assert await svc.get_streamer_name(1) == ""
    assert await svc.streamer_count() == 1

    await svc.save_live_room_info(9646546, uid=91156053, title="标题",
                                  live_status=1, online=5, area_name="虚拟主播")
    await svc.save_live_room_info(9646546, online=9)  # 仅覆盖online
    # 借apply_status_info刷新主播昵称验证room表联动不受影响
    assert await svc.streamer_count() == 1
    await db.close()


async def test_apply_status_info(tmp_path):
    """get_status_info_by_uids结构批量写入两表"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.apply_status_info({
        "672328094": {
            "title": "【B限】玩个毛线", "room_id": 22637261, "uid": 672328094,
            "online": 4087370, "live_status": 2, "short_id": 0,
            "area_v2_name": "虚拟主播", "area_v2_parent_name": "虚拟主播",
            "uname": "嘉然今天吃什么", "face": "http://example.com/f.jpg",
            "cover_from_user": "http://example.com/c.jpg", "keyframe": "http://example.com/k.jpg",
        },
        "bad": {"room_id": 1},  # 非法键跳过不抛
    })
    assert await svc.get_streamer_name(672328094) == "嘉然今天吃什么"
    assert await svc.streamer_count() == 1
    await db.close()


def test_get_bound_uids():
    """全部绑定关系中的主播uid（去重、去0）"""
    from blivepost.services.bindings import Binding, BindingStore
    store = BindingStore(None)
    store._bindings = {
        "G1": {1: Binding("G1", 1, uid=5), 2: Binding("G1", 2, uid=7)},
        "G2": {3: Binding("G2", 3, uid=5), 4: Binding("G2", 4, uid=0)},
    }
    assert store.get_bound_uids() == [5, 7]


async def test_streamer_refresher_batch(tmp_path, monkeypatch):
    """每日刷新：分批查询并落库两表"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)

    from blivepost.services.bindings import Binding, BindingStore
    store = BindingStore(None)
    store._bindings = {
        "G1": {1: Binding("G1", 1, uid=7), 2: Binding("G1", 2, uid=8)},
    }

    class FakeAPI:
        def __init__(self):
            pass

        def get_status_info_by_uids(self, uids):
            return {str(u): {"uname": f"u{u}", "room_id": 1000 + u, "uid": u,
                             "live_status": 1, "online": 10} for u in uids}

    import blivepost.services.bilibili as bili
    monkeypatch.setattr(bili, "BilibiliAPI", FakeAPI)

    from blivepost.core.manager import StreamerRefresher
    refresher = StreamerRefresher(store, svc)
    n = await refresher.refresh_once()
    assert n == 2
    assert await svc.streamer_count() == 2
    assert await svc.get_streamer_name(7) == "u7"
    await db.close()


@pytest.mark.asyncio
async def test_trend_room_selector(tmp_path):
    """趋势房间选择：room参数过滤数据源，label标注房间，下拉回显选中项"""
    panel = _make_panel()
    sample = [{"day": "2026-09-06", "danmaku": 5, "gift_gold": 1.0,
               "ship_gold": 0, "sc_total": 0}]

    class D:
        @staticmethod
        async def get_daily_history_range(start_day, end_day, room_ids=None):
            D.requested_rooms = room_ids
            return sample

        @staticmethod
        async def get_recent_live_status(limit=10): return []
        @staticmethod
        async def list_room_ids(): return [100, 9646546]

    panel.db_service = D()

    async def fake_collect():
        return dict(_FULL_COLLECT)
    panel._collect = fake_collect

    class Req:
        query = {"range": "d30", "room": "9646546"}
        headers = {}
        cookies = {}
    resp = await panel._handle_trend(Req())
    assert resp.status == 200
    body = resp.text
    assert "房间9646546" in body  # label标注
    assert D.requested_rooms == [9646546]  # 数据查询按房间过滤
    assert '>直播间 9646546</option>' in body and 'selected' in body  # 下拉回显
    assert '>全部直播间</option>' in body

    # 不带room参数=全部直播间总和
    class ReqAll:
        query = {"range": "d30"}
        headers = {}
        cookies = {}
    resp2 = await panel._handle_trend(ReqAll())
    assert resp2.status == 200
    assert D.requested_rooms is None  # None=跨全部房间子库扇出


# ==================== 登录体系/后台/推送开关（第五十九批） ====================


@pytest.mark.asyncio
async def test_admin_login_user_manage_and_switch(tmp_path):
    """管理员config登录→建用户→用户登录→开关权限链路全测"""
    from blivepost.services.webpanel import WebPanel, hash_password, verify_password
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)

    panel = WebPanel(None, None, None, None, svc,
                     admin_user="boss", admin_password="SECRET123")

    # 口令哈希往返
    h = hash_password("abcd1234")
    assert h != "abcd1234" and verify_password("abcd1234", h)
    assert not verify_password("wrong", h)

    # 管理员登录成功 → 302 /admin + 会话cookie
    resp_login = await panel._action_web_login(FakeReq(form={
        "username": "boss", "password": "SECRET123"}))
    assert resp_login.status == 302 and resp_login.location == "/admin"
    admin_sid = resp_login.cookies["blivepost_sid"].value
    assert panel._session_from_sid(admin_sid)["admin"] is True

    # 登录失败 → 回登录页带错误
    with pytest.raises(web.HTTPFound) as ei2:
        await panel._action_web_login(FakeReq(form={
            "username": "boss", "password": "wrong"}))
    assert "/login?err=1" in ei2.value.location

    admin_req = FakeReq(cookies={"blivepost_sid": admin_sid})

    # 管理员建前台用户（密码过短拒绝）
    resp = await panel._action_admin_user_add(FakeReq(
        cookies={"blivepost_sid": admin_sid},
        form={"username": "alice", "password": "12"}))
    assert resp.status == 200  # 动作静默，无建户
    assert await svc.get_web_user("alice") is None
    await panel._action_admin_user_add(FakeReq(
        cookies={"blivepost_sid": admin_sid},
        form={"username": "alice", "password": "abcd1234"}))
    assert await svc.get_web_user("alice") is not None

    # 后台页面管理员可进
    resp_admin = await panel._handle_admin_page(admin_req)
    assert resp_admin.status == 200
    assert "管理后台" in resp_admin.text and "alice" in resp_admin.text

    # 用户登录（非管理员）→ 首页
    resp_alice = await panel._action_web_login(FakeReq(form={
        "username": "alice", "password": "abcd1234"}))
    assert resp_alice.status == 302 and resp_alice.location == "/"
    alice_sid = resp_alice.cookies["blivepost_sid"].value
    assert panel._session_from_sid(alice_sid)["user"] == "alice"

    # 用户改开关：默认allow_modify关→403；管理员放行后可切
    deny = await panel._action_switch_toggle(FakeReq(
        cookies={"blivepost_sid": alice_sid}, form={"name": "anchor"}))
    assert deny.status == 403
    assert await svc.get_push_switch_enabled("anchor", False) is False

    await panel._action_admin_switch(FakeReq(
        cookies={"blivepost_sid": admin_sid},
        form={"name": "anchor", "allow_modify": "1"}))
    await panel._action_switch_toggle(FakeReq(
        cookies={"blivepost_sid": alice_sid}, form={"name": "anchor"}))
    assert await svc.get_push_switch_enabled("anchor", False) is True

    # 用户不可改allow_modify（越权拒绝）
    resp_am = await panel._action_admin_switch(FakeReq(
        cookies={"blivepost_sid": alice_sid},
        form={"name": "anchor", "allow_modify": "0"}))
    assert resp_am.status == 403

    # 管理员改密+删用户
    await panel._action_admin_user_password(FakeReq(
        cookies={"blivepost_sid": admin_sid},
        form={"username": "alice", "password": "newpass99"}))
    rec = await svc.get_web_user("alice")
    assert verify_password("newpass99", rec["password_hash"])
    await panel._action_admin_user_delete(FakeReq(
        cookies={"blivepost_sid": admin_sid}, form={"username": "alice"}))
    assert await svc.get_web_user("alice") is None

    # 登出：响应清除会话cookie（签名会话无服务端状态，登出=客户端丢弃cookie）
    with pytest.raises(web.HTTPFound) as ei_logout:
        await panel._action_web_logout(FakeReq(cookies={"blivepost_sid": alice_sid}))
    morsel = ei_logout.value.cookies["blivepost_sid"]
    assert morsel.value == "" and int(morsel["max-age"]) == 0
    await db.close()


@pytest.mark.asyncio
async def test_push_switch_seed_and_consumer_gate(tmp_path):
    """开关播种幂等；消费者按群独立设置过滤（行值优先，缺行回落全局）"""
    from blivepost.core.manager import PushQueueConsumer
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)

    await svc.seed_push_switches({"anchor": False, "system_event": True})
    await svc.seed_push_switches({"anchor": True})  # 已有行不覆盖
    assert await svc.get_push_switch_enabled("anchor", True) is False
    assert await svc.get_push_switch_enabled("system_event", False) is True
    assert await svc.get_push_switch_enabled("daily_report", False) is False

    # 绑定群播种继承全局：两群anchor=关；随后手动开G1独立anchor
    await svc.seed_group_push_settings(100, ["G1", "G2"], {"anchor": False})
    own = await svc.get_group_push_setting(100, "G1")
    assert own == {s: False for s in (
        "anchor", "guard_achievement", "system_event", "room_change",
        "daily_report", "weekly_report", "monthly_report")}
    await svc.set_group_push_setting(100, "G1", "anchor", True)
    assert (await svc.get_group_push_setting(100, "G1"))["anchor"] is True

    class Store:
        def get_monitored_groups(self, rid):
            return ["G1", "G2"]

    received = []

    class Push:
        @staticmethod
        def _groups(allowed):
            Push.last_allowed = allowed
            from contextlib import nullcontext
            return nullcontext()

        def send_message(self, content, room_id=None, keyboard=None):
            received.append(content)
            return True

    Push.last_allowed = "unset"
    consumer = PushQueueConsumer(None, Push(), db_service=svc, binding_store=Store(),
                                 switch_defaults={"anchor": True})
    item = {"type": "push", "room_id": 100, "content": "hello",
            "media": None, "keyboard": None, "switch": "anchor"}

    # G1独立开→收；G2缺行回落全局(关)→不收：限定群列表恰为[G1]
    await consumer._handle(dict(item))
    assert received == ["hello"]
    assert Push.last_allowed == ["G1"]

    # 全局开+两群都独立关→无人收
    await svc.set_push_switch("anchor", enabled=True)
    await svc.set_group_push_setting(100, "G1", "anchor", False)
    await svc.seed_group_push_settings(100, ["G2"], {"anchor": True})
    await svc.set_group_push_setting(100, "G2", "anchor", False)
    assert await consumer._allowed_groups(100, "anchor") == []

    # 无tag推送不受开关影响（override解除）
    await consumer._handle({"type": "push", "room_id": 100, "content": "x",
                            "media": None, "keyboard": None, "switch": None})
    assert received[-1] == "x"
    assert Push.last_allowed is None
    await db.close()


@pytest.mark.asyncio
async def test_screen_requires_login_and_renders(tmp_path):
    """数据大屏：游客拒绝（HTMX返回HX-Redirect/普通302），登录后可访问并渲染各区块"""
    from aiohttp.test_utils import make_mocked_request

    from blivepost.services.webpanel import WebPanel

    async def _fake_collect():
        return {"now": "t", "bot": {"client_started": True, "token_ready": True,
                "token_expires_in": 3600},
                "me": {"id": "9", "username": "bot", "avatar": "", "share_url": ""},
                "app_id": "100", "db_size_mb": 1.2,
                "accounts": [],
                "rooms": [{"room_id": 100, "alive": True, "uid": 1,
                           "streamer": "s", "danmaku": 1, "gold": 0,
                           "ship": 0, "sc": 0}],
                "dynamics": [{"up_uid": 1, "name": "u", "alive": True, "groups": 1}],
                "recent": []}
    panel = WebPanel(None, None, None, None, None, bot_client=None)
    panel._collect = _fake_collect

    # 游客访问整页 → 302登录页；HTMX局部 → HX-Redirect
    resp_denied = await panel._handle_screen_page(make_mocked_request("GET", "/screen"))
    assert resp_denied.status == 302 and "/login" in resp_denied.location
    req_hx = make_mocked_request("GET", "/panel/screen",
                                 headers={"HX-Request": "true"})
    resp_hx = await panel._handle_screen_data(req_hx)
    assert resp_hx.headers.get("HX-Redirect") == "/login?err=2"

    # 登录会话 → 大屏整页渲染（标题/指标/趋势容器）
    sid = panel._create_session("alice", is_admin=False)
    req = make_mocked_request("GET", "/screen",
                              headers={"Cookie": f"blivepost_sid={sid}"})
    resp = await panel._handle_screen_page(req)
    assert resp.status == 200
    body = resp.text
    assert "数据大屏" in body and "screen-clock" in body
    assert "screen-trend" in body and "renderScreenTrend" in body
    assert "监控房间" in body and "账号池" in body
    # HTMX局部渲染
    req2 = make_mocked_request("GET", "/panel/screen",
                               headers={"Cookie": f"blivepost_sid={sid}",
                                        "HX-Request": "true"})
    resp2 = await panel._handle_screen_data(req2)
    assert resp2.status == 200
    assert "screen-trend" in resp2.text and "房间今日排行" in resp2.text


@pytest.mark.asyncio
async def test_screen_context_batches_room_summaries():
    """大屏房间今日排行：多房间批量get_daily_summaries恰1次，不再逐房串行N+1"""
    from blivepost.services.webpanel import WebPanel

    async def _fake_collect():
        return {"now": "t", "bot": {"client_started": True, "token_ready": True,
                "token_expires_in": 3600},
                "me": {"id": "9", "username": "bot", "avatar": "", "share_url": ""},
                "app_id": "100", "db_size_mb": 1.2,
                "accounts": [], "rooms": [],
                "dynamics": [], "recent": []}

    class FakeDB:
        """计数器桩：记录批量/逐房汇总与主播名查询各自的调用次数"""
        batch_calls = 0
        single_calls = 0
        room_uid_calls = 0
        streamer_name_calls = 0

        async def list_room_ids(self):
            return [101, 102, 103]

        async def get_daily_summaries(self, room_ids, day=None):
            FakeDB.batch_calls += 1
            assert list(room_ids) == [101, 102, 103] and day is not None
            return {101: {"danmaku_count": 5, "gift_gold": 10.0,
                          "ship_total": 0, "sc_total": 0},
                    102: {"danmaku_count": 1, "gift_gold": 50.0,
                          "ship_total": 30.0, "sc_total": 20.0},
                    103: {"danmaku_count": 0, "gift_gold": 0,
                          "ship_total": 0, "sc_total": 0}}

        async def get_daily_summary(self, rid, day=None):
            FakeDB.single_calls += 1
            return {}

        async def get_room_uid(self, rid):
            FakeDB.room_uid_calls += 1
            return 0

        async def get_streamer_name(self, uid):
            FakeDB.streamer_name_calls += 1
            return ""

        async def get_daily_history_range(self, start, end, room_ids=None):
            return []

        async def load_groups(self, active_only=False):
            return []

    panel = WebPanel(None, None, None, None, None, bot_client=None)
    panel._collect = _fake_collect
    panel.db_service = FakeDB()

    ctx = await panel._screen_context()
    # 批量汇总恰1次；旧逐房路径（get_daily_summary/get_room_uid/
    # get_streamer_name逐房串行）不再被触发
    assert FakeDB.batch_calls == 1
    assert FakeDB.single_calls == 0
    assert FakeDB.room_uid_calls == 0
    assert FakeDB.streamer_name_calls == 0
    # 排序按 gold+ship+sc 降序：102(100)>101(10)>103(0)
    rooms = ctx["rooms_today"]
    assert [r["room_id"] for r in rooms] == [102, 101, 103]
    assert rooms[0]["gold"] == 50.0 and rooms[0]["ship"] == 30.0
    assert rooms[1]["danmaku"] == 5 and rooms[1]["gold"] == 10.0
    # binding_store为None→主播名回落房号占位
    assert rooms[2]["streamer"] == "房间103"
    assert ctx["trend7"] == [] and ctx["group_count"] == 0


def test_logger_debug_gating(capsys):
    """DEBUG日志仅在debug开关开启时输出；jieba日志级别随开关联动"""
    import logging

    from blivepost.utils.logger import Logger

    logger = Logger().setup(name="blivepost", console=True, debug=False)
    logger.debug("debug隐藏")
    logger.info("info显示")
    out = capsys.readouterr().out
    assert "debug隐藏" not in out and "info显示" in out

    Logger().apply_debug(True)
    logger.debug("debug显示")
    out2 = capsys.readouterr().out
    assert "debug显示" in out2
    assert logger.level == logging.DEBUG
    assert logging.getLogger("jieba").level == logging.DEBUG

    Logger().apply_debug(False)
    assert logger.level == logging.INFO
    assert logging.getLogger("jieba").level == logging.WARNING


# ==================== 面板数据收集并行化/TTL缓存（N+1查询扇出优化） ====================


async def test_collect_multi_room_batch_and_parallel():
    """多房间_collect：批量汇总接口一次扇出全部房间，行数据完整且顺序稳定"""
    panel = _make_panel()
    calls = {"batch": 0, "names": []}

    class D:
        requested = None

        @staticmethod
        async def get_daily_summaries(room_ids, day=None):
            calls["batch"] += 1
            D.requested = list(room_ids)
            return {rid: {"day": "t", "danmaku_count": rid * 10, "gift_gold": 1.5,
                          "gift_count": 0, "ship_total": 2.0, "ship_count": 0,
                          "sc_total": 3, "sc_count": 0, "red_pocket": 0}
                    for rid in room_ids}

        @staticmethod
        async def get_streamer_name(uid):
            calls["names"].append(uid)
            return f"主播{uid}"

        @staticmethod
        async def get_recent_live_status(limit=10):
            return []

    class MM:
        @staticmethod
        def room_ids():
            return [101, 202, 303]

        @staticmethod
        def has(rid):
            return rid != 303

        @staticmethod
        def get_cookie_uid(rid):
            return rid + 1

    class BS:
        @staticmethod
        def get_monitored_groups(rid):
            return ["G1"]

        @staticmethod
        def get_binding(group, rid):
            class B:
                uid = rid + 1000
            return B()

        @staticmethod
        def get_dynamic_up_uids():
            return []

    panel.db_service = D()
    panel.monitor_manager = MM()
    panel.binding_store = BS()
    panel._streamer_names.clear()

    data = await panel._collect()
    # 汇总批量一次拿到全部房间（不再逐房串行get_daily_summary）
    assert calls["batch"] == 1 and D.requested == [101, 202, 303]
    assert [r["room_id"] for r in data["rooms"]] == [101, 202, 303]
    assert data["rooms"][0]["danmaku"] == 1010
    assert data["rooms"][1]["gold"] == 1.5 and data["rooms"][1]["sc"] == 3
    assert data["rooms"][0]["streamer"] == "主播1101"
    assert data["rooms"][0]["alive"] is True and data["rooms"][2]["alive"] is False
    assert sorted(calls["names"]) == [1101, 1202, 1303]


async def test_streamer_name_ttl_cache_no_db_requery():
    """TTL缓存：首次查库，缓存命中期间二次调用不触库；到期后重新查库"""
    import time as _time

    panel = _make_panel()
    counter = {"n": 0}

    class D:
        @staticmethod
        async def get_streamer_name(uid):
            counter["n"] += 1
            return "墨翎"

    class BS:
        @staticmethod
        def get_monitored_groups(rid):
            return ["G1"]

        @staticmethod
        def get_binding(group, rid):
            class B:
                uid = 42
            return B()

    panel.db_service = D()
    panel.binding_store = BS()
    panel._streamer_names.clear()

    assert await panel._streamer_name(7) == "墨翎"
    assert counter["n"] == 1
    assert await panel._streamer_name(7) == "墨翎"  # 缓存命中：不再触库
    assert counter["n"] == 1

    # 缓存过期 → 重新查库
    panel._streamer_names[7] = (_time.monotonic() - 0.001, "旧名")
    assert await panel._streamer_name(7) == "墨翎"
    assert counter["n"] == 2


async def test_get_daily_summaries_batch(tmp_path):
    """批量汇总：一次调用返回各房间当日汇总，与单房get_daily_summary语义一致"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    await svc.save_danmaku(room_id=100, uid=1, uname="u", msg="a")
    await svc.save_gift(room_id=200, uid=1, uname="u", gift_id=1,
                        gift_name="g", num=1, price=1000, coin_type="gold")
    summaries = await svc.get_daily_summaries([100, 200, 300])
    assert set(summaries) == {100, 200, 300}
    assert summaries[100]["danmaku_count"] == 1 and summaries[100]["gift_gold"] == 0
    assert summaries[200]["danmaku_count"] == 0 and summaries[200]["gift_gold"] == 1.0
    assert summaries[300]["danmaku_count"] == 0  # 无数据房间零值汇总
    assert summaries[100] == await svc.get_daily_summary(100)  # 等价单房查询
    assert await svc.get_daily_summaries([]) == {}
    await db.close()
