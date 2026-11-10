# -*- coding: utf-8 -*-
"""
时间戳工具（第十六批：全库13位毫秒时间戳）

统一入口：存储层一律用 13位毫秒Unix时间戳（BIGINT，与时区无关），
展示层经 Setting.timezone 配置的时区转换。

核心函数：
  now_ms()               当前毫秒时间戳
  dt_to_ms(dt)           datetime → 毫秒时间戳
  ms_to_dt(ts, tz)       毫秒时间戳 → 指定时区的datetime
  ms_to_str(ts, fmt, tz) 毫秒时间戳 → 显示字符串
  day_bounds_ms(day, tz) 某日的 [start_ms, end_ms) 半开区间
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

# 默认时区（可被Setting.timezone覆盖）
_default_tz = ZoneInfo("Asia/Shanghai")


def set_default_timezone(name: str) -> None:
    """设置全局默认时区（Settings加载后调用）"""
    global _default_tz
    try:
        _default_tz = ZoneInfo(name)
    except Exception:
        from ..utils.logger import get_logger
        get_logger("timeutil").warning(f"无效时区名 {name}，保持 {_default_tz}")


def get_default_timezone() -> ZoneInfo:
    return _default_tz


def now_ms() -> int:
    """当前13位毫秒Unix时间戳"""
    return int(datetime.now(dt_timezone.utc).timestamp() * 1000)


def dt_to_ms(dt: datetime) -> int:
    """datetime → 毫秒时间戳（naive按默认时区解释）"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_default_tz)
    return int(dt.timestamp() * 1000)


def ms_to_dt(ts: int, tz: Optional[ZoneInfo] = None) -> datetime:
    """毫秒时间戳 → 指定时区的datetime（默认全局时区）"""
    tz = tz or _default_tz
    return datetime.fromtimestamp(ts / 1000, tz=tz)


def ms_to_str(ts: int, fmt: str = "%Y-%m-%d %H:%M", tz: Optional[ZoneInfo] = None) -> str:
    """毫秒时间戳 → 显示字符串"""
    if not ts:
        return ""
    return ms_to_dt(ts, tz).strftime(fmt)


def day_bounds_ms(day: Optional[datetime] = None,
                  tz: Optional[ZoneInfo] = None) -> Tuple[int, int]:
    """某日的 [start_ms, end_ms) 半开区间（消除23:59:59漏秒问题）

    Args:
        day: 日期（naive按默认时区；None=今天）
        tz: 时区（默认全局）

    Returns:
        (day_start_ms, next_day_start_ms)  左闭右开
    """
    tz = tz or _default_tz
    if day is None:
        day = datetime.now(tz)
    if day.tzinfo is None:
        day = day.replace(tzinfo=tz)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000), int((start + timedelta(days=1)).timestamp() * 1000)


def range_bounds_ms(start: datetime, end: datetime) -> Tuple[int, int]:
    """时间段的 [start_ms, end_ms) 半开区间"""
    return dt_to_ms(start), dt_to_ms(end)


def day_key_ms(ts: int, tz: Optional[ZoneInfo] = None) -> str:
    """毫秒时间戳 → "YYYY-MM-DD" 日期键（分组聚合用）"""
    return ms_to_str(ts, "%Y-%m-%d", tz)


def hour_key_ms(ts: int, tz: Optional[ZoneInfo] = None) -> int:
    """毫秒时间戳 → 小时（0-23，热力图用）"""
    return ms_to_dt(ts, tz).hour
