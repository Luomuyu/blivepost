# -*- coding: utf-8 -*-
"""
BLivePost 命令行入口

主进程职责：QQ机器人（在线+群命令+推送路由）、绑定管理、数据库落库、
监控进程管理。直播间监控全部由子进程完成（见core/manager.py）。
"""

import argparse
import asyncio
import multiprocessing
import sys
from pathlib import Path
from typing import Optional

from .core.manager import MonitorManager, PushQueueConsumer, CookieMaintainer
from .database import DatabaseManager, DatabaseService
from .services.admin_commands import AdminCommandHandler
from .services.bilibili import BilibiliAPI
from .services.cookie_pool import CookiePool
from .services.bindings import BindingStore
from .services.commands import CommandHandler
from .services.fingerprint import FingerprintService
from .services.push import PushService
from .services.report import DailyReportJob
from .services.dynamics import DynamicManager, LiveStateWatcher
from .services.webpanel import WebPanel
from .services.bili_login import ensure_playwright_chromium
from .services.qqbot import QQBotClient
from .config.settings import Settings
from .utils.logger import Logger, get_logger


def parse_args() -> argparse.Namespace:
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        prog="blivepost",
        description="B站直播监控与推送系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  blivepost                          # 使用默认配置运行
  blivepost -c config.yaml           # 指定配置文件
  blivepost -s SESSDATA              # 指定B站登录凭证
  blivepost --log-dir ./logs         # 指定日志目录
        """
    )

    parser.add_argument(
        "-c", "--config",
        default="config.yaml",
        help="配置文件路径 (默认: config.yaml)"
    )
    parser.add_argument(
        "-s", "--sessdata",
        default=None,
        help="B站登录凭证 SESSDATA (优先级高于Redis和配置文件)"
    )
    parser.add_argument(
        "--log-dir",
        default="logs",
        help="日志文件目录"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version="%(prog)s 1.1.0"
    )

    return parser.parse_args()


def _merge_unique(*lists) -> list:
    """合并多个openid名单并去重（保持顺序）"""
    seen, result = set(), []
    for lst in lists:
        for item in lst or []:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result


def get_sessdata_from_redis(settings) -> Optional[str]:
    """
    从Redis获取SESSDATA

    Args:
        settings: 配置对象

    Returns:
        Optional[str]: SESSDATA，如果获取失败返回None
    """
    try:
        import redis

        r = redis.Redis(
            host=settings.redis.host,
            port=settings.redis.port,
            password=settings.redis.password if settings.redis.password else None,
            db=settings.redis.db,
            decode_responses=True
        )
        sessdata = r.get("SESSDATA")
        if sessdata:
            return sessdata
    except redis.exceptions.ConnectionError:
        pass
    except Exception:
        pass
    return None


async def main_async(
    config_path: str,
    sessdata: Optional[str],
    log_dir: Optional[str] = None
) -> None:
    """异步主函数（主进程）"""
    # 初始化日志
    Logger().setup(
        name="blivepost",
        log_dir=log_dir,
        console=True
    )
    logger = get_logger()

    logger.info("=" * 50)
    logger.info("BLivePost v1.0.0 - B站直播监控与推送系统")
    logger.info("=" * 50)

    settings = None
    qqbot = None
    monitor_manager = None
    db_manager = None
    daily_report = None
    live_watcher = None
    consumer = None
    web_panel = None

    try:
        # 加载配置（文件不存在时自动生成默认配置）
        settings = Settings.load(config_path)

        # 初始化数据库
        logger.info(f"正在初始化数据库 ({settings.database.type})...")
        db_manager = DatabaseManager(settings.database)
        await db_manager.initialize()
        db_service = DatabaseService(db_manager)
        logger.info("数据库初始化成功")

        # 加载群绑定与Cookie账号池
        binding_store = BindingStore(db_service)
        await binding_store.load()
        cookie_pool = CookiePool(db_service)
        await cookie_pool.load()

        # 推送服务（按绑定关系路由）
        push_service = PushService(settings)
        push_service.binding_store = binding_store

        # 确定最终使用的sessdata
        # 优先级：命令行参数 > Redis > 配置文件
        final_sessdata = sessdata

        if final_sessdata is None:
            redis_sessdata = get_sessdata_from_redis(settings)
            if redis_sessdata:
                logger.info("从Redis获取到SESSDATA")
                final_sessdata = redis_sessdata
            else:
                final_sessdata = settings.auth.sessdata
                if final_sessdata:
                    logger.info("使用配置文件中的SESSDATA")
                else:
                    logger.warning("未配置SESSDATA，部分功能可能受限")

        # 指纹服务与B站API（主进程：房间信息查询供命令回复用）
        # 同步requests经to_thread执行，不阻塞事件循环
        fingerprint = await asyncio.to_thread(FingerprintService, settings, final_sessdata)
        bilibili_api = BilibiliAPI(final_sessdata)
        fp_cookie = await asyncio.to_thread(fingerprint.get_cookie)
        if fp_cookie:
            bilibili_api.set_fp_cookie(fp_cookie)

        # 多房间监控管理器（直播间监控在子进程中执行）
        monitor_manager = MonitorManager(
            settings,
            bilibili_api=bilibili_api,
            log_dir=log_dir,
            cookie_pool=cookie_pool
        )

        # 阻塞安装playwright chromium内核（已安装时秒级跳过；安装完成后才启动后续服务）
        logger.info("检查playwright chromium内核...")
        await ensure_playwright_chromium()

        # 动态轮询管理器（与监控共用跨进程队列；直播状态文件放数据库目录，实例私有）
        state_path = Path(settings.database.sqlite_path).parent / "dynamic_live_state.json"
        dynamic_manager = DynamicManager(
            settings, monitor_manager.push_queue, cookie_pool=cookie_pool,
            enabled=settings.message.dynamic_monitor, state_path=state_path,
        )

        # 启动QQ官方机器人
        if settings.qqbot.app_id:
            logger.info("正在启动QQ官方机器人...")
            qqbot = QQBotClient(settings.qqbot.app_id, settings.qqbot.app_secret)
            qqbot.start(
                CommandHandler(
                    binding_store, monitor_manager, db_service=db_service,
                    group_admin_openids=_merge_unique(
                        settings.admin.openids, settings.admin.main_openids
                    ),
                ),
                c2c_handler=AdminCommandHandler(
                    cookie_pool, settings.admin.openids, bot_client=qqbot,
                    db_service=db_service, monitor_manager=monitor_manager
                ),
            )
            push_service.bot_client = qqbot
            if settings.admin.openids:
                main_count = len(settings.admin.main_openids)
                logger.info(
                    f"已配置 {len(settings.admin.openids)} 个管理员"
                    + (f"（含 {main_count} 个主管理员）" if main_count else "")
                    + "（私聊/登录 命令可用）"
                )
            else:
                logger.warning("未配置Admin.openids，私聊/登录 命令不可用且无法收到Cookie失效提醒")
        else:
            logger.warning("未配置QQBot的app_id，消息推送不可用")

        # 启动子进程消息消费者（推送路由 + 集中落库 + 凭证失效处理）
        consumer = PushQueueConsumer(
            monitor_manager.push_queue,
            push_service,
            db_service=db_service,
            monitor_manager=monitor_manager,
            cookie_pool=cookie_pool,
            admin_openids=settings.admin.openids,
            main_admin_openids=settings.admin.main_openids,
            binding_store=binding_store
        )
        consumer.start()

        # 每日结算推送（Setting.daily_report开关，默认关闭）
        if settings.message.daily_report or settings.message.weekly_report \
                or settings.message.monthly_report:
            daily_report = DailyReportJob(
                binding_store, push_service, db_service,
                weekly=settings.message.weekly_report,
                monthly=settings.message.monthly_report,
            )
            daily_report.start()

        # 直播状态watcher（动态监控开启时：批量查询+共享文件+开播提醒）
        if settings.message.dynamic_monitor:
            live_watcher = LiveStateWatcher(
                binding_store, monitor_manager.push_queue, state_path=state_path
            )
            live_watcher.start()

        # Web状态面板（Setting.web_panel开关，默认仅本机访问）
        if settings.message.web_panel:
            web_panel = WebPanel(
                cookie_pool, monitor_manager,
                binding_store=binding_store, db_service=db_service,
                host=settings.message.web_host, port=settings.message.web_port,
            )
            await web_panel.start()

        # Cookie维护循环（临近过期自动刷新，失效私聊提醒管理员）
        maintainer = CookieMaintainer(
            cookie_pool,
            bot_client=qqbot,
            admin_openids=settings.admin.openids,
            main_admin_openids=settings.admin.main_openids
        )
        maintainer.start()

        # 恢复上次运行中处于监控状态的房间与动态订阅
        await monitor_manager.restore(binding_store.get_monitored_room_ids())
        dynamic_ups = binding_store.get_dynamic_up_uids() \
            if settings.message.dynamic_monitor else []
        if dynamic_ups:
            logger.info(f"恢复 {len(dynamic_ups)} 个UP主的动态轮询")
            for up_uid in dynamic_ups:
                try:
                    await dynamic_manager.ensure(up_uid)
                except Exception as e:
                    logger.error(f"恢复UP主 {up_uid} 动态轮询失败: {e}")

        # 配置文件指定的主监控房间（同样由子进程监控）
        if settings.live.room_id:
            logger.info(f"启动配置房间 {settings.live.room_id} 的监控")
            await monitor_manager.ensure(int(settings.live.room_id))

        # 常驻等待（机器人连接在后台任务中运行）
        await asyncio.Event().wait()

    except FileNotFoundError as e:
        logger.error(f"配置文件错误: {e}")
        sys.exit(1)
    except ValueError as e:
        logger.error(f"配置错误: {e}")
        sys.exit(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到中断信号，正在停止...")
    except Exception as e:
        logger.error(f"运行错误: {e}")
        sys.exit(1)
    finally:
        # 停止全部房间监控进程
        if monitor_manager:
            await monitor_manager.stop_all()

        # 停止子进程消息消费者与Web面板
        try:
            if consumer:
                await consumer.stop()
        except Exception:
            pass
        try:
            if web_panel:
                await web_panel.stop()
        except Exception:
            pass

        # 停止每日结算、Cookie维护与直播状态watcher
        try:
            await daily_report.stop()
        except Exception:
            pass
        try:
            await live_watcher.stop()
        except Exception:
            pass
        try:
            await maintainer.stop()
        except Exception:
            pass

        # 停止QQ官方机器人
        if qqbot:
            await qqbot.stop()

        # 关闭数据库连接
        if db_manager:
            await db_manager.close()

        logger.info("程序已退出")


def main() -> None:
    """主入口函数"""
    # 冻结打包（PyInstaller等）时子进程spawn必需
    multiprocessing.freeze_support()
    args = parse_args()

    try:
        asyncio.run(main_async(
            config_path=args.config,
            sessdata=args.sessdata,
            log_dir=args.log_dir
        ))
    except KeyboardInterrupt:
        pass


def run() -> None:
    """
    运行入口点（供外部调用）

    用于从Python代码中直接调用：
        from blivepost import run
        run()
    """
    main()


if __name__ == "__main__":
    main()
