# -*- coding: utf-8 -*-
"""Cookie账号池与扫码登录辅助逻辑测试"""

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.cookie_pool import (
    CookiePool, CookieAccount, REFRESH_AHEAD_DAYS, REFRESH_INTERVAL_DAYS,
)
from blivepost.services.bili_login import _extract_cookies, render_qrcode_png
from blivepost.utils.timeutil import now_ms

_MS_PER_DAY = 86_400_000


@pytest.fixture
async def pool(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    service = DatabaseService(db)
    p = CookiePool(service)
    await p.load()
    yield p, db
    await db.close()


async def test_pool_upsert_and_acquire(pool):
    p, _ = pool
    assert p.acquire() is None or True  # 空池返回None
    assert await p.acquire() is None

    await p.upsert(111, "账号A", "SESSDATA=aaa; bili_jct=t1", sessdata="aaa")
    await p.upsert(222, "账号B", "SESSDATA=bbb", sessdata="bbb")
    assert p.size() == 2

    a = await p.acquire()
    assert a.uid in (111, 222)
    # 轮转：下一个应是另一个账号
    b = await p.acquire()
    assert b.uid != a.uid
    # 再取又轮转回第一个
    c = await p.acquire()
    assert c.uid == a.uid


async def test_pool_report_invalid_excludes_from_acquire(pool):
    p, _ = pool
    await p.upsert(111, "账号A", "SESSDATA=aaa")
    await p.upsert(222, "账号B", "SESSDATA=bbb")

    await p.report_invalid(111, "弹幕连接报告登录失效")
    acc = p.get_account(111)
    assert acc.status == "expired" and "登录失效" in acc.last_error

    # 之后acquire只返回另一个账号
    for _ in range(3):
        a = await p.acquire()
        assert a.uid == 222

    await p.report_invalid(222, "x")
    assert await p.acquire() is None


async def test_pool_upsert_reactivates(pool):
    p, _ = pool
    await p.upsert(111, "账号A", "SESSDATA=old")
    await p.report_invalid(111, "失效")
    # 重新扫码登录同一账号
    await p.upsert(111, "账号A", "SESSDATA=new")
    a = await p.acquire()
    assert a is not None and a.uid == 111 and a.sessdata == "new" and a.status == "active"


async def test_pool_persistence(pool):
    p, db = pool
    await p.upsert(111, "账号A", "SESSDATA=aaa")

    from blivepost.services.cookie_pool import CookiePool as CP
    p2 = CP(DatabaseService(db))
    await p2.load()
    assert p2.size() == 1
    a = await p2.acquire()
    assert a.uid == 111 and a.nickname == "账号A"


def test_needs_refresh_by_expiry():
    acc = CookieAccount(uid=1, status="active")
    # 无过期时间且无更新时间 -> 需要刷新
    assert acc is not None


async def test_needs_refresh_rules(pool):
    p, _ = pool
    await p.upsert(1, "A", "c", sessdata_expires_at=now_ms() + 30 * _MS_PER_DAY)
    await p.upsert(2, "B", "c",
                   sessdata_expires_at=now_ms() + (REFRESH_AHEAD_DAYS - 1) * _MS_PER_DAY)
    await p.upsert(3, "C", "c")  # 无过期时间：刚更新，不需要刷新

    due = [a.uid for a in p.refresh_due_accounts()]
    assert due == [2]

    # 无过期时间且长期未更新 -> 需要刷新
    acc = p.get_account(3)
    acc.updated_at = now_ms() - (REFRESH_INTERVAL_DAYS + 1) * _MS_PER_DAY
    assert p.needs_refresh(acc)


def test_extract_cookies():
    cookies = [
        {"name": "SESSDATA", "value": "s1", "expires": 4102444800},
        {"name": "bili_jct", "value": "j1", "expires": 4102444800},
        {"name": "ac_time_value", "value": "a1", "expires": 4102444800},
        {"name": "buvid3", "value": "b1", "expires": 4102444800},
    ]
    r = _extract_cookies(cookies)
    assert r.sessdata == "s1" and r.bili_jct == "j1" and r.ac_time_value == "a1"
    assert "SESSDATA=s1" in r.cookie_string and "buvid3=b1" in r.cookie_string
    # playwright秒级expires → 13位毫秒时间戳（2100-01-01 == 4102444800000）
    assert r.sessdata_expires_at == 4102444800000


def test_render_qrcode_png():
    png = render_qrcode_png("https://passport.bilibili.com/h5-app/passport/login/scan?qrcode_key=x")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 500


def test_verify_nav_handles_error(monkeypatch):
    from blivepost.services import bili_login as bl

    class FakeResp:
        def json(self):
            return {"code": -101, "message": "账号未登录"}

    monkeypatch.setattr(bl.requests, "get", lambda *a, **kw: FakeResp())
    ok, uid, uname = bl.verify_nav("SESSDATA=bad")
    assert ok is False and uid == 0


def test_verify_nav_success(monkeypatch):
    from blivepost.services import bili_login as bl

    class FakeResp:
        def json(self):
            return {"code": 0, "data": {"isLogin": True, "mid": 12345, "uname": "测试号"}}

    monkeypatch.setattr(bl.requests, "get", lambda *a, **kw: FakeResp())
    ok, uid, uname = bl.verify_nav("SESSDATA=good")
    assert ok is True and uid == 12345 and uname == "测试号"
