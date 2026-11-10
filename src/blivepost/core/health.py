# -*- coding: utf-8 -*-
"""
健康检查端点与定时备份任务

/healthz：游客可访问的只读端点（不暴露账号/群等运营情报），报告
进程状态/房间子进程存活数/动态轮询数/DB可达性/发送队列深度。

BackupJob：Setting.backup_*驱动的定时脱敏备份（exports/backups/滚动保留），
复用WebPanel的_build_export_zip脱敏zip逻辑。
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..utils.logger import get_logger

if TYPE_CHECKING:
    from ..config.settings import Settings
    from ..database.service import DatabaseService

logger = get_logger("health")


async def build_health_report(db_service, monitor_manager, dynamic_manager,
                              bot_client) -> dict:
    """构建健康报告（只读、脱敏：不含账号UID/群openid/cookie等运营情报）"""
    report: dict = {"status": "ok",
                    "time": datetime.now().isoformat(timespec="seconds")}

    # 房间子进程存活
    if monitor_manager is not None:
        rooms = monitor_manager.room_ids()
        alive = sum(1 for r in rooms if monitor_manager.has(r))
        report["rooms"] = {"total": len(rooms), "alive": alive}
        if rooms and alive < len(rooms):
            report["status"] = "degraded"

    # 动态轮询进程
    if dynamic_manager is not None:
        report["dynamics_enabled"] = bool(dynamic_manager.enabled)

    # DB可达性
    if db_service is not None:
        try:
            await db_service.get_table_counts()
            report["db"] = "ok"
        except Exception:
            report["db"] = "error"
            report["status"] = "error"
    else:
        report["db"] = "unknown"

    # 发送队列深度（bot未启动时略）
    if bot_client is not None and bot_client._send_queue is not None:
        depth = bot_client._send_queue.qsize()
        report["send_queue"] = depth
        if depth >= bot_client.SEND_QUEUE_SIZE:
            report["status"] = "degraded"

    # 机器人
    if bot_client is not None:
        report["bot"] = "started" if bot_client.started else "stopped"
        if not bot_client.started:
            report["status"] = "degraded"

    return report


class BackupJob:
    """定时脱敏备份（exports/backups/滚动保留）

    Setting.backup_keep=0 关闭；默认每天 backup_hour:backup_minute 执行一次，
    导出脱敏zip到 exports/backups/blivepost_backup_YYYYMMDD_HHMMSS.zip，
    超出backup_keep份数的旧备份自动删除。
    """

    def __init__(self, settings: "Settings", db_service: "DatabaseService"):
        self.hour = settings.message.backup_hour
        self.minute = settings.message.backup_minute
        self.keep = settings.message.backup_keep
        self._db = db_service
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """启动定时循环（需在事件循环内调用）"""
        if self.keep <= 0:
            logger.info("定时备份已关闭（backup_keep=0）")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="backup-job")
            logger.info(f"定时备份已启动（每天{self.hour:02d}:{self.minute:02d}，"
                        f"保留{self.keep}份）")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                delay = self._seconds_until_next()
                await asyncio.sleep(delay)
                await self.backup_now()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"定时备份失败: {e}")
                await asyncio.sleep(3600)  # 出错后1小时重试

    def _seconds_until_next(self) -> float:
        now = datetime.now()
        target = now.replace(hour=self.hour, minute=self.minute,
                             second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def backup_now(self) -> Optional[Path]:
        """立即执行一次脱敏备份，返回备份文件路径"""
        from ..services.webpanel_admin import AdminMixin  # 复用脱敏zip逻辑
        sqlite_path = getattr(self._db, "sqlite_path", lambda: None)()
        if not sqlite_path:
            logger.warning("定时备份：非SQLite存储，跳过")
            return None
        db_path = Path(sqlite_path)
        if not db_path.exists():
            logger.warning("定时备份：数据库文件不存在")
            return None

        backup_dir = Path("exports/backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"blivepost_backup_{stamp}.zip"

        buf = await asyncio.get_running_loop().run_in_executor(
            None, AdminMixin._build_export_zip, db_path, db_path.parent, False)
        backup_path.write_bytes(buf.getvalue())
        logger.info(f"定时备份完成: {backup_path} ({backup_path.stat().st_size}B)")

        # 滚动保留
        backups = sorted(backup_dir.glob("blivepost_backup_*.zip"))
        for old in backups[:-self.keep]:
            try:
                old.unlink()
                logger.info(f"滚动清理旧备份: {old.name}")
            except OSError:
                pass
        return backup_path
