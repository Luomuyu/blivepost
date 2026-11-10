# -*- coding: utf-8 -*-
"""
BLivePost - B站直播监控与推送系统

一个功能完整的B站直播监控与消息推送系统，支持：
- 直播间弹幕、礼物、上舰、SC等消息监控
- B站/微博动态监控
- 多平台消息推送（QQ群、QQ频道）
- WBI签名与会话管理
- Redis缓存支持
"""

__version__ = "1.2.0"
__author__ = "Luomuyu"

# 核心模块
from .core.handler import LiveEventHandler
from .core.manager import MonitorManager, RoomProcess, PushQueueConsumer

# 配置模块
from .config.settings import Settings

# 服务模块
from .services.push import PushService
from .services.bilibili import BilibiliAPI
from .services.fingerprint import FingerprintService
from .services.qqbot import QQBotClient
from .services.wbi import WbiSigner, wbi_sign, derive_wbi_key
from .services.report import DailyReportJob
from .services.dynamics import DynamicManager, LiveStateWatcher
from .services.cookie_pool import CookiePool, CookieAccount
from .services.admin_commands import AdminCommandHandler
from .services.bili_login import BiliLoginService, verify_nav
from .services.bindings import BindingStore, Binding
from .services.commands import CommandHandler

# 模型模块
from .models.live import (
    LiveRoomInfo,
    LiveStats,
    TempData,
    UserInfo,
    DanmakuInfo,
    GiftInfo,
    SuperChatInfo,
    GuardInfo,
)

# 工具模块
from .utils.logger import Logger, get_logger

# 运行入口
from .__main__ import main, run


__all__ = [
    # 核心类（LiveMonitor已于v1.2.0移除，监控由MonitorManager子进程承担）
    "LiveEventHandler",
    # 配置
    "Settings",
    # 服务
    "PushService",
    "BilibiliAPI",
    "FingerprintService",
    "DynamicManager",
    "LiveStateWatcher",
    "QQBotClient",
    "BindingStore",
    "Binding",
    "CommandHandler",
    "MonitorManager",
    "RoomProcess",
    "PushQueueConsumer",
    "DailyReportJob",
    "WbiSigner",
    "wbi_sign",
    "derive_wbi_key",
    "CookiePool",
    "CookieAccount",
    "AdminCommandHandler",
    "BiliLoginService",
    "verify_nav",
    # 模型
    "LiveRoomInfo",
    "LiveStats",
    "TempData",
    "UserInfo",
    "DanmakuInfo",
    "GiftInfo",
    "SuperChatInfo",
    "GuardInfo",
    # 工具
    "Logger",
    "get_logger",
    # 入口
    "main",
    "run",
]
