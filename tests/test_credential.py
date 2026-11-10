# -*- coding: utf-8 -*-
"""访问凭证状态与面板token鉴权测试"""

import pytest

from blivepost.services.qqbot import QQBotClient
from blivepost.services.webpanel import WebPanel


class FakeToken:
    """botpy Token桩（getAppAccessToken刷新后的形态）"""

    access_token = "TOK123"
    expires_in = 1799999  # epoch秒


class FakeHttp:
    _token = FakeToken()


class FakeApiClient:
    _http = FakeHttp()


class FakeInnerClient:
    api = FakeApiClient()


def test_token_state_ready():
    """凭证就绪：剩余有效期正确计算"""
    import time

    c = QQBotClient("1", "2")
    c._client = FakeInnerClient()
    # 重写expires为未来时间
    FakeToken.expires_in = int(time.time()) + 3600
    state = c.token_state()
    assert state["token_ready"] is True
    assert 3500 <= state["token_expires_in"] <= 3600
    assert state["client_started"] is False  # 未start


def test_token_state_no_token():
    """无token（未登录）时状态为未就绪"""
    c = QQBotClient("1", "2")
    c._client = FakeInnerClient()
    FakeToken.access_token = None
    state = c.token_state()
    assert state["token_ready"] is False
    FakeToken.access_token = "TOK123"


def test_token_state_expired_shows_zero():
    """token过期（expires_in已过）显示剩余0秒"""
    import time

    c = QQBotClient("1", "2")
    c._client = FakeInnerClient()
    FakeToken.expires_in = int(time.time()) - 10
    state = c.token_state()
    assert state["token_expires_in"] == 0


# ==================== 面板token鉴权 ====================


class FakePool:
    accounts = []


class FakeMM:
    def room_ids(self):
        return []

    def get_account_rooms(self, uid):
        return []

    def has(self, rid):
        return False

    def get_cookie_uid(self, rid):
        return 0


class FakeDyn:
    def has(self, uid):
        return False


class FakeStore:
    def get_dynamic_up_uids(self):
        return []

    def get_dynamic_up_name(self, uid):
        return ""

    def get_dynamic_groups(self, uid):
        return []


class FakeBot:
    def token_state(self):
        return {"client_started": True, "token_ready": True, "token_expires_in": 3600}


class D:
    @staticmethod
    async def get_daily_history(days=30):
        return []

    @staticmethod
    async def get_daily_history_range(start_day, end_day, room_ids=None):
        return []

    @staticmethod
    async def get_recent_live_status(limit=10):
        return []

    @staticmethod
    async def list_room_ids():
        return []


def _panel(token=""):
    return WebPanel(FakePool(), FakeMM(), FakeDyn(), FakeStore(), D(),
                    token=token, bot_client=FakeBot())


def _full_collect():
    return {"now": "t", "bot": {"client_started": True, "token_ready": True,
            "token_expires_in": 3600},
            "me": {"id": "9", "username": "bot", "avatar": "", "share_url": ""},
            "app_id": "100", "db_size_mb": 1.2,
            "accounts": [], "rooms": [], "dynamics": [], "recent": []}


@pytest.mark.asyncio
async def test_panel_guest_renders_credentials():
    """新鉴权模型：页面公开（游客只读），概览含凭证与在线状态"""
    panel = _panel()
    panel._collect = lambda: _full_collect_async()

    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/")
    resp = await panel._make_page_handler("overview")(req)
    assert resp.status == 200
    body = resp.text
    assert "token剩余" in body and "在线" in body
    assert "游客" in body  # 默认游客身份，可点击登录


