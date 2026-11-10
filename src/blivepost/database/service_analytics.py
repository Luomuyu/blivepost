# -*- coding: utf-8 -*-
"""数据库存储服务 - 观众分析（AnalyticsMixin）

活跃时段热力（7天×24小时弹幕矩阵）、高频发言榜、弹幕量日分布。

第十六批起时间列为13位毫秒时间戳。按日/按小时分组经SQL侧整数分桶
（(timestamp+时区偏移ms)/86400000，房间子库恒为SQLite的整数除法，
分日语义与Python侧day_key_ms/hour_key_ms完全一致），只回传聚合行
——此前全区间行拉回内存逐行计数，30天热门房间的单次请求可达数十万行。
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Dict, List

from sqlalchemy import func, select

from ..utils.logger import get_logger
from ..utils.timeutil import (
    day_bounds_ms, get_default_timezone, ms_to_dt, ms_to_str, now_ms
)
from .models import DanmakuRecord

if TYPE_CHECKING:
    from .manager import DatabaseManager

logger = get_logger("db_analytics")

_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000


def _tz_off_ms() -> int:
    """Setting.timezone的UTC偏移毫秒（分桶口径，与day_key_ms同时区源）

    注：取查询时刻的固定偏移——DST时区（America/*等）跨夏令时切换日的
    历史数据会与Python侧fromtimestamp的逐条历史偏移差1小时桶；默认
    Asia/Shanghai无夏令时，不受影响。
    """
    off = datetime.now(get_default_timezone()).utcoffset()
    return int(off.total_seconds() * 1000) if off else 0


def _bucket_day_key(bucket: int, off: int) -> str:
    """日桶序号 → YYYY-MM-DD（与day_key_ms互为逆运算）"""
    return ms_to_str(bucket * _DAY_MS - off, "%Y-%m-%d")


class AnalyticsMixin:
    """观众分析查询"""

    db_manager: "DatabaseManager"
    # 由DatabaseService组合类提供（mypy经Callable绕行）
    from typing import Any as _Any
    _room_db_name: _Any
    _ensure_registry: _Any
    _all_room_managers: _Any

    async def get_activity_heatmap(self, room_id: int = 0,
                                   days: int = 7) -> dict:
        """弹幕活跃热力图：{day: [24个时段的弹幕数]}，day为YYYY-MM-DD

        Args:
            room_id: 房间ID（0=全部房间）
            days: 天数（默认7，含今天）
        """
        start_ms = day_bounds_ms(
            ms_to_dt(now_ms()) - timedelta(days=days - 1))[0]
        off = _tz_off_ms()
        ts = DanmakuRecord.timestamp
        # SQLite整数除法（值恒正，截断即向下取整）＝day_key_ms的SQL等价
        day_b = (ts + off) / _DAY_MS
        hour_b = ((ts + off) / _HOUR_MS) % 24

        def _empty() -> Dict[str, list]:
            base = ms_to_dt(start_ms)
            return {(base + timedelta(days=i)).strftime("%Y-%m-%d"): [0] * 24
                    for i in range(days)}

        result = _empty()
        try:
            if room_id:
                managers = [await self._ensure_registry().get(
                    await self._room_db_name(room_id))]
            else:
                managers = await self._all_room_managers()

            for manager in managers:
                async with manager.get_session() as session:
                    rows = await session.execute(
                        select(day_b, hour_b, func.count()).where(
                            ts >= start_ms,
                        ).group_by(day_b, hour_b))
                    for db_, hb, cnt in rows.all():
                        day = _bucket_day_key(int(db_), off)
                        if day in result:
                            result[day][int(hb)] += int(cnt)
        except Exception as e:
            logger.error(f"查询活跃热力图失败: {e}")
        return result

    async def get_top_speakers(self, room_id: int = 0,
                               days: int = 7, limit: int = 20) -> List[dict]:
        """高频发言榜：[{uid, uname, count}]，按弹幕数降序"""
        start_ms = day_bounds_ms(
            ms_to_dt(now_ms()) - timedelta(days=days - 1))[0]
        agg: dict = {}
        try:
            if room_id:
                managers = [await self._ensure_registry().get(
                    await self._room_db_name(room_id))]
            else:
                managers = await self._all_room_managers()

            for manager in managers:
                async with manager.get_session() as session:
                    rows = await session.execute(
                        select(
                            DanmakuRecord.uid,
                            DanmakuRecord.uname,
                            func.count(),
                        ).where(
                            DanmakuRecord.timestamp >= start_ms,
                        ).group_by(
                            DanmakuRecord.uid, DanmakuRecord.uname,
                        ).order_by(func.count().desc()).limit(limit * 3))
                    for uid, uname, count in rows.all():
                        key = int(uid)
                        if key not in agg:
                            agg[key] = {"uid": int(uid), "uname": uname or "",
                                        "count": 0}
                        agg[key]["count"] += count
        except Exception as e:
            logger.error(f"查询高频发言榜失败: {e}")
        return sorted(agg.values(), key=lambda x: -x["count"])[:limit]

    async def get_danmaku_daily_distribution(self, room_id: int = 0,
                                             days: int = 30) -> List[dict]:
        """弹幕量日分布：[{day, count}]，按日升序（仅含有数据的日期）"""
        start_ms = day_bounds_ms(
            ms_to_dt(now_ms()) - timedelta(days=days - 1))[0]
        off = _tz_off_ms()
        ts = DanmakuRecord.timestamp
        day_b = (ts + off) / _DAY_MS
        agg: Dict[str, int] = {}
        try:
            if room_id:
                managers = [await self._ensure_registry().get(
                    await self._room_db_name(room_id))]
            else:
                managers = await self._all_room_managers()

            for manager in managers:
                async with manager.get_session() as session:
                    rows = await session.execute(
                        select(day_b, func.count()).where(
                            ts >= start_ms,
                        ).group_by(day_b))
                    for db_, cnt in rows.all():
                        day = _bucket_day_key(int(db_), off)
                        agg[day] = agg.get(day, 0) + int(cnt)
        except Exception as e:
            logger.error(f"查询弹幕日分布失败: {e}")
        return [{"day": d, "count": agg[d]} for d in sorted(agg)]
