# -*- coding: utf-8 -*-
"""
主进程维护循环

- CookieMaintainer：Cookie账号池周期检查与刷新
- StreamerRefresher：绑定主播信息每日0点批量刷新
- GroupInfoRefresher：群信息每日刷新
"""

import asyncio
import os

from datetime import datetime, timedelta
from typing import List, Optional

from ..utils.logger import get_logger


logger = get_logger("manager")


class CookieMaintainer:
    """
    Cookie维护循环（主进程）

    周期检查账号池：临近过期的账号用无头浏览器载入刷新（续期
    SESSDATA与浏览器票据），刷新失败且nav验证失效的账号标记为
    失效并私聊提醒管理员（不在群里发送）。
    """

    def __init__(
        self,
        cookie_pool,
        bot_client=None,
        admin_openids: Optional[list] = None,
        main_admin_openids: Optional[list] = None,
        interval: int = 6 * 3600,
        db_service=None,
        db_size_warn_mb: int = 500
    ):
        """
        初始化维护循环

        Args:
            cookie_pool: Cookie账号池
            bot_client: QQ机器人客户端（私聊提醒用，可后续设置）
            admin_openids: 管理员openid列表
            main_admin_openids: 主管理员列表（敏感通知，未配置时发全部管理员）
            interval: 检查间隔（秒）
            db_service: 数据库服务（体积监控用）
            db_size_warn_mb: 数据库体积预警阈值（MB）
        """
        self.pool = cookie_pool
        self.bot_client = bot_client
        self.admin_openids = [str(o) for o in (admin_openids or [])]
        self.main_admin_openids = [str(o) for o in (main_admin_openids or [])]
        self.interval = interval
        self._last_rescan_date: str = ""
        # 数据量监控（永久保留策略下的增长提醒）
        self.db_service = db_service
        self.db_size_warn_mb = db_size_warn_mb
        self._db_warned = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动维护循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="cookie-maintainer")
            logger.info(f"Cookie维护循环已启动（间隔{self.interval // 3600}小时）")

    async def stop(self) -> None:
        """停止维护循环"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def run(self) -> None:
        """维护主循环"""
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check_once()
            except Exception as e:
                logger.error(f"Cookie维护检查失败: {e}")

    async def check_once(self) -> None:
        """执行一轮维护：临近过期刷新、失效标记并提醒管理员"""
        due = self.pool.refresh_due_accounts()
        if not due:
            return

        # 惰性导入避免未安装playwright时影响其他功能
        from ..services.bili_login import BiliLoginService, verify_nav

        service = BiliLoginService()
        for account in due:
            logger.info(f"刷新账号 {account.uid}（{account.nickname}）的cookie...")
            try:
                result = await service.refresh(account.cookie_string)
            except Exception as e:
                logger.warning(f"账号 {account.uid} cookie刷新异常: {e}")
                continue

            if result.ok and result.uid == account.uid:
                await self.pool.upsert(
                    uid=result.uid,
                    nickname=result.nickname,
                    cookie_string=result.cookie_string,
                    sessdata=result.sessdata,
                    bili_jct=result.bili_jct,
                    ac_time_value=result.ac_time_value,
                    sessdata_expires_at=result.sessdata_expires_at,
                )
                continue

            # 刷新失败：nav复核登录态（同步requests，移出事件循环）
            ok, _, _ = await asyncio.to_thread(verify_nav, account.cookie_string)
            if ok:
                logger.warning(
                    f"账号 {account.uid} 刷新失败但登录态仍有效，等待下轮: {result.error}"
                )
                continue

            reason = result.error or "维护刷新失败且登录态已失效"
            await self.pool.report_invalid(account.uid, reason)
            await self._notify_admins(
                f"⚠️ 账号 {account.uid}（{account.nickname}）的Cookie已失效并移出可用池。\n"
                f"原因：{reason}\n请私聊机器人发送 /登录 重新扫码添加。",
                sensitive=True,
            )
            warn = self.pool.warn_if_needed()
            if warn:
                await self._notify_admins(f"⚠️ {warn}", sensitive=True)

        # 失效账号自动重扫：nav复核通过则自动回池（如临时风控恢复场景，每日一次）
        await self._rescan_expired()

        # 数据库体积检查（永久保留策略下的增长监控，提醒而非删除）
        if self.db_size_warn_mb > 0:
            await self._check_db_size(self.db_size_warn_mb)

    async def _check_db_size(self, warn_mb: int = 500) -> None:
        """数据库体积超阈值时私聊提醒管理员（只提醒不删除，符合永久保留决策）"""
        db_service = self.db_service
        # db_path在DatabaseManager上（DatabaseService.db_manager转发）
        db_manager = getattr(db_service, "db_manager", None)
        path = getattr(db_manager, "db_path", None) if db_manager else None
        if not path or not os.path.exists(path):
            return
        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb < warn_mb:
            self._db_warned = False
            return
        if getattr(self, "_db_warned", False):
            return  # 已提醒过且未恢复，不重复
        self._db_warned = True
        msg1 = (
            "⚠️ 数据库体积已达 " + str(round(size_mb, 1)) + "MB（阈值" + str(warn_mb) + "MB）。"
        )
        msg2 = "数据按策略永久保留，不会自动删除；建议导出后归档，或考虑迁移到MySQL/PostgreSQL。"
        await self._notify_admins(msg1 + msg2, sensitive=True)

    async def _rescan_expired(self) -> int:
        """
        nav复核全部expired账号，登录态恢复的自动回到可用池（每日一次）

        Returns:
            int: 恢复的账号数量
        """
        expired = [a for a in self.pool.accounts if a.status == "expired"]
        if not expired:
            return 0

        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_rescan_date == today:
            return 0
        self._last_rescan_date = today

        from ..services.bili_login import verify_nav
        restored = 0
        for account in expired:
            ok, _, uname = await asyncio.to_thread(verify_nav, account.cookie_string)
            if ok:
                await self.pool.reactivate(account.uid, reason="自动重扫发现登录态恢复")
                restored += 1
                logger.info(f"账号 {account.uid}（{uname or account.nickname}）"
                            f"重扫通过，已自动恢复可用")
                await self._notify_admins(
                    f"✅ 账号 {account.uid}（{uname or account.nickname}）"
                    f"重扫发现登录态恢复，已自动回到可用池"
                )
        if restored:
            logger.info(f"自动重扫恢复 {restored} 个账号")
        return restored

    async def _notify_admins(self, content: str, sensitive: bool = False) -> None:
        """
        私聊提醒管理员

        Args:
            content: 通知内容
            sensitive: 敏感通知发主管理员（未配置时发全部管理员）
        """
        targets = self.admin_openids
        if sensitive and self.main_admin_openids:
            targets = self.main_admin_openids
        if not targets:
            logger.info(f"(无管理员配置，仅记录) {content}")
            return
        if self.bot_client is None:
            logger.warning(f"机器人未启动，无法私聊提醒管理员: {content}")
            return
        await self.bot_client.notify_admins(targets, content)


class StreamerRefresher:
    """
    绑定主播信息每日0点批量刷新（主进程）

    通过免登录的get_status_info_by_uids批量查询全部绑定主播，
    更新streamer与live_room两表快照（WebPanel主播名直接读库，
    不再实时调API）。
    """

    BATCH_SIZE = 50  # get_status_info_by_uids单次上限

    def __init__(self, binding_store, db_service):
        """
        Args:
            binding_store: 群绑定存储（取全部绑定主播uid）
            db_service: 数据库服务（快照写入）
        """
        self.binding_store = binding_store
        self.db_service = db_service
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动每日刷新循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="streamer-refresher")
            logger.info("主播信息每日刷新任务已启动（每日0点）")

    async def stop(self) -> None:
        """停止刷新循环"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @staticmethod
    def _seconds_until_midnight() -> int:
        """距下一个0点的秒数（本地时区）"""
        now = datetime.now()
        next_mid = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=5, microsecond=0)
        return max(int((next_mid - now).total_seconds()), 1)

    async def run(self) -> None:
        """每日0点刷新主循环"""
        while True:
            await asyncio.sleep(self._seconds_until_midnight())
            try:
                count = await self.refresh_once()
                logger.info(f"每日主播信息刷新完成：{count}个主播")
            except Exception as e:
                logger.error(f"每日主播信息刷新失败: {e}")

    async def refresh_once(self) -> int:
        """
        执行一轮批量刷新

        Returns:
            int: 本次更新的主播数量

        Raises:
            Exception: 批量查询失败时上抛（由调用方记录）
        """
        uids = self.binding_store.get_bound_uids()
        if not uids:
            return 0

        from ..services.bilibili import BilibiliAPI
        api = BilibiliAPI()
        total = 0
        for i in range(0, len(uids), self.BATCH_SIZE):
            batch = uids[i:i + self.BATCH_SIZE]
            status = await asyncio.to_thread(api.get_status_info_by_uids, batch)
            await self.db_service.apply_status_info(status)
            total += len(status)
        return total

    async def backfill_if_empty(self) -> None:
        """主播表为空时立即回填一次（旧绑定升级后无需等到0点）"""
        try:
            if await self.db_service.streamer_count() == 0:
                count = await self.refresh_once()
                if count:
                    logger.info(f"主播表为空，已回填{count}个绑定主播")
        except Exception as e:
            logger.warning(f"主播信息回填失败: {e}")


