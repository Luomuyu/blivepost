# -*- coding: utf-8 -*-
"""
数据库存储服务 - 统计汇总查询（StatsMixin）
"""

import asyncio

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from .models import (
    DanmakuRecord, GiftRecord, SuperChatRecord, GuardRecord,
    AnchorRecord, RedPocketRecord, GuardAchievementRecord,
    SystemEventRecord, RoomChangeRecord, LiveStatusRecord, local_now,
    GroupBindingRecord, CookieAccountRecord, DynamicBindingRecord,
    RoomDbRecord
)
from ..utils.logger import get_logger

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager
    from .manager import DatabaseManager


logger = get_logger("db_service")


class StatsMixin:
    """统计汇总查询混入（每日/区间汇总、历史趋势、表行数与词云聚合；组合进DatabaseService）"""

    if TYPE_CHECKING:
        # 组合后由DatabaseService或其他混入提供的能力（前向声明，供类型检查）
        db_manager: "DatabaseManager"
        _event_session: Callable[[Any], "AbstractAsyncContextManager[Any]"]
        _room_manager: Callable[[int], Awaitable["DatabaseManager"]]
        _all_room_managers: Callable[[], Awaitable[list]]

    @staticmethod
    def _zero_summary(day_start: datetime) -> dict:
        """全零汇总（空房间列表等场景，不触库）"""
        return {
            "day": day_start.strftime("%Y-%m-%d"),
            "danmaku_count": 0, "gift_gold": 0.0, "gift_count": 0,
            "ship_total": 0.0, "ship_count": 0, "sc_total": 0,
            "sc_count": 0, "red_pocket": 0,
        }

    # ========== 统计查询 ==========

    async def get_daily_summary(self, room_id, day: Optional[datetime] = None,
                                _window=None) -> dict:
        """
        查询指定房间某日（默认今天）的收入与弹幕汇总

        各表聚合以标量子查询组合为单条SQL执行，避免跨表串行往返。

        Args:
            room_id: 直播间ID
            day: 日期（取该日0点到24点），默认今天

        Returns:
            dict: {
                day, danmaku_count, gift_gold(元), gift_count,
                ship_total(元), ship_count, sc_total(元), sc_count, red_pocket(电池)
            }
        """
        # 兼容多房间列表传参：拆分单房子库查询后合并
        if isinstance(room_id, (list, tuple)):
            rooms = list(room_id)
            if not rooms:
                day = day or local_now()
                return self._zero_summary(
                    day.replace(hour=0, minute=0, second=0, microsecond=0))
            summaries = [await self.get_daily_summary(r, day=day, _window=_window)
                         for r in rooms]
            merged = summaries[0]
            for other in summaries[1:]:
                for k, v in other.items():
                    if k == "day":
                        continue
                    # 收入类保留两位到分位（避免浮点尾差如4.499999999999999）
                    merged[k] = round(merged[k] + v, 2) if isinstance(merged[k], float) \
                        else merged[k] + v
            return merged

        if _window is not None:
            day_start, day_end = _window
        else:
            day = day or local_now()
            day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
            day_end = day_start + timedelta(days=1)

        summary = {
            "day": day_start.strftime("%Y-%m-%d"),
            "danmaku_count": 0,
            "gift_gold": 0.0,
            "gift_count": 0,
            "ship_total": 0.0,
            "ship_count": 0,
            "sc_total": 0,
            "sc_count": 0,
            "red_pocket": 0,
        }

        def _scalar(model, aggregate, *filters):
            """单列标量子查询（每表每个聚合一个，组合进同一条SQL）"""
            room_filter = (model.room_id.in_(room_id) if isinstance(room_id, (list, tuple))
                           else model.room_id == room_id)
            return select(aggregate).select_from(model).where(
                room_filter,
                model.timestamp >= day_start,
                model.timestamp < day_end,
                *filters,
            ).scalar_subquery()

        try:
            async with self._event_session(room_id) as session:
                res = await session.execute(
                    select(
                        _scalar(DanmakuRecord, func.count()).label("danmaku_count"),
                        # 礼物（金瓜子，瓜子→元）
                        _scalar(GiftRecord, func.coalesce(func.sum(GiftRecord.total_price), 0),
                                GiftRecord.coin_type == "gold").label("gift_gold"),
                        _scalar(GiftRecord, func.count(),
                                GiftRecord.coin_type == "gold").label("gift_count"),
                        # 上舰（瓜子→元）
                        _scalar(GuardRecord, func.coalesce(
                            func.sum(GuardRecord.price * GuardRecord.num), 0)
                        ).label("ship_gold"),
                        _scalar(GuardRecord, func.count()).label("ship_count"),
                        # SC（元）
                        _scalar(SuperChatRecord, func.coalesce(
                            func.sum(SuperChatRecord.price), 0)).label("sc_total"),
                        _scalar(SuperChatRecord, func.count()).label("sc_count"),
                        # 红包（电池）
                        _scalar(RedPocketRecord, func.coalesce(
                            func.sum(RedPocketRecord.total_price), 0)).label("red_pocket"),
                    )
                )
                row = res.one()

                summary["danmaku_count"] = row.danmaku_count or 0
                # 收入类保留两位到分位（瓜子÷1000得元，统一round(...,2)防浮点尾差）
                summary["gift_gold"] = round(int(row.gift_gold) / 1000, 2)
                summary["gift_count"] = row.gift_count
                summary["ship_total"] = round(int(row.ship_gold) / 1000, 2)
                summary["ship_count"] = row.ship_count
                summary["sc_total"] = int(row.sc_total)
                summary["sc_count"] = row.sc_count
                summary["red_pocket"] = int(row.red_pocket or 0)
        except Exception as e:
            logger.error(f"查询每日汇总失败: {e}")

        return summary

    async def get_daily_summaries(self, room_ids: List[int],
                                  day: Optional[datetime] = None) -> Dict[int, dict]:
        """
        批量查询多房间某日（默认今天）汇总（Web面板数据收集用）

        房间子库天然分库，无法单SQL跨房聚合：方法内对各房间的
        get_daily_summary并行扇出（asyncio.gather）。单房间失败由
        get_daily_summary自身容错返回零值汇总，不影响其余房间；
        现有get_daily_summary语义保持不变。

        Args:
            room_ids: 直播间ID列表
            day: 日期（取该日0点到24点），默认今天

        Returns:
            Dict[int, dict]: room_id -> 汇总dict（结构同get_daily_summary）
        """
        ids = list(room_ids)
        if not ids:
            return {}
        results = await asyncio.gather(
            *(self.get_daily_summary(r, day=day) for r in ids),
            return_exceptions=True)
        summaries: Dict[int, dict] = {}
        zero_day: Optional[datetime] = None
        for rid, res in zip(ids, results):
            if isinstance(res, BaseException):
                logger.error(f"批量查询房间{rid}每日汇总失败: {res}")
                if zero_day is None:
                    d = day or local_now()
                    zero_day = d.replace(hour=0, minute=0, second=0, microsecond=0)
                summaries[rid] = self._zero_summary(zero_day)
            else:
                summaries[rid] = res
        return summaries

    async def get_summary_range(self, room_id: int, start: datetime, end: datetime) -> dict:
        """
        查询指定房间时间段 [start, end) 的收入与弹幕汇总（周报/月报用）

        Args:
            room_id: 直播间ID
            start: 起始时间（含）
            end: 结束时间（不含）

        Returns:
            dict: 结构同get_daily_summary，day字段为时间范围文本
        """
        summary = await self.get_daily_summary(room_id, _window=(start, end))
        summary["day"] = f"{start.strftime('%Y-%m-%d')}~{end.strftime('%Y-%m-%d')}"
        return summary

    async def get_top_super_chat(
        self,
        room_id: int,
        day: Optional[datetime] = None,
        limit: int = 5
    ) -> List[Tuple[str, int, str]]:
        """
        查询指定房间某日（默认今天）SC金额排行

        Args:
            room_id: 直播间ID
            day: 日期，默认今天
            limit: 条数

        Returns:
            list[(uname, price, message)]，按金额降序
        """
        day = day or local_now()
        day_start = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        try:
            async with self._event_session(room_id) as session:
                res = await session.execute(
                    select(SuperChatRecord.uname, SuperChatRecord.price,
                           SuperChatRecord.message)
                    .where(
                        SuperChatRecord.room_id == room_id,
                        SuperChatRecord.timestamp >= day_start,
                        SuperChatRecord.timestamp < day_end,
                    )
                    .order_by(SuperChatRecord.price.desc())
                    .limit(limit)
                )
                return [(r.uname, r.price, r.message) for r in res.all()]
        except Exception as e:
            logger.error(f"查询SC排行失败: {e}")
            return []

    async def get_recent_live_status(self, limit: int = 10) -> list:
        """
        查询最近的下播记录（Web面板用）

        Args:
            limit: 条数

        Returns:
            list[dict]: 下播记录列表
        """
        try:
            merged: List[dict] = []
            for manager in await self._all_room_managers():
                async with manager.get_session() as session:
                    result = await session.execute(
                        select(LiveStatusRecord).where(
                            LiveStatusRecord.status == "live_end"
                        ).order_by(LiveStatusRecord.timestamp.desc()).limit(limit)
                    )
                    merged.extend(
                        {
                            "room_id": rec.room_id,
                            "title": rec.title,
                            "danmaku_count": rec.danmaku_count,
                            "gold_count": rec.gold_count,
                            "ship_count": rec.ship_count,
                            "sc_count": rec.sc_count,
                            "duration": rec.duration,
                            "end_time": rec.end_time,
                        }
                        for rec in result.scalars().all()
                    )
            merged.sort(key=lambda d: d["end_time"] or datetime.min, reverse=True)
            # end_time格式化为字符串：裸datetime经json_response序列化会500
            for row in merged:
                if row["end_time"] is not None:
                    row["end_time"] = row["end_time"].strftime("%Y-%m-%d %H:%M")
            return merged[:limit]
        except Exception as e:
            logger.error(f"查询下播记录失败: {e}")
            return []

    async def get_daily_history(self, room_ids: Optional[List[int]] = None,
                                days: int = 30) -> List[dict]:
        """
        按日聚合的历史趋势数据（Web面板折线图用）

        Args:
            room_ids: 房间列表（None=全部房间）
            days: 统计最近N天

        Returns:
            list[dict]: [{"day": "YYYY-MM-DD", "danmaku": n, "gift_gold": f,
                          "ship_gold": f, "sc_total": n}] 按日期升序
        """
        today = local_now().replace(hour=0, minute=0, second=0, microsecond=0)
        return await self.get_daily_history_range(
            (today - timedelta(days=days - 1)).date(), today.date(),
            room_ids=room_ids)

    async def get_daily_history_range(self, start_day, end_day,
                                      room_ids: Optional[List[int]] = None) -> List[dict]:
        """
        按日聚合的自然日区间历史数据（Web面板趋势选择器用）

        Args:
            start_day/end_day: 起止日期（date/datetime，含首尾）
            room_ids: 房间列表（None=全部房间）

        Returns:
            list[dict]: 同get_daily_history，按日期升序；区间内无数据补零
        """
        from datetime import datetime as _dt, date as _date, timedelta as _td
        if isinstance(start_day, _date) and not isinstance(start_day, _dt):
            start_day = _dt.combine(start_day, _dt.min.time())
        if isinstance(end_day, _date) and not isinstance(end_day, _dt):
            end_day = _dt.combine(end_day, _dt.min.time())
        start = start_day
        span_days = (end_day - start_day).days + 1
        if span_days <= 0:
            return []
        end_bound = start + _td(days=span_days)

        if room_ids:
            managers = [await self._room_manager(r) for r in room_ids]
        else:
            managers = await self._all_room_managers()

        async def _by_day(manager, model, aggregate, coin_filter=None):
            conditions = [model.timestamp >= start, model.timestamp < end_bound]
            if coin_filter is not None:
                conditions.append(coin_filter)
            async with manager.get_session() as session:
                res = await session.execute(
                    select(
                        func.date(model.timestamp).label("day"),
                        aggregate,
                    ).select_from(model).where(*conditions).group_by("day")
                )
                return {str(r.day): int(r[1] or 0) for r in res.all()}

        history: Dict[str, dict] = {
            (start + _td(days=i)).strftime("%Y-%m-%d"):
                {"day": (start + _td(days=i)).strftime("%Y-%m-%d"),
                 "danmaku": 0, "gift_gold": 0.0, "ship_gold": 0.0, "sc_total": 0}
            for i in range(span_days)
        }
        try:
            for manager in managers:
                danmaku = await _by_day(manager, DanmakuRecord, func.count())
                gift = await _by_day(manager, GiftRecord,
                                     func.sum(GiftRecord.total_price),
                                     GiftRecord.coin_type == "gold")
                ship = await _by_day(manager, GuardRecord,
                                     func.sum(GuardRecord.price * GuardRecord.num))
                sc = await _by_day(manager, SuperChatRecord, func.sum(SuperChatRecord.price))
                for day, row in history.items():
                    row["danmaku"] += danmaku.get(day, 0)
                    # 收入类保留两位到分位（防浮点尾差）
                    row["gift_gold"] = round(row["gift_gold"] + gift.get(day, 0) / 1000, 2)
                    row["ship_gold"] = round(row["ship_gold"] + ship.get(day, 0) / 1000, 2)
                    row["sc_total"] += sc.get(day, 0)
        except Exception as e:
            logger.error(f"查询历史趋势失败: {e}")
            return []

        return [history[day] for day in sorted(history)]

    async def get_table_counts(self) -> List[Tuple[str, int]]:
        """
        统计各数据表行数（数据治理用）

        Returns:
            list[(表名, 行数)]，按行数降序
        """
        main_tables: List[Tuple[str, Any]] = [
            ("群绑定", GroupBindingRecord), ("Cookie账号", CookieAccountRecord),
            ("动态订阅", DynamicBindingRecord),
        ]
        event_tables: List[Tuple[str, Any]] = [
            ("弹幕", DanmakuRecord), ("礼物", GiftRecord),
            ("SC", SuperChatRecord), ("舰长", GuardRecord),
            ("天选", AnchorRecord), ("红包", RedPocketRecord),
            ("大航海成就", GuardAchievementRecord), ("系统事件", SystemEventRecord),
            ("房间变更", RoomChangeRecord), ("开播下播", LiveStatusRecord),
        ]
        counts: Dict[str, int] = {}
        try:
            async with self.db_manager.get_session() as session:
                for name, model in main_tables:
                    res = await session.execute(select(func.count()).select_from(model))
                    counts[name] = int(res.scalar() or 0)
            # 事件表行数跨全部房间子库合计
            for manager in await self._all_room_managers():
                async with manager.get_session() as session:
                    for name, model in event_tables:
                        res = await session.execute(select(func.count()).select_from(model))
                        counts[name] = counts.get(name, 0) + int(res.scalar() or 0)
        except Exception as e:
            logger.error(f"统计表行数失败: {e}")
        result: List[Tuple[str, int]] = list(counts.items())
        return sorted(result, key=lambda x: x[1], reverse=True)

    async def list_room_ids(self) -> List[int]:
        """全部已知的直播间ID（子库映射登记过的，升序；趋势房间选择用）"""
        try:
            async with self.db_manager.get_session() as session:
                res = await session.execute(select(RoomDbRecord.room_id))
                return sorted(int(r[0]) for r in res.all())
        except Exception as e:
            logger.error(f"查询房间列表失败: {e}")
            return []

    # ========== 弹幕词云 ==========

    async def get_danmaku_texts(self, room_ids=None, start_day=None,
                                end_day=None, limit: int = 200_000) -> List[str]:
        """词云用弹幕文本聚合（房间子库扇出；按时间范围取msg，限行保护）

        Args:
            room_ids: 指定房间列表（None=全部已知房间）
            start_day: 起始自然日（默认今天）
            end_day: 结束自然日（默认同start_day）
            limit: 最多取的弹幕行数（防止超大范围拖垮内存）
        """
        start_day = start_day or local_now()
        end_day = end_day or start_day
        start = datetime(start_day.year, start_day.month, start_day.day)
        end = datetime(end_day.year, end_day.month, end_day.day,
                       23, 59, 59)
        if room_ids is None:
            room_ids = await self.list_room_ids()
        texts: List[str] = []
        try:
            for rid in room_ids:
                if len(texts) >= limit:
                    break
                async with self._event_session(int(rid)) as session:
                    res = await session.execute(
                        select(DanmakuRecord.msg).where(
                            DanmakuRecord.timestamp >= start,
                            DanmakuRecord.timestamp <= end)
                        .limit(max(limit - len(texts), 0)))
                    texts.extend(r[0] or "" for r in res.all())
        except Exception as e:
            logger.error(f"词云弹幕聚合失败: {e}")
        return texts[:limit]
