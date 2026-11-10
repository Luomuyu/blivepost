# -*- coding: utf-8 -*-
"""
Web状态面板公共模块（webpanel.py与各webpanel_*混入共享的模块级对象与纯函数）

包含：日志器、Jinja2模板渲染环境、会话/口令常量、全局推送开关注册表、
口令哈希工具与日期解析/月度聚合纯函数。独立成模块以避免webpanel.py与
各混入文件之间的循环导入。
"""

import hashlib
import secrets

from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from ..utils.logger import get_logger


logger = get_logger("webpanel")

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=True,
)

_SESSION_COOKIE = "blivepost_sid"
_SESSION_TTL = timedelta(days=7)
_PBKDF2_ITERATIONS = 120_000

# 全局推送开关注册表：name -> (显示名, config默认值字段)
SWITCH_DEFS = [
    ("anchor", "天选时刻"),
    ("guard_achievement", "大航海成就"),
    ("system_event", "系统事件"),
    ("room_change", "房间变更"),
    ("dynamic_monitor", "动态监控"),
    ("daily_report", "每日结算"),
    ("weekly_report", "每周结算"),
    ("monthly_report", "每月结算"),
]


def hash_password(password: str) -> str:
    """pbkdf2口令哈希（随机盐，格式 pbkdf2$iterations$salt$hash）"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令哈希（格式不合法/不匹配返回False）"""
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                   bytes.fromhex(salt), int(iterations))
        return secrets.compare_digest(calc.hex(), digest)
    except (ValueError, TypeError):
        return False


def _add_days(d: date, days: int) -> date:
    from datetime import timedelta
    return d + timedelta(days=days)


def _parse_month(text: str):
    """解析YYYY-MM，返回(y, m)；非法返回None"""
    try:
        parts = text.strip().split("-")
        if len(parts) != 2:
            return None
        y, m = int(parts[0]), int(parts[1])
        if not (1 <= m <= 12) or not (2000 <= y <= 2100):
            return None
        return y, m
    except (ValueError, AttributeError):
        return None


def _aggregate_monthly(daily: list) -> list:
    """按日行汇总为自然月行（day为YYYY-MM），DB无关的纯Python聚合"""
    months: dict = {}
    for row in daily:
        key = row["day"][:7]
        m = months.setdefault(key, {"day": key, "danmaku": 0, "gift_gold": 0.0,
                                    "ship_gold": 0.0, "sc_total": 0})
        m["danmaku"] += row.get("danmaku", 0) or 0
        m["gift_gold"] += row.get("gift_gold", 0) or 0
        m["ship_gold"] += row.get("ship_gold", 0) or 0
        m["sc_total"] += row.get("sc_total", 0) or 0
    for m in months.values():
        m["gift_gold"] = round(m["gift_gold"], 1)
        m["ship_gold"] = round(m["ship_gold"], 1)
    return [months[k] for k in sorted(months)]


def _parse_day(text: str):
    """YYYY-MM-DD→date（非法返回None）"""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
