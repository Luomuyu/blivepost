# -*- coding: utf-8 -*-
"""第十五批：v1.2.0清理与硬化测试

1. LiveMonitor移除（import失败）
2. botpy_compat适配层
3. 会话吊销（jti黑名单）
"""


# ---------- 1. LiveMonitor移除 ----------

def test_live_monitor_removed():
    """LiveMonitor不可导入；替代品MonitorManager仍可用"""
    import blivepost
    assert not hasattr(blivepost, 'LiveMonitor')
    from blivepost import MonitorManager
    assert MonitorManager is not None


# ---------- 2. botpy_compat ----------

async def test_botpy_compat_valid_structure():
    """正常botpy结构：提取HTTP通道并发起请求"""
    from blivepost.services.botpy_compat import api_request, get_http

    class FakeHttp:
        async def request(self, route, **kw):
            return {"ok": True, "echo": kw.get("json")}

    class FakeApi:
        _http = FakeHttp()

    class FakeClient:
        api = FakeApi()

    http = get_http(FakeClient())
    assert http is not None

    class FakeRoute:
        path = "/v2/test"

    res = await api_request(FakeClient(), FakeRoute(), json={"a": 1})
    assert res["ok"] is True and res["echo"] == {"a": 1}


async def test_botpy_compat_broken_structure():
    """botpy结构变化：返回None不崩溃"""
    from blivepost.services.botpy_compat import api_request, get_http

    class BrokenApi:
        _http = None  # _http为None → AttributeError

    class BrokenClient:
        api = BrokenApi()

    assert get_http(BrokenClient()) is None
    res = await api_request(BrokenClient(), "route")
    assert res is None


# ---------- 3. 会话吊销 ----------

async def test_session_revocation():
    """登出吊销：sid验签通过但jti在黑名单→无效；未吊销sid正常"""
    from blivepost.services.webpanel import WebPanel

    panel = WebPanel(None, None, None, None, None, token="TK")
    sid1 = panel._create_session("alice", False)
    sid2 = panel._create_session("bob", True)

    # 未吊销：两sid均有效
    assert panel._session_from_sid(sid1) is not None
    assert panel._session_from_sid(sid2) is not None
    assert panel._session_from_sid(sid1)["user"] == "alice"

    # 吊销sid1 → 无效；sid2不受影响
    assert panel.revoke_session(sid1) is True
    assert panel._session_from_sid(sid1) is None       # 已吊销
    assert panel._session_from_sid(sid2) is not None   # 未吊销

    # 无效sid吊销 → False
    assert panel.revoke_session("garbage") is False


async def test_logout_revokes_session():
    """登出动作调用revoke（cookie残留重放防护）"""
    from blivepost.services.webpanel import WebPanel
    panel = WebPanel(None, None, None, None, None, token="TK")
    sid = panel._create_session("alice", False)
    assert panel._session_from_sid(sid) is not None

    class FakeReq:
        cookies = {"blivepost_sid": sid}
        remote = "127.0.0.1"
    try:
        resp = await panel._action_web_logout(FakeReq())
        assert resp.status == 302
    except Exception as e:
        # aiohttp HTTPFound raised as exception → 也是正常的302重定向
        assert hasattr(e, 'status') and e.status == 302
    # 登出后sid已吊销
    assert panel._session_from_sid(sid) is None
