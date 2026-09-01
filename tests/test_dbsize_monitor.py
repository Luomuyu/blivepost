# -*- coding: utf-8 -*-
"""数据库体积监控端到端测试（真实DatabaseService→db_manager→db_path链路）"""

import asyncio
import os
import tempfile

import pytest

from blivepost.config.settings import Settings
from blivepost.core.manager import CookieMaintainer
from blivepost.database import DatabaseManager, DatabaseService


class NotifyBot:
    def __init__(self):
        self.notified = []

    async def notify_admins(self, admins, content):
        self.notified.append((list(admins), content))
        return len(admins)


def _make_maintainer(db_service, bot, admins=(), main=()):
    m = CookieMaintainer.__new__(CookieMaintainer)
    m.pool = None
    m.bot_client = bot
    m.admin_openids = list(admins)
    m.main_admin_openids = list(main)
    m.db_service = db_service
    m.db_size_warn_mb = 500
    m._db_warned = False
    m._last_rescan_date = ""
    return m


def test_check_db_size_real_chain(tmp_path):
    """真链路：DatabaseService→db_manager→db_path 取体积并触发提醒"""
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "real.db")
    db = DatabaseManager(settings.database)
    asyncio.run(db.initialize())
    svc = DatabaseService(db)

    bot = NotifyBot()
    m = _make_maintainer(svc, bot, admins=["ADMIN1"], main=["MAIN1"])

    # 阈值0.001MB：新库文件必超 → 提醒且只提醒一次
    asyncio.run(m._check_db_size(0.001))
    assert len(bot.notified) == 1
    assert bot.notified[0][0] == ["MAIN1"]  # 敏感通知发主管理员
    assert "永久保留" in bot.notified[0][1]

    # 已提醒未恢复：不重复
    asyncio.run(m._check_db_size(0.001))
    assert len(bot.notified) == 1

    db2 = None
    asyncio.run(db.close())


def test_check_db_size_below_threshold(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "real.db")
    db = DatabaseManager(settings.database)
    asyncio.run(db.initialize())
    svc = DatabaseService(db)

    bot = NotifyBot()
    m = _make_maintainer(svc, bot, admins=["A"])
    # 阈值1GB：不触发
    asyncio.run(m._check_db_size(1024))
    assert bot.notified == []
    asyncio.run(db.close())


def test_check_db_size_no_admins_logs_only(tmp_path):
    settings = Settings()
    settings.database.sqlite_path = str(tmp_path / "real.db")
    db = DatabaseManager(settings.database)
    asyncio.run(db.initialize())
    svc = DatabaseService(db)

    bot = NotifyBot()
    m = _make_maintainer(svc, bot, admins=[])
    asyncio.run(m._check_db_size(0.001))  # 无管理员配置：不崩溃
    assert bot.notified == []
    asyncio.run(db.close())
