# -*- coding: utf-8 -*-
"""
数据库存储服务 - 统计汇总查询（StatsMixin）

第十六批起时间列统一存储13位毫秒Unix时间戳：日期范围查询一律使用
day_bounds_ms/range_bounds_ms生成的 [start_ms, end_ms) 半开区间（消除
23:59:59漏秒），按日/按小时分组在Python侧用day_key_ms/hour_key_ms完成
（SQL侧strftime仅对SQLite的text时间有效且无时区概念，已全部移除）。
"""

import asyncio

from datetime import date as _date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, select

from .models import (
    DanmakuRecord, GiftRecord, SuperChatRecord, GuardRecord,
    AnchorRecord, RedPocketRecord, GuardAchievementRecord,
    SystemEventRecord, RoomChangeRecord, LiveStatusRecord,
    GroupBindingRecord, CookieAccountRecord, DynamicBindingRecord,
    RoomDbRecord
)
from ..utils.logger import get_logger
from ..utils.timeutil import (
    day_bounds_ms, day_key_ms, get_default_timezone, ms_to_dt, ms_to_str,
    now_ms, range_bounds_ms
)


def _tz_off_ms() -> int:
    """Setting.timezone的UTC偏移毫秒（SQL分桶口径，与day_key_ms同时区源）

    注：固定偏移近似——DST时区跨夏令时切换日有1小时误差（默认
    Asia/Shanghai无夏令时，不受影响）。
    """
    from datetime import datetime as _dt
    off = _dt.now(get_default_timezone()).utcoffset()
    return int(off.total_seconds() * 1000) if off else 0

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
    def _zero_summary(day_start_ms: int) -> dict:
        """全零汇总（空房间列表等场景，不触库）"""
        return {
            "day": day_key_ms(day_start_ms),
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
        时间列为毫秒时间戳：按 [start_ms, end_ms) 半开区间过滤。

        Args:
            room_id: 直播间ID
            day: 日期（取该日0点到24点），默认今天
            _window: 显式时间窗（start/end，datetime或毫秒int；内部统一毫秒化）

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
                return self._zero_summary(day_bounds_ms(day)[0])
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
            win_start, win_end = _window
            if isinstance(win_start, datetime) or isinstance(win_start, _date):
                start_ms, end_ms = range_bounds_ms(
                    _as_datetime(win_start), _as_datetime(win_end))
            else:
                start_ms, end_ms = int(win_start), int(win_end)
        else:
            start_ms, end_ms = day_bounds_ms(day)

        summary = {
            "day": day_key_ms(start_ms),
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
                model.timestamp >= start_ms,
                model.timestamp < end_ms,
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
        fallback_ms = 0
        for rid, res in zip(ids, results):
            if isinstance(res, BaseException):
                logger.error(f"批量查询房间{rid}每日汇总失败: {res}")
                if not fallback_ms:
                    fallback_ms = day_bounds_ms(day)[0]
                summaries[rid] = self._zero_summary(fallback_ms)
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
        start_ms, end_ms = range_bounds_ms(start, end)
        summary = await self.get_daily_summary(room_id, _window=(start_ms, end_ms))
        summary["day"] = (
            f"{ms_to_str(start_ms, '%Y-%m-%d')}~{ms_to_str(end_ms, '%Y-%m-%d')}")
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
        start_ms, end_ms = day_bounds_ms(day)

        try:
            async with self._event_session(room_id) as session:
                res = await session.execute(
                    select(SuperChatRecord.uname, SuperChatRecord.price,
                           SuperChatRecord.message)
                    .where(
                        SuperChatRecord.room_id == room_id,
                        SuperChatRecord.timestamp >= start_ms,
                        SuperChatRecord.timestamp < end_ms,
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

        end_time为13位毫秒时间戳（int经json_response序列化安全；
        展示层经webpanel的ms2str filter或ms_to_str格式化）。
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
            merged.sort(key=lambda d: d["end_time"] or 0, reverse=True)
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
        today_start_ms = day_bounds_ms()[0]
        start_dt = ms_to_dt(today_start_ms) - timedelta(days=days - 1)
        return await self.get_daily_history_range(
            start_dt.date(), ms_to_dt(today_start_ms).date(), room_ids=room_ids)

    async def get_daily_history_range(self, start_day, end_day,
                                      room_ids: Optional[List[int]] = None) -> List[dict]:
        """
        按日聚合的自然日区间历史数据（Web面板趋势选择器用）

        按日分组经SQL侧整数分桶（(timestamp+时区偏移ms)/86400000，房间
        子库恒为SQLite的整数除法，分日语义与Python侧day_key_ms一致），
        只回传聚合行——此前全区间行拉回内存逐行计数。

        Args:
            start_day/end_day: 起止日期（date/datetime，含首尾）
            room_ids: 房间列表（None=全部房间）

        Returns:
            list[dict]: 同get_daily_history，按日期升序；区间内无数据补零
        """
        start_day = _as_datetime(start_day)
        end_day = _as_datetime(end_day)
        span_days = (end_day.date() - start_day.date()).days + 1
        if span_days <= 0:
            return []
        start_ms = day_bounds_ms(start_day)[0]
        end_ms = day_bounds_ms(end_day)[1]  # 末日次日0点（半开区间右端）
        tz_off = _tz_off_ms()
        _DAY_MS = 86_400_000

        def _day_bucket(col):
            return (col + tz_off) / _DAY_MS

        if room_ids:
            managers = [await self._room_manager(r) for r in room_ids]
        else:
            managers = await self._all_room_managers()

        history: Dict[str, dict] = {}
        start_dt_local = ms_to_dt(start_ms)
        for i in range(span_days):
            key = (start_dt_local + timedelta(days=i)).strftime("%Y-%m-%d")
            history[key] = {"day": key, "danmaku": 0, "gift_gold": 0.0,
                            "ship_gold": 0.0, "sc_total": 0}

        def _bucket_day(bucket: int) -> Optional[str]:
            # 日桶序号→日期键（day_key_ms的逆运算）；越界返回None
            key = ms_to_str(bucket * _DAY_MS - tz_off, "%Y-%m-%d")
            return key if key in history else None

        try:
            for manager in managers:
                async with manager.get_session() as session:
                    # 弹幕计数（SQL侧分桶）
                    b = _day_bucket(DanmakuRecord.timestamp)
                    res = await session.execute(
                        select(b, func.count()).where(
                            DanmakuRecord.timestamp >= start_ms,
                            DanmakuRecord.timestamp < end_ms).group_by(b))
                    for db_, cnt in res.all():
                        day_key = _bucket_day(int(db_))
                        if day_key:
                            history[day_key]["danmaku"] += int(cnt)
                    # 礼物（金瓜子，瓜子→元；SUM下推）
                    b = _day_bucket(GiftRecord.timestamp)
                    res = await session.execute(
                        select(b, func.sum(GiftRecord.total_price)).where(
                            GiftRecord.coin_type == "gold",
                            GiftRecord.timestamp >= start_ms,
                            GiftRecord.timestamp < end_ms).group_by(b))
                    for db_, val in res.all():
                        day_key = _bucket_day(int(db_))
                        if day_key:
                            history[day_key]["gift_gold"] = round(
                                history[day_key]["gift_gold"] + int(val or 0) / 1000, 2)
                    # 上舰（瓜子→元）
                    b = _day_bucket(GuardRecord.timestamp)
                    res = await session.execute(
                        select(b, func.sum(GuardRecord.price * GuardRecord.num)).where(
                            GuardRecord.timestamp >= start_ms,
                            GuardRecord.timestamp < end_ms).group_by(b))
                    for db_, val in res.all():
                        day_key = _bucket_day(int(db_))
                        if day_key:
                            history[day_key]["ship_gold"] = round(
                                history[day_key]["ship_gold"] + int(val or 0) / 1000, 2)
                    # SC（元）
                    b = _day_bucket(SuperChatRecord.timestamp)
                    res = await session.execute(
                        select(b, func.sum(SuperChatRecord.price)).where(
                            SuperChatRecord.timestamp >= start_ms,
                            SuperChatRecord.timestamp < end_ms).group_by(b))
                    for db_, val in res.all():
                        day_key = _bucket_day(int(db_))
                        if day_key:
                            history[day_key]["sc_total"] += int(val or 0)
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

        时间过滤为 [start_ms, end_ms) 半开区间（末日全覆盖，无漏秒）。

        Args:
            room_ids: 指定房间列表（None=全部已知房间）
            start_day: 起始自然日（date/datetime，默认今天）
            end_day: 结束自然日（date/datetime，默认同start_day）
            limit: 最多取的弹幕行数（防止超大范围拖垮内存）
        """
        start_day = _as_datetime(start_day or ms_to_dt(now_ms()))
        end_day = _as_datetime(end_day or start_day)
        start_ms = day_bounds_ms(start_day)[0]
        end_ms = day_bounds_ms(end_day)[1]
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
                            DanmakuRecord.timestamp >= start_ms,
                            DanmakuRecord.timestamp < end_ms)
                        .limit(max(limit - len(texts), 0)))
                    texts.extend(r[0] or "" for r in res.all())
        except Exception as e:
            logger.error(f"词云弹幕聚合失败: {e}")
        return texts[:limit]


def _as_datetime(day) -> datetime:
    """date/datetime统一为naive datetime（毫秒化时按默认时区解释）"""
    if isinstance(day, _date) and not isinstance(day, datetime):
        return datetime(day.year, day.month, day.day)
    return day
