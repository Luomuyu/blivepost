# -*- coding: utf-8 -*-
"""数据治理（/体积）与终端stop命令测试"""

import asyncio

import pytest

from blivepost.config.settings import Settings
from blivepost.database import DatabaseManager, DatabaseService
from blivepost.services.admin_commands import AdminCommandHandler


class FakeAuthor:
    def __init__(self, openid="ADMIN"):
        self.user_openid = openid


class FakeMsg:
    def __init__(self, content, openid="ADMIN"):
        self.author = FakeAuthor(openid)
        self.content = content
        self.replies = []

    async def reply(self, **kw):
        self.replies.append(kw.get("content", ""))


class FakeBot:
    async def send_c2c_message(self, *a, **kw):
        return True


@pytest.fixture
async def env(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "t.db")
    db = DatabaseManager(settings.database)
    await db.initialize()
    svc = DatabaseService(db)
    handler = AdminCommandHandler(None, ["ADMIN"], bot_client=FakeBot(), db_service=svc)
    # 接入db_path（main装配时DatabaseManager.init已设置）
    await db.initialize()  # 幂等
    yield handler, svc, db
    await db.close()


async def test_size_report_command(env):
    handler, svc, _ = env
    await svc.save_danmaku(room_id=1, uid=1, uname="u", msg="hi")
    await svc.save_gift(room_id=1, uid=1, uname="u", gift_id=1, gift_name="g",
                        num=1, price=100, coin_type="gold")
    r = await handler._db_size_report()
    assert "数据库体积" in r and "MB" in r
    assert "弹幕：1条" in r and "礼物：1条" in r
    assert "空表" in r


async def test_size_report_no_db_path(env):
    """非sqlite（db_path为空）时提示不可用"""
    handler, _, _ = env
    handler.db_service.db_manager.db_path = None
    r = await handler._db_size_report()
    assert "不可用" in r


async def test_terminal_stop_command_checked():
    """_listen_terminal对stop/quit/exit置位事件（输入mock到线程）"""
    import io
    from blivepost.__main__ import _listen_terminal

    async def main():
        stop_event = asyncio.Event()
        fake_stdin = io.StringIO("stop\n")
        import sys as _sys
        real_stdin = _sys.stdin
        _sys.stdin = fake_stdin
        try:
            await asyncio.wait_for(_listen_terminal(stop_event), timeout=5)
        finally:
            _sys.stdin = real_stdin
        return stop_event.is_set()

    assert await main() is True


async def test_terminal_ignores_other_input():
    """非stop输入不触发停止"""
    import io
    import sys as _sys
    from blivepost.__main__ import _listen_terminal

    async def main():
        stop_event = asyncio.Event()
        fake_stdin = io.StringIO("hello\nstop\n")
        real_stdin = _sys.stdin
        _sys.stdin = fake_stdin
        try:
            await asyncio.wait_for(_listen_terminal(stop_event), timeout=5)
        finally:
            _sys.stdin = real_stdin
        return stop_event.is_set()

    assert await main() is True
