# -*- coding: utf-8 -*-
"""第十二批新功能：use_count、自动重扫、token鉴权、history测试"""

import pytest

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


@pytest.mark.asyncio
async def test_token_auth_required():
    panel = _make_panel(token="SECRET")
    class Req:
        query = {}
        headers = {}
        cookies = {}
    req = Req()
    resp = await panel._handle_index(req)
    assert resp.status == 401
    resp2 = await panel._handle_history(req)
    assert resp2.status == 401
    resp3 = await panel._handle_summary(req)
    assert resp3.status == 401
    resp4 = await panel._handle_trend(req)
    assert resp4.status == 401


@pytest.mark.asyncio
async def test_token_auth_via_query(monkeypatch, tmp_path):
    """携带正确token时放行（home渲染空数据集），并下发cookie供HTMX局部请求使用"""
    panel = _make_panel(token="SECRET")
    class D:
        @staticmethod
        async def get_daily_history(days=30): return []
        @staticmethod
        async def get_daily_history_range(start_day, end_day): return []
        @staticmethod
        async def get_recent_live_status(limit=10): return []
    panel.db_service = D()

    async def fake_collect():
        return dict(_FULL_COLLECT)
    panel._collect = fake_collect

    class Req:
        query = {"token": "SECRET"}
        headers = {}
        cookies = {}
    resp = await panel._handle_index(Req())
    assert resp.status == 200
    body = resp.text
    # HTMX自动刷新 + viewport + 趋势图容器 + 机器人详情
    assert "hx-trigger" in body and "viewport" in body and 'id="trend"' in body
    assert "bot" in body

    # 局部摘要请求凭cookie放行
    class CookieReq:
        query = {}
        headers = {}
        cookies = {"web_token": "SECRET"}
    resp2 = await panel._handle_summary(CookieReq())
    assert resp2.status == 200
    assert "在线" in resp2.text and "token剩余" in resp2.text

    # 趋势局部：12个月按月汇总模式
    class MonthReq:
        query = {"range": "12m"}
        headers = {}
        cookies = {"web_token": "SECRET"}
    resp3 = await panel._handle_trend(MonthReq())
    assert resp3.status == 200
    assert "按月汇总" in resp3.text


@pytest.mark.asyncio
async def test_token_via_header():
    panel = _make_panel(token="SECRET")
    seen = {}
    panel._collect = lambda: _fake_collect(seen)
    panel._check_token_original = panel._check_token

    class Req:
        query = {}
        headers = {"X-Web-Token": "SECRET"}

    # 头携带token应放行
    assert panel._check_token(Req()) is True


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
    from datetime import datetime, timedelta
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    now = datetime.now()
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