@pytest.mark.asyncio
async def test_panel_token_quick_login():
    """令牌快捷登录收窄（v1.2.0）：/?token=不再建会话（游客200）；
    /login?token=建管理员会话；携会话渲染后台入口"""
    panel = _panel(token="SECRET")
    panel._collect = lambda: _full_collect_async()

    from aiohttp.test_utils import make_mocked_request
    # 普通页?token= → 游客视图（200、无会话cookie）
    req = make_mocked_request("GET", "/?token=SECRET")
    resp = await panel._make_page_handler("overview")(req)
    assert resp.status == 200
    assert "blivepost_sid" not in resp.cookies

    # /login?token= → 建管理员会话重定向
    login_resp = await panel._handle_login_page(
        make_mocked_request("GET", "/login?token=SECRET"))
    assert login_resp.status == 302
    sid = login_resp.cookies["blivepost_sid"].value
    assert panel._session_from_sid(sid)["admin"] is True
    # 携会话渲染
    req2 = make_mocked_request("GET", "/", headers={"Cookie": f"blivepost_sid={sid}"})
    resp2 = await panel._make_page_handler("overview")(req2)
    assert resp2.status == 200
    assert "后台" in resp2.text


async def _full_collect_async():
    return _full_collect()


@pytest.mark.asyncio
async def test_get_me_parses_dict_response():
    """get_me正确解析botpy返回的dict（此前按对象属性取值恒为空串）"""
    import asyncio

    class FakeAPI:
        async def me(self):
            return {"id": "123", "username": "测试机器人",
                    "avatar": "http://example.com/a.png", "bot": True}

    class FakeInner:
        api = FakeAPI()

    c = object.__new__(QQBotClient)
    c._me_cache = None
    c._client = FakeInner()

    async def _hang():
        await asyncio.Event().wait()
    c._task = asyncio.get_running_loop().create_task(_hang())

    me = await c.get_me()
    assert me["username"] == "测试机器人"
    assert me["id"] == "123"
    assert me["avatar"] == "http://example.com/a.png"
    c._task.cancel()


async def fake_collect():
    return {"now": "t", "bot": {"client_started": True, "token_ready": True,
                                "token_expires_in": 3600},
            "db_size_mb": 1.2, "accounts": [], "rooms": [], "dynamics": [],
            "recent": []}


def test_note_send_denied_dedup():
    """40034105识别为主动消息权限拒绝并按群低频提示；非权限错误不命中"""
    c = object.__new__(QQBotClient)
    c._deny_warned = {}
    denied = Exception("[40034105] 主动消息失败, 无权限")
    assert c._note_send_denied("G1", denied) is True
    ts = c._deny_warned["G1"]
    assert c._note_send_denied("G1", denied) is True  # 冷却期内命中但不重复提示
    assert c._deny_warned["G1"] == ts
    assert c._note_send_denied("G1", Exception("connect timeout")) is False
    assert "40034105" not in str(Exception("connect timeout"))


def test_debug_author_log_gated(monkeypatch, caplog):
    """debug模式输出消息发送者身份明细；关闭时不输出"""
    import logging

    from blivepost.services.qqbot import _PushClient

    class FakeAuthor:
        user_openid = "ou_a"
        union_openid = "uu_1"
        username = "测试用户"
        member_openid = None  # 未携带的字段不输出

    c = object.__new__(_PushClient)

    class Msg:
        group_openid = "GABC"

    c._debug = True
    with caplog.at_level(logging.INFO, logger="blivepost.qqbot"):
        c._debug_log_message("群聊", Msg(), FakeAuthor())
    text = caplog.text
    assert "[debug] 收到群聊消息" in text
    assert "user_openid=ou_a" in text and "union_openid=uu_1" in text
    assert "username=测试用户" in text
    assert "group_openid=GABC" in text
    assert "member_openid" not in text  # None字段不输出

    c._debug = False
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="blivepost.qqbot"):
        c._debug_log_message("群聊", Msg(), FakeAuthor())
    assert caplog.text == ""


def test_setting_debug_parse():
    """Setting.debug配置解析：Y/N"""
    import tempfile
    from pathlib import Path

    from blivepost.config.settings import Settings

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "c.yaml"
        p.write_text(
            "Setting:\n  debug: \"Y\"\n", encoding="utf-8")
        s = Settings.load(str(p))
        assert s.message.debug is True
        p.write_text(
            "Setting:\n  debug: \"N\"\n", encoding="utf-8")
        s2 = Settings.load(str(p))
        assert s2.message.debug is False
