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
    async def get_recent_live_status(limit=10):
        return []


def _panel(token=""):
    return WebPanel(FakePool(), FakeMM(), FakeDyn(), FakeStore(), D(),
                    token=token, bot_client=FakeBot())


@pytest.mark.asyncio
async def test_panel_token_required():
    panel = _panel(token="SECRET")
    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/")
    resp = await panel._handle_home(req)
    assert resp.status == 401


@pytest.mark.asyncio
async def test_panel_token_ok_renders_credentials(monkeypatch):
    panel = _panel(token="SECRET")
    async def fake_collect():
        return {"now": "t", "bot": {"client_started": True, "token_ready": True,
                "token_expires_in": 3600}, "db_size_mb": 1.2, "accounts": [],
                "rooms": [], "dynamics": [], "recent": []}
    panel._collect = fake_collect

    from aiohttp.test_utils import make_mocked_request
    req = make_mocked_request("GET", "/?token=SECRET")
    resp = await panel._handle_home(req)
    assert resp.status == 200
    body = resp.text
    assert "token剩余" in body and "在线" in body


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
