# -*- coding: utf-8 -*-
"""第十二批新功能：use_count、自动重扫、token鉴权、history测试"""

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.cookie_pool import CookiePool
from blivepost.services.webpanel import _render_trend_svg


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


def _make_panel(token=""):
    from blivepost.services.webpanel import WebPanel
    return WebPanel(None, None, None, None, None, token=token)


@pytest.mark.asyncio
async def test_token_auth_required():
    panel = _make_panel(token="SECRET")
    class Req:
        query = {}
        headers = {}
    req = Req()
    resp = await panel._handle_home(req)
    assert resp.status == 401
    resp2 = await panel._handle_history(req)
    assert resp2.status == 401


@pytest.mark.asyncio
async def test_token_auth_via_query(monkeypatch, tmp_path):
    """携带正确token时放行（home渲染空数据集）"""
    panel = _make_panel(token="SECRET")
    class D:
        @staticmethod
        async def get_daily_history(days=30): return []
        @staticmethod
        async def get_recent_live_status(limit=10): return []
    panel.db_service = D()

    async def fake_collect():
        return {"now": "t", "accounts": [], "rooms": [], "dynamics": [], "recent": []}
    panel._collect = fake_collect

    import blivepost.services.webpanel as wp
    monkeypatch.setattr(wp, "_render_trend_svg", lambda h: "")

    class Req:
        query = {"token": "SECRET"}
        headers = {}
    resp = await panel._handle_home(Req())
    assert resp.status == 200


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


def test_render_trend_svg():
    history = [
        {"day": f"2026-08-{d:02d}", "danmaku": d, "gift_gold": d * 2,
         "ship_gold": d, "sc_total": 0}
        for d in range(1, 31)
    ]
    svg = _render_trend_svg(history)
    assert svg.startswith("<svg") and "polyline" in svg
    assert svg.count("<polyline") == 4  # 四条序列


def test_render_trend_svg_empty():
    assert "暂无" in _render_trend_svg([])


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
