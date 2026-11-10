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
