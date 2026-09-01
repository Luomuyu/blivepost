# -*- coding: utf-8 -*-
"""指纹服务回归：get_cookie全链路不因导入缺失崩溃（审查发现的历史bug）"""

from blivepost.config.settings import Settings
from blivepost.services.fingerprint import FingerprintService


def _fake_json(payload):
    class R:
        def json(self):
            return payload
    return R()


def test_get_cookie_full_chain(monkeypatch):
    """get_cookie完整链路（buvid/fingerprint/sid mock，防NameError回归）"""
    from blivepost.services import fingerprint as fp_mod

    def fake_get(url=None, **kw):
        url = url or kw.get("url", "")
        if "finger/spi" in url:
            return _fake_json({"data": {"b_3": "b3", "b_4": "b4"}})
        if "fpfmc" in url:
            return _fake_json({"data": {"buvid_fp": "fp"}})
        if "player/wbi" in url:
            class R2:
                headers = {"Set-Cookie": "a=1; sid=S123; path=/"}
                def json(self):
                    return {}
            return R2()
        return _fake_json({})

    monkeypatch.setattr(fp_mod.requests, "get", fake_get)
    # 无Redis环境快速降级（不测Redis本身）
    monkeypatch.setattr(fp_mod.RedisCache, "_connect_failed", True)

    svc = FingerprintService(Settings(), sessdata="SESS_TEST")
    cookie = svc.get_cookie()
    assert "SESSDATA=SESS_TEST" in cookie
    assert "buvid3=b3" in cookie and "buvid_fp=fp" in cookie and "sid=S123" in cookie


def test_get_cookie_async(monkeypatch):
    """异步入口可用（网络mock，不真实请求）"""
    import asyncio
    from blivepost.services import fingerprint as fp_mod
    from blivepost.services.fingerprint import FingerprintService

    monkeypatch.setattr(fp_mod.requests, "get",
                        lambda *a, **kw: _fake_json({"data": {"b_3": "b", "b_4": "b"}}))
    monkeypatch.setattr(fp_mod.RedisCache, "_connect_failed", True)
    monkeypatch.setattr(fp_mod.FingerprintService.get_fingerprint, "__self__", None) if False else None

    svc = FingerprintService(Settings(), sessdata="X")
    # get_fingerprint内部也走网络，一并mock
    monkeypatch.setattr(svc, "get_fingerprint", lambda: ("f", "fp"))
    monkeypatch.setattr(svc, "get_sid", lambda: "S1")
    cookie = asyncio.run(svc.get_cookie_async())
    assert "SESSDATA=X" in cookie