class GroupInfoRefresher:
    """
    群信息每日刷新（主进程）

    经群基本信息接口（白名单，30QPM）检查并更新bot_group表的
    简介/分类/标签/成员数；查询数据项为空时落NULL。
    """

    CALL_INTERVAL = 2  # 单次调用间隔（秒），30QPM限流

    def __init__(self, db_service, bot_client=None):
        self.db_service = db_service
        self.bot_client = bot_client
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动每日刷新循环（需在事件循环内调用）"""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="group-info-refresher")
            logger.info("群信息每日刷新任务已启动（每日0点）")

    async def stop(self) -> None:
        """停止刷新循环"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    @staticmethod
    def _seconds_until_midnight() -> int:
        """距下一个0点的秒数（本地时区，+30秒错峰）"""
        now = datetime.now()
        next_mid = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=30, microsecond=0)
        return max(int((next_mid - now).total_seconds()), 1)

    async def run(self) -> None:
        """每日0点刷新主循环"""
        while True:
            await asyncio.sleep(self._seconds_until_midnight())
            try:
                count = await self.refresh_once()
                logger.info(f"每日群信息刷新完成：{count}个群")
            except Exception as e:
                logger.error(f"每日群信息刷新失败: {e}")

    async def refresh_once(self, openids: Optional[List[str]] = None) -> int:
        """
        执行一轮群信息刷新（逐群查询，限流间隔）

        Args:
            openids: 指定群列表（None=bot_group表全部群）

        Returns:
            int: 成功更新的群数量
        """
        if self.bot_client is None:
            return 0
        targets = openids if openids is not None \
            else await self.db_service.list_group_openids()
        updated = 0
        for openid in targets:
            info = await self.bot_client.get_group_info(openid)
            if info:
                await self.db_service.update_group_info(openid, info)
                updated += 1
            await asyncio.sleep(self.CALL_INTERVAL)
        return updated

    async def backfill_missing(self) -> int:
        """存在信息空值的群立即查询更新（启动时调用）"""
        try:
            missing = await self.db_service.list_groups_missing_info()
        except Exception as e:
            logger.warning(f"查询缺失群信息的群失败: {e}")
            return 0
        if not missing:
            return 0
        logger.info(f"{len(missing)}个群信息存在空值，立即回填")
        return await self.refresh_once(missing)
