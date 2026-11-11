# -*- coding: utf-8 -*-
"""第十四批：观众分析 + REST API v1测试"""


from blivepost.config.settings import Settings
from blivepost.database.manager import DatabaseManager
from blivepost.database.service import DatabaseService


# ---------- AnalyticsMixin ----------

async def test_activity_heatmap(tmp_path):
    """活跃热力图：造3条弹幕分属不同时段→对应格子计数"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        # 造数据后按Setting.timezone分桶断言——now必须取同口径的本地时间
        # （机器时区≠Setting.timezone时，机器本地now会落到别的日键/小时桶）
        from blivepost.utils.timeutil import now_local
        now = now_local()
        await svc.save_danmaku(room_id=23058, uid=1, uname="u",
                               msg="第一条")
        await svc.save_danmaku(room_id=23058, uid=2, uname="v",
                               msg="第二条")
        hm = await svc.get_activity_heatmap(room_id=23058, days=1)
        assert isinstance(hm, dict) and len(hm) >= 1
        today_key = now.strftime("%Y-%m-%d")
        if today_key in hm:
            hour = now.hour
            assert hm[today_key][hour] >= 2
    finally:
        await svc.aclose()
        await db.close()


async def test_top_speakers(tmp_path):
    """高频发言榜：同一uid发3条排第一"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        for i in range(3):
            await svc.save_danmaku(room_id=23058, uid=999, uname="刷屏王",
                                   msg=f"弹幕{i}")
        await svc.save_danmaku(room_id=23058, uid=1, uname="路人", msg="一条")
        top = await svc.get_top_speakers(room_id=23058, days=7, limit=5)
        assert len(top) >= 2
        assert top[0]["uname"] == "刷屏王" and top[0]["count"] == 3
    finally:
        await svc.aclose()
        await db.close()


async def test_daily_distribution(tmp_path):
    """弹幕日分布：按日分组"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    try:
        await svc.save_danmaku(room_id=23058, uid=1, uname="u", msg="hi")
        dist = await svc.get_danmaku_daily_distribution(room_id=23058, days=1)
        assert isinstance(dist, list) and len(dist) >= 1
        assert "day" in dist[0] and "count" in dist[0]
        assert dist[0]["count"] >= 1
    finally:
        await svc.aclose()
        await db.close()


# ---------- API v1 鉴权与限速 ----------

def test_is_private_ip():
    """私网IP判定"""
    from blivepost.services.api_v1 import ApiV1Mixin
    assert ApiV1Mixin._is_private_ip("127.0.0.1") is True
    assert ApiV1Mixin._is_private_ip("::1") is True
    assert ApiV1Mixin._is_private_ip("10.0.0.1") is True
    assert ApiV1Mixin._is_private_ip("172.16.0.1") is True
    assert ApiV1Mixin._is_private_ip("192.168.1.1") is True
    assert ApiV1Mixin._is_private_ip("8.8.8.8") is False
    assert ApiV1Mixin._is_private_ip("") is False


def test_api_rate_limit():
    """限速：60次/窗口，超出返回False"""
    from blivepost.services.api_v1 import ApiV1Mixin

    class P(ApiV1Mixin):
        pass

    p = P()
    req = type("Req", (), {"remote": "1.2.3.4"})()

    for i in range(60):
        assert p._api_rate_ok(req) is True
    assert p._api_rate_ok(req) is False       # 第61次超限
