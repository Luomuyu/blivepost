# -*- coding: utf-8 -*-
"""数据库存储服务 - 观众分析（AnalyticsMixin）

活跃时段热力（7天×24小时弹幕矩阵）、高频发言榜、弹幕量日分布。
"""

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, List

from sqlalchemy import func, select

from ..utils.logger import get_logger
from .models import DanmakuRecord

if TYPE_CHECKING:
    from .manager import DatabaseManager

logger = get_logger("db_analytics")


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
            days: 天数（默认7）
        """
        now = datetime.now()
        start = (now - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        result: dict = {}

        def _empty():
            return {d.strftime("%Y-%m-%d"): [0] * 24
                    for d in (start + timedelta(days=i) for i in range(days))}

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
                        select(
                            func.strftime("%Y-%m-%d", DanmakuRecord.timestamp),
                            func.strftime("%H", DanmakuRecord.timestamp),
                            func.count(),
                        ).where(
                            DanmakuRecord.timestamp >= start,
                        ).group_by(
                            func.strftime("%Y-%m-%d", DanmakuRecord.timestamp),
                            func.strftime("%H", DanmakuRecord.timestamp),
                        ))
                    for day, hour, count in rows:
                        if day in result and 0 <= int(hour) < 24:
                            result[day][int(hour)] += count
        except Exception as e:
            logger.error(f"查询活跃热力图失败: {e}")
        return result

    async def get_top_speakers(self, room_id: int = 0,
                               days: int = 7, limit: int = 20) -> List[dict]:
        """高频发言榜：[{uid, uname, count}]，按弹幕数降序"""
        now = datetime.now()
        start = (now - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
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
                            DanmakuRecord.timestamp >= start,
                        ).group_by(
                            DanmakuRecord.uid, DanmakuRecord.uname,
                        ).order_by(func.count().desc()).limit(limit * 3))
                    for uid, uname, count in rows:
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
        """弹幕量日分布：[{day, count}]，按日升序"""
        now = datetime.now()
        start = (now - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0)
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
                            func.strftime("%Y-%m-%d", DanmakuRecord.timestamp),
                            func.count(),
                        ).where(
                            DanmakuRecord.timestamp >= start,
                        ).group_by(
                            func.strftime("%Y-%m-%d", DanmakuRecord.timestamp)))
                    for day, count in rows:
                        agg[day] = agg.get(day, 0) + count
        except Exception as e:
            logger.error(f"查询弹幕日分布失败: {e}")
        return [{"day": d, "count": agg[d]} for d in sorted(agg)]
