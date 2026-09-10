# -*- coding: utf-8 -*-
"""
每日结算推送

每天固定时间（默认00:05）向所有监控中的绑定群推送各房间昨日
收入汇总。注意：主动消息有平台频控（未放量约4条/月/群），
通过Setting.daily_report开关控制，默认关闭。
"""

import asyncio
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .push import PushService
    from ..database.service import DatabaseService


logger = get_logger("report")


class DailyReportJob:
    """每日结算推送任务"""

    def __init__(
        self,
        binding_store: "BindingStore",
        push_service: "PushService",
        db_service: "DatabaseService",
        hour: int = 0,
        minute: int = 5,
        daily: bool = True,
        weekly: bool = False,
        monthly: bool = False,
    ):
        """
        初始化每日结算任务

        Args:
            binding_store: 群绑定存储（确定推送哪些房间/群）
            push_service: 推送服务
            db_service: 数据库服务（汇总查询）
            hour: 推送时刻（时）
            minute: 推送时刻（分）
            daily: 启用每日结算
            weekly: 启用每周结算（周一推送上周汇总）
            monthly: 启用每月结算（每月1日推送上月汇总）
        """
        self.binding_store = binding_store
        self.push_service = push_service
        self.db_service = db_service
        self.hour = hour
        self.minute = minute
        self.daily_enabled = daily
        self.weekly_enabled = weekly
        self.monthly_enabled = monthly
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动每日结算循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="daily-report")
            logger.info(f"每日结算推送已启动（每天{self.hour:02d}:{self.minute:02d}）")

    async def stop(self) -> None:
        """停止任务"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        """主循环：等待到下一个推送时刻，执行日结（及周一周结/月初月结）"""
        while True:
            try:
                delay = self._seconds_until_next()
                await asyncio.sleep(delay)
                if self.daily_enabled:
                    await self.check_once()
                # 周结（周一）、月结（每月1日）紧随日结执行
                await self.check_periodic()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"结算推送失败: {e}")
                await asyncio.sleep(60)

    async def check_periodic(self) -> int:
        """按配置执行周结/月结（周一推上周、每月1日推上月；发送前查运行时开关）"""
        now = datetime.now()
        count = 0
        if self.weekly_enabled and now.weekday() == 0:
            if await self._switch_on("weekly_report"):
                start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0,
                                                          microsecond=0)
                count += await self._push_range("【每周结算】", start, now)
        if self.monthly_enabled and now.day == 1:
            if await self._switch_on("monthly_report"):
                start = (now - timedelta(days=now.day)).replace(day=1, hour=0, minute=0,
                                                                second=0, microsecond=0)
                count += await self._push_range("【每月结算】", start, now)
        return count

    async def _switch_on(self, name: str) -> bool:
        """结算开关运行时状态（DB优先，缺行/无DB回落config默认值）"""
        if name == "daily_report":
            default = self.daily_enabled
        elif name == "weekly_report":
            default = self.weekly_enabled
        else:
            default = self.monthly_enabled
        if self.db_service is None:
            return default
        try:
            return await self.db_service.get_push_switch_enabled(name, default)
        except Exception:
            return default

    async def _push_range(self, title: str, start: datetime, end: datetime) -> int:
        """推送指定时间段各监控房间汇总"""
        room_ids = self.binding_store.get_monitored_room_ids()
        count = 0
        for rid in room_ids:
            try:
                summary = await self.db_service.get_summary_range(rid, start, end)
                if not self._has_data(summary):
                    continue
                text = self._render(rid, summary, title=title)
                self.push_service.send_message(text, room_id=rid)
                count += 1
            except Exception as e:
                logger.error(f"房间 {rid} 周期结算推送失败: {e}")
        logger.info(f"{title.strip()}推送完成，共推送 {count} 个房间")
        return count

    def _seconds_until_next(self) -> float:
        """距下一个推送时刻的秒数"""
        now = datetime.now()
        target = now.replace(hour=self.hour, minute=self.minute,
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def check_once(self, day: Optional[datetime] = None) -> int:
        """
        执行一次结算推送

        Args:
            day: 结算日期（默认昨天）

        Returns:
            int: 推送的房间数
        """
        day = day or (datetime.now() - timedelta(days=1))
        if not await self._switch_on("daily_report"):
            logger.info("每日结算推送开关已关闭，跳过")
            return 0
        room_ids = self.binding_store.get_monitored_room_ids()
        if not room_ids:
            logger.info("无监控中的房间，跳过每日结算推送")
            return 0

        count, skipped = 0, 0
        for rid in room_ids:
            try:
                summary = await self.db_service.get_daily_summary(rid, day=day)
                # 仅推送有数据的房间（全部指标为0则跳过）
                if not self._has_data(summary):
                    skipped += 1
                    continue
                text = self._render(rid, summary)
                self.push_service.send_message(text, room_id=rid)
                count += 1
            except Exception as e:
                logger.error(f"房间 {rid} 每日结算推送失败: {e}")

        if skipped:
            logger.info(f"每日结算推送完成，推送 {count} 个房间，跳过 {skipped} 个无数据房间")
        else:
            logger.info(f"每日结算推送完成，共推送 {count} 个房间")
        return count

    @staticmethod
    def _has_data(s: dict) -> bool:
        """汇总是否包含任何数据"""
        return any([
            s["danmaku_count"], s["gift_count"], s["ship_count"],
            s["sc_count"], s["red_pocket"],
        ])

    def _render(self, room_id: int, s: dict, title: str = "【每日结算】") -> str:
        """结算消息文本（优先使用Template.daily_report自定义模板）"""
        fields = {
            "title": title,
            "room_id": room_id,
            "day": s["day"],
            "danmaku_count": s["danmaku_count"],
            "gift_gold": s["gift_gold"],
            "gift_count": s["gift_count"],
            "ship_total": s["ship_total"],
            "ship_count": s["ship_count"],
            "sc_total": s["sc_total"],
            "sc_count": s["sc_count"],
            "red_pocket": s["red_pocket"],
        }
        text = self.push_service._render(self.push_service.templates.daily_report, fields)
        if text is not None:
            return text
        return (
            f"{title}直播间{room_id} {s['day']}\n"
            f"· 弹幕：{s['danmaku_count']}条\n"
            f"· 礼物：{s['gift_gold']}元（{s['gift_count']}次）\n"
            f"· 上舰：{s['ship_total']}元（{s['ship_count']}次）\n"
            f"· SC：{s['sc_total']}元（{s['sc_count']}次）\n"
            f"· 红包：{s['red_pocket']}电池"
        )
