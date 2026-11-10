# -*- coding: utf-8 -*-
"""
QQ官方机器人推送客户端（基于botpy）
"""

import asyncio
import hashlib
import os
import time
from typing import Any, Callable, Iterable, Optional, TYPE_CHECKING

import aiohttp
from botpy.http import Route
import botpy
from botpy.message import C2CMessage, GroupMessage

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from .commands import CommandHandler
    from .admin_commands import AdminCommandHandler
    from .contacts import ContactEventHandler


logger = get_logger("qqbot")


class _PushClient(botpy.Client):
    """botpy客户端：负责消息API调用与群/私聊命令事件处理"""

    def __init__(
        self,
        intents,
        command_handler: Optional["CommandHandler"] = None,
        c2c_handler: Optional["AdminCommandHandler"] = None,
        contact_handler: Optional["ContactEventHandler"] = None,
        log_config: Optional[dict] = None,
        ext_handlers: Any = True,
        debug: bool = False,
    ):
        log_kwargs = {"log_config": log_config} if log_config else {}
        super().__init__(intents=intents, ext_handlers=ext_handlers, **log_kwargs)
        self._command_handler = command_handler
        self._c2c_handler = c2c_handler
        self._contact_handler = contact_handler
        self._debug = debug

    @staticmethod
    def _debug_author_fields(author) -> str:
        """debug模式用：发送者身份明细（openid各形态/union/昵称等已有值的字段）"""
        fields = []
        for attr in ("user_openid", "member_openid", "union_openid",
                     "union_user_account", "username", "member_role", "id"):
            value = getattr(author, attr, None)
            if value is not None:
                fields.append(f"{attr}={value}")
        return " ".join(fields)

    def _debug_log_message(self, kind: str, message, author) -> None:
        """debug模式：输出收到的QQ消息发送者身份明细"""
        if not self._debug:
            return
        extra = ""
        group_openid = getattr(message, "group_openid", None)
        if group_openid:
            extra = f" group_openid={group_openid}"
        logger.info(f"[debug] 收到{kind}消息 sender: "
                    f"{self._debug_author_fields(author)}{extra}")

    async def on_group_add_robot(self, event) -> None:
        """机器人加入群"""
        if self._contact_handler:
            await self._contact_handler.on_group_add(event.group_openid)

    async def on_group_del_robot(self, event) -> None:
        """机器人被移出群"""
        if self._contact_handler:
            await self._contact_handler.on_group_del(event.group_openid)

    async def on_group_msg_receive(self, event) -> None:
        """群消息接收开启"""
        if self._contact_handler:
            await self._contact_handler.on_group_receive(event.group_openid, 1)

    async def on_group_msg_reject(self, event) -> None:
        """群消息接收关闭"""
        if self._contact_handler:
            await self._contact_handler.on_group_receive(event.group_openid, 0)

    async def on_friend_add(self, event) -> None:
        """用户添加好友"""
        if self._contact_handler:
            # union_openid在author子对象上（C2CManageEvent顶层无此字段）
            union = getattr(getattr(event, "author", None), "union_openid", None) or ""
            await self._contact_handler.on_friend_add(getattr(event, "openid", ""), union)

    async def on_friend_del(self, event) -> None:
        """用户删除好友"""
        if self._contact_handler:
            await self._contact_handler.on_friend_del(getattr(event, "openid", ""))

    async def on_c2c_msg_receive(self, event) -> None:
        """单聊消息接收开启"""
        if self._contact_handler:
            await self._contact_handler.set_friend_receive_async(
                getattr(event, "openid", ""), 1)

    async def on_c2c_msg_reject(self, event) -> None:
        """单聊消息接收关闭"""
        if self._contact_handler:
            await self._contact_handler.set_friend_receive_async(
                getattr(event, "openid", ""), 0)

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        """私聊消息事件：回填好友union后分发给管理员命令处理器"""
        self._debug_log_message("私聊", message, message.author)
        if self._contact_handler:
            # 好友记录以单聊user_openid为主键，union_openid仅作回填值
            author = message.author
            union = getattr(author, "union_openid", None)
            openid = getattr(author, "user_openid", None)
            if union and openid:
                try:
                    await self._contact_handler.backfill_friend_union(openid, union)
                except Exception as e:
                    logger.error(f"回填好友union_openid失败: {e}")
        if self._c2c_handler is None:
            return
        try:
            await self._c2c_handler.handle(message)
        except Exception as e:
            logger.error(f"处理私聊命令失败: {e}")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """群@机器人消息事件：分发给命令处理器并被动回复"""
        self._debug_log_message("群聊", message, message.author)
        if self._command_handler is None:
            return
        try:
            reply = await self._command_handler.handle(message)
            if reply:
                await message.reply(content=reply)
        except Exception as e:
            logger.error(f"处理群命令失败: {e}")
            try:
                await message.reply(content="命令执行失败，请稍后重试")
            except Exception as e2:
                logger.error(f"回复错误提示失败: {e2}")


class QQBotClient:
    """
    QQ官方机器人客户端

    官方消息接口要求机器人保持websocket在线，因此botpy Client在
    start()后常驻后台任务，消息发送通过client.api进行。

    消息发送入口push()是线程安全的：经有界发送队列由固定数量
    工作协程消费（限制并发与内存占用，队列满时丢弃并告警），
    在其他线程（如动态监控线程）中调度到主事件循环。
    """

    # 发送队列容量与工作协程数：超出容量的推送丢弃（防内存无限增长）
    SEND_QUEUE_SIZE = 200
    SEND_WORKERS = 2
    # stop时等待队列排空的上限秒数
    SEND_DRAIN_TIMEOUT = 30.0

    def __init__(self, app_id: str, app_secret: str, debug: bool = False):
        """
        初始化QQ官方机器人客户端

        Args:
            app_id: 机器人AppID
            app_secret: 机器人AppSecret
            debug: debug模式（输出收到消息的发送者身份明细）
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.debug = debug
        self._me_cache: Optional[dict] = None
        self._client: Optional[_PushClient] = None
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # 有界发送队列与工作协程（start时创建；防fire-and-forget任务无限堆积）
        self._send_queue: Optional[asyncio.Queue] = None
        self._send_workers: list = []
        # 已确认不支持艾特全体的群（发送被拒后降级成功前记录；进程生命周期内不再尝试）
        self._at_all_unsupported: set = set()
        # 群角色缓存：{group_openid: (过期monotonic, role)}——bot_state仅白名单可用且30QPM
        self._group_role_cache: dict = {}
        # 艾特全体发送失败时的配额退还钩子（__main__注入db_service.clear_group_at_all）
        self.at_all_refund: Optional[Callable[[str], Any]] = None
        # 主动消息权限拒绝提示的去重：{group_openid: 上次提示的monotonic时刻}
        self._deny_warned: dict = {}

    @property
    def started(self) -> bool:
        """客户端是否正在运行"""
        return self._task is not None and not self._task.done()

    # 群角色缓存时长（秒）：角色变更少、接口30QPM限频
    GROUP_ROLE_TTL = 600.0

    async def get_group_role(self, group_openid: str) -> Optional[str]:
        """机器人在群内的成员角色（owner=群主/admin=管理员/member=普通成员）。

        GET /v2/groups/{group_openid}/bot_state——注意该接口**仅白名单机器人**
        可用（错误码11253），查询失败/无权限/未启动时返回None；调用方应把
        None视作"未知"，放行艾特尝试并依赖发送失败降级兜底。
        结果缓存GROUP_ROLE_TTL秒。
        """
        now = time.monotonic()
        cached = self._group_role_cache.get(group_openid)
        if cached and cached[0] > now:
            return cached[1]
        if not self.started or self._client is None:
            return None
        role: Optional[str] = None
        try:
            route = Route("GET", "/v2/groups/{group_openid}/bot_state",
                          group_openid=group_openid)
            data = await self._client.api._http.request(route)
            if isinstance(data, dict):
                # 文档示例为平铺字段；兼容可能的data包裹
                role = (data.get("member_role")
                        or (data.get("data") or {}).get("member_role"))
        except Exception as e:
            logger.debug(f"查询群角色失败(群{group_openid[:6]}…): {e}")
            role = None
        self._group_role_cache[group_openid] = (now + self.GROUP_ROLE_TTL, role)
        return role

    async def _refund_at_all(self, group_openid: str) -> None:
        """艾特全体发送失败后退还当日配额（钩子由__main__注入；失败仅记日志）"""
        refund = self.at_all_refund
        if refund is None:
            return
        try:
            await refund(group_openid)
            logger.info(f"群{group_openid[:6]}… 艾特全体发送失败，已退还当日配额")
        except Exception as e:
            logger.warning(f"退还艾特全体配额失败(群{group_openid[:6]}…): {e}")

    async def get_me(self, force_refresh: bool = False) -> Optional[dict]:
        """
        获取机器人详情（GET /users/@me：username/avatar/share_url等）

        Args:
            force_refresh: 强制刷新缓存

        Returns:
            Optional[dict]: {"id","username","avatar","share_url"}，失败None
        """
        if not force_refresh and self._me_cache is not None:
            return self._me_cache
        if not self.started or self._client is None:
            return None
        try:
            me = await self._client.api.me()
            # botpy的http.request返回解析后的dict（非User对象），按dict取字段
            if not isinstance(me, dict):
                me = {"id": getattr(me, "id", ""), "username": getattr(me, "username", ""),
                      "avatar": getattr(me, "avatar", ""), "share_url": getattr(me, "share_url", "")}
            self._me_cache = {
                "id": str(me.get("id") or ""),
                "username": str(me.get("username") or ""),
                "avatar": str(me.get("avatar") or ""),
                "share_url": str(me.get("share_url") or ""),
            }
            return self._me_cache
        except Exception as e:
            logger.error(f"获取机器人详情失败: {e}")
            return None

    def token_state(self) -> dict:
        """
        访问凭证状态诊断（对齐官方access-token文档）

        botpy在每次API请求前自动刷新token（有效期7200秒，
        请求级惰性刷新）；此方法暴露当前凭证的剩余有效期。

        Returns:
            dict: {"client_started", "token_ready", "token_expires_in"}
        """
        import time as _time
        state = {"client_started": self.started,
                 "token_ready": False,
                 "token_expires_in": 0}
        api = getattr(self._client, "api", None) if self._client else None
        http = getattr(api, "_http", None) if api is not None else None
        token = getattr(http, "_token", None) if http is not None else None
        if token is not None and getattr(token, "access_token", None):
            state["token_ready"] = True
            state["token_expires_in"] = max(
                int(token.expires_in - _time.time()), 0)
        return state

    def start(
        self,
        command_handler: Optional["CommandHandler"] = None,
        c2c_handler: Optional["AdminCommandHandler"] = None,
        contact_handler: Optional["ContactEventHandler"] = None,
    ) -> None:
        """
        启动botpy客户端（需在事件循环内调用）

        Args:
            command_handler: 群命令处理器（提供后开启群@消息事件监听）
            c2c_handler: 私聊命令处理器（提供后开启私聊消息事件监听）
        """
        if self.started:
            return

        self._loop = asyncio.get_running_loop()
        # 有界发送队列：push入队、固定worker消费（限并发+背压丢弃）
        self._send_queue = asyncio.Queue(maxsize=self.SEND_QUEUE_SIZE)
        self._send_workers = [
            asyncio.create_task(self._send_worker(), name=f"qqbot-send-{i}")
            for i in range(self.SEND_WORKERS)
        ]
        intents = (
            botpy.Intents(public_messages=True)
            if (command_handler is not None or c2c_handler is not None
                or contact_handler is not None)
            else botpy.Intents.none()
        )
        # botpy日志纳管：统一为项目格式，文件日志轮转写入logs/目录
        os.makedirs("logs", exist_ok=True)
        log_config = {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "std": {
                    "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "std",
                    "level": "INFO",
                },
                "file": {
                    "class": "logging.handlers.TimedRotatingFileHandler",
                    "formatter": "std",
                    "level": "INFO",
                    "filename": "logs/botpy.log",
                    "when": "D",
                    "backupCount": 7,
                    "encoding": "utf-8",
                },
            },
            "loggers": {
                "botpy": {
                    "handlers": ["console", "file"],
                    "level": "INFO",
                    "propagate": False,
                }
            },
        }
        self._client = _PushClient(
            intents=intents,
            command_handler=command_handler,
            c2c_handler=c2c_handler,
            contact_handler=contact_handler,
            log_config=log_config,
            # 禁用botpy默认的根目录botpy.log文件handler（logs/botpy.log已接管）
            ext_handlers=False,
            debug=self.debug,
        )
        self._task = self._loop.create_task(
            self._client.start(self.app_id, self.app_secret),
            name="qqbot-client",
        )
        self._task.add_done_callback(self._on_task_done)
        logger.info(f"QQ官方机器人客户端启动中 (appid={self.app_id})")

    @staticmethod
    def _on_task_done(task: asyncio.Task) -> None:
        """客户端任务退出回调（stop主动取消不算异常）"""
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return  # stop时被取消，非异常
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            logger.error(f"botpy客户端异常退出: {exc}")

    async def stop(self) -> None:
        """停止botpy客户端（含发送worker与start循环派生的ws/心跳/事件子任务）"""
        # 先收尾发送worker：排入哨兵让worker处理完队列余量后退出，
        # 超时（或队列满无法排哨兵）则取消（放弃剩余未发送消息）
        send_queue = self._send_queue
        if self._send_workers and send_queue is not None:
            for _ in self._send_workers:
                try:
                    send_queue.put_nowait(None)
                except asyncio.QueueFull:
                    break
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._send_workers),
                    timeout=self.SEND_DRAIN_TIMEOUT,
                )
            except (asyncio.TimeoutError, Exception):
                for worker in self._send_workers:
                    worker.cancel()
            self._send_workers = []
            self._send_queue = None

        # 取消start任务：botpy的start循环经ensure_future派生ws子任务且不保留
        # 句柄，取消不会传播到子任务；若留给事件循环拆除时强杀，会产出
        # CancelledError回溯、"会话未正常回队"与Unclosed client session噪音
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # 任务异常已由_on_task_done记录，不阻断关闭序列
            self._task = None

        if self._loop is not None and self._loop.is_running():
            await self._cancel_botpy_tasks(self._loop)
            self._loop = None

        # 子任务全部退出后再关http会话（避免在存活连接脚下关闭）
        if self._client is not None:
            try:
                await self._client.close()
            except Exception as e:
                logger.warning(f"关闭botpy客户端出错: {e}")
            self._client = None

    @staticmethod
    async def _cancel_botpy_tasks(loop: asyncio.AbstractEventLoop) -> None:
        """取消事件循环上botpy模块派生的余留任务（ws连接/心跳/事件回调）"""
        pending = []
        for t in asyncio.all_tasks(loop):
            if t is asyncio.current_task() or t.done():
                continue
            if QQBotClient._coro_module(t.get_coro()).startswith("botpy"):
                pending.append(t)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _coro_module(coro) -> str:
        """协程所属模块名（coroutine对象无__module__属性，从frame globals取）"""
        mod = getattr(coro, "__module__", None)
        if mod:
            return mod
        frame = getattr(coro, "cr_frame", None)
        if frame is not None:
            return frame.f_globals.get("__name__", "") or ""
        return ""

    def _note_send_denied(self, group_openid: str, error: Exception) -> bool:
        """
        识别平台主动消息权限拒绝并输出低频可操作提示

        平台限制：主动消息（无msg_id的下发，如开播/结算推送）需要单独
        授权/放量，未放量机器人约每群每月4条；无权限或额度耗尽返回
        40034105"主动消息失败, 无权限"。与消息接收开关（收@消息）无关。

        Returns:
            bool: 是否命中主动消息权限拒绝
        """
        text = str(error)
        if "40034105" not in text and "主动消息" not in text:
            return False
        now = time.monotonic()
        last = self._deny_warned.get(group_openid)
        if last is None or now - last >= 6 * 3600:
            self._deny_warned[group_openid] = now
            logger.warning(
                f"群{group_openid}主动消息被平台拒绝(40034105)：该群无主动消息权限"
                "或当月额度已用尽（未放量机器人约4条/群/月）。请到QQ开放平台为机器人"
                "申请主动消息能力放量；@机器人的指令回复属被动消息不受影响。"
                "（本提示每6小时输出一次，期间的接口报错为botpy原生日志）")
        return True

    async def send_group_message(self, group_openid: str, content: str,
                                 keyboard: Optional[dict] = None) -> bool:
        """
        向单个群发送文本主动消息

        Args:
            group_openid: 群openid
            content: 文本内容
            keyboard: 按钮keyboard结构（可选；发送失败自动去按钮重发，
                保证推送必达——按钮需平台单独授权）

        Returns:
            bool: 是否发送成功
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，消息发送失败")
            return False
        client = self._client

        async def _send(kb):
            await client.api.post_group_message(
                group_openid=group_openid,
                msg_type=0,
                content=content,
                keyboard=kb,
            )

        try:
            try:
                await _send(keyboard)
            except Exception as e:
                if keyboard is None:
                    raise
                logger.warning(f"带按钮消息发送失败，去按钮重发: {e}")
                await _send(None)
            return True
        except Exception as e:
            if self._note_send_denied(group_openid, e):
                logger.debug(f"发送群消息被平台拒绝: group={group_openid}, error={e}")
            else:
                logger.error(f"发送群消息失败: group={group_openid}, error={e}")
            return False

    async def send_group_media(self, group_openid: str, image_url: str, content: str = "",
                               keyboard: Optional[dict] = None) -> bool:
        """
        向单个群发送图片富媒体消息

        Args:
            group_openid: 群openid
            image_url: 图片URL（png/jpg）
            content: 附带文本
            keyboard: 按钮keyboard结构（可选；发送失败自动去按钮重发）

        Returns:
            bool: 是否发送成功（失败时调用方可回退文本）
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，富媒体消息发送失败")
            return False
        client = self._client

        async def _send(kb):
            media = await client.api.post_group_file(
                group_openid=group_openid,
                file_type=1,
                url=image_url,
            )
            await client.api.post_group_message(
                group_openid=group_openid,
                msg_type=7,
                content=content or "",
                media=media,
                keyboard=kb,
            )

        try:
            try:
                await _send(keyboard)
            except Exception as e:
                if keyboard is None:
                    raise
                logger.warning(f"带按钮富媒体发送失败，去按钮重发: {e}")
                await _send(None)
            return True
        except Exception as e:
            if self._note_send_denied(group_openid, e):
                logger.debug(f"富媒体发送被平台拒绝: group={group_openid}, error={e}")
            else:
                logger.warning(f"发送富媒体消息失败: group={group_openid}, error={e}")
            return False

    async def _broadcast_message(self, groups: Iterable[str], content: str,
                                 media: Optional[str] = None,
                                 keyboard: Optional[dict] = None,
                                 at_all_groups: Optional[Iterable[str]] = None) -> bool:
        """
        消息推送降级链（单群）：Markdown(原生嵌图/按钮) → 文本

        所有推送统一Markdown优先——原生markdown支持图片语法（免模板、
        群聊对所有机器人开放），封面/图片直接嵌入正文，按钮仅Markdown下
        渲染；Markdown失败（无权限/接口异常）降级纯文本（图片以URL保留）。

        at_all_groups中的群在正文前嵌入 <qqbot-at-everyone />（@全部成员，
        文本/图文/markdown消息均支持解析；平台每群每日1次配额由调用方判定）。

        艾特全体被平台拒绝（常见：机器人无群管理员权限）时自动去标签降级
        重发普通消息（消息必达优先于艾特），该群记入本会话不支持集合不再尝试。
        """
        from .push import live_markdown_content

        # 注：@全体配额由平台按群每日1次硬性限制——正文即便被注入
        # <qqbot-at-everyone />也只会消耗该群当日平台配额，无需程序侧防护
        at_all = set(at_all_groups or [])
        success = True

        async def _send_pair(group: str, body: str) -> bool:
            """单群发送链：Markdown → 文本降级"""
            sent = await self.send_group_markdown(
                group, live_markdown_content(body, media), keyboard=keyboard)
            if not sent:
                text = body + f"\n图片：{media}" if media else body
                sent = await self.send_group_message(group, text)
            return sent

        for group in groups:
            group_content = content
            use_at_all = group in at_all and group not in self._at_all_unsupported
            if group in at_all and group in self._at_all_unsupported:
                logger.info(f"群{group}本会话已确认不支持艾特全体，跳过艾特")
            if use_at_all:
                group_content = f"<qqbot-at-everyone /> {content}"
            sent = await _send_pair(group, group_content)
            if not sent and use_at_all:
                # 艾特全体被拒（无群管理员权限等）→ 去标签降级重发，保证消息必达；
                # 艾特未送达，退还该群当日配额（发送失败不计入）
                logger.warning(
                    f"群{group} 艾特全体发送失败（机器人可能无群管理员权限），"
                    f"降级为普通消息重发")
                self._at_all_unsupported.add(group)
                await self._refund_at_all(group)
                sent = await _send_pair(group, content)
            if not sent:
                success = False
            await asyncio.sleep(0.5)  # 避免发送过快
        return success

    async def _send_worker(self) -> None:
        """发送工作协程：从有界队列取任务串行执行（None为stop哨兵）"""
        assert self._send_queue is not None
        while True:
            coro = await self._send_queue.get()
            try:
                if coro is None:
                    return  # stop哨兵：退出worker
                await coro
            except asyncio.CancelledError:
                coro.close()
                raise
            except Exception as e:
                logger.error(f"推送任务执行失败: {e}")
            finally:
                self._send_queue.task_done()

    def _enqueue_send(self, coro) -> bool:
        """把推送协程放入有界队列（须在主循环内调用）；满则丢弃；stop收尾窗口拒绝"""
        queue = self._send_queue
        if queue is None:
            # stop()先置空队列后取消client任务的窗口期：直接丢弃防AssertionError
            coro.close()
            return False
        try:
            queue.put_nowait(coro)
            return True
        except asyncio.QueueFull:
            coro.close()
            logger.error(
                f"发送队列已满（{self.SEND_QUEUE_SIZE}），丢弃一条推送——"
                f"消费速率不足，检查发送失败日志")
            return False

    def push(self, content: str, groups: Iterable[str],
             keyboard: Optional[dict] = None,
             media: Optional[str] = None,
             at_all_groups: Optional[Iterable[str]] = None) -> bool:
        """
        线程安全的统一推送入口（同步，立即返回）

        Markdown(嵌图/按钮)优先 → 富媒体 → 文本。
        经有界队列由worker消费，队列满时丢弃并告警（防任务无限堆积）。

        Args:
            content: 文本内容
            groups: 群openid列表
            keyboard: 按钮keyboard结构（可选，仅开播推送传入）
            media: 图片URL（可选，Markdown嵌图/富媒体共用）
            at_all_groups: 需要艾特全体的群（可选，正文嵌入<qqbot-at-everyone />；
                每群每日配额由调用方——消费者——判定）

        Returns:
            bool: 是否成功入队（跨线程调用时恒True，丢弃仅记日志）
        """
        if self._loop is None or not self.started:
            logger.error("QQ官方机器人未启动，消息发送失败")
            return False

        coro = self._broadcast_message(groups, content, media=media, keyboard=keyboard,
                                       at_all_groups=at_all_groups)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is self._loop:
            return self._enqueue_send(coro)

        async def _threadsafe_enqueue() -> bool:
            return self._enqueue_send(coro)

        # 跨线程：调度入队到主循环（非阻塞，无法同步取回结果）
        asyncio.run_coroutine_threadsafe(_threadsafe_enqueue(), self._loop)
        return True

    def push_media(self, image_url: str, content: str, groups: Iterable[str],
                   keyboard: Optional[dict] = None,
                   at_all_groups: Optional[Iterable[str]] = None) -> bool:
        """富媒体推送兼容入口（等同push携带media）"""
        return self.push(content, groups, keyboard=keyboard, media=image_url,
                         at_all_groups=at_all_groups)

    def broadcast(self, groups: Iterable[str], content: str,
                  keyboard: Optional[dict] = None,
                  at_all_groups: Optional[Iterable[str]] = None) -> bool:
        """文本广播兼容入口（内部走markdown优先降级链）"""
        return self.push(content, groups, keyboard=keyboard, at_all_groups=at_all_groups)

    async def send_group_markdown(self, group_openid: str, content: str,
                                  keyboard: Optional[dict] = None) -> bool:
        """
        向单个群发送Markdown消息（msg_type=2；按钮仅在此消息类型下渲染）

        注意：群内Markdown需平台单独授权（原生Markdown/模板权限），
        无权限时接口报错，调用方应自行降级富媒体/文本。

        Args:
            group_openid: 群openid
            content: Markdown内容（文档要求markdown字段填写后content为空）
            keyboard: 按钮keyboard结构（可选）

        Returns:
            bool: 是否发送成功
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，Markdown消息发送失败")
            return False
        try:
            await self._client.api.post_group_message(
                group_openid=group_openid,
                msg_type=2,
                content="",
                markdown={"content": content},
                keyboard=keyboard,
            )
            return True
        except Exception as e:
            logger.warning(f"发送Markdown消息失败: group={group_openid}, error={e}")
            return False

    async def get_group_info(self, group_openid: str) -> Optional[dict]:
        """
        获取群基本信息（GET /v2/groups/{group_openid}/info，白名单接口）

        Args:
            group_openid: 群openid

        Returns:
            Optional[dict]: {group_name, group_finger_memo(简介),
            group_class_text(分类), group_tags(标签数组),
            group_member_num(成员数)}，失败None
        """
        if not self.started or self._client is None:
            return None
        try:
            return await self._client.api.get_group_info(group_openid)
        except Exception as e:
            logger.warning(f"获取群信息失败: group={group_openid}, error={e}")
            return None

    async def send_c2c_message(self, openid: str, content: str,
                               msg_id: Optional[str] = None,
                               msg_seq: int = 1) -> bool:
        """
        向单个用户发送私聊文本消息

        Args:
            openid: 用户openid
            content: 文本内容
            msg_id: 回复的消息ID（被动回复，不受主动消息配额限制）
            msg_seq: 回复序号（同一msg_id下需递增，重复组合会被平台拒绝）

        Returns:
            bool: 是否发送成功
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，私聊消息发送失败")
            return False

        try:
            await self._client.api.post_c2c_message(
                openid=openid,
                msg_type=0,
                content=content,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
            return True
        except Exception as e:
            logger.error(f"发送私聊消息失败: user={openid}, error={e}")
            return False

    async def send_c2c_image(
        self,
        openid: str,
        image_bytes: bytes,
        msg_id: Optional[str] = None,
        filename: str = "image.png",
        msg_seq: int = 1,
    ) -> bool:
        """
        向单个用户发送私聊图片消息（本地图片经multipart直传）

        说明：官方文档的富媒体上传以URL/分片两种方式为主，multipart
        本地直传（file_data字段）为平台支持的兼容路径，用于本地生成的
        图片（如登录二维码）；上传返回file_info后再以msg_type=7发送。

        Args:
            openid: 用户openid
            image_bytes: 图片二进制数据（png/jpg）
            msg_id: 回复的消息ID（被动回复）
            filename: 文件名
            msg_seq: 回复序号

        Returns:
            bool: 是否发送成功
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，私聊图片发送失败")
            return False

        try:
            media = await self._upload_image_file(openid, image_bytes, filename)
            if media is None:
                logger.error(f"私聊图片上传失败: user={openid}")
                return False

            await self._client.api.post_c2c_message(
                openid=openid,
                msg_type=7,
                content="",
                media=media,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
            return True
        except Exception as e:
            logger.error(f"发送私聊图片失败: user={openid}, error={e}")
            return False

    async def _upload_image_file(self, openid: str, image_bytes: bytes,
                                 filename: str):
        """
        按官方文档分片上传本地图片，返回files接口的media（file_info）

        流程：upload_prepare（MD5/SHA1校验）→ 逐片PUT预签名URL →
        upload_part_finish逐片确认 → files合并返回media。

        Args:
            openid: 用户openid
            image_bytes: 图片二进制数据
            filename: 文件名

        Returns:
            media对象（file_info），失败返回None
        """
        if self._client is None:
            return None
        api = self._client.api
        http = api._http
        file_size = len(image_bytes)
        md5 = hashlib.md5(image_bytes).hexdigest()
        sha1 = hashlib.sha1(image_bytes).hexdigest()
        # 文档要求：文件前10002432字节（约10MB）的MD5；不足10MB时即整个文件
        md5_10m = hashlib.md5(image_bytes[:10002432]).hexdigest()

        prepare = await http.request(
            Route("POST", "/v2/users/{user_id}/upload_prepare", user_id=openid),
            json={
                "file_type": 1,
                "file_size": str(file_size),
                "file_name": filename,
                "md5": md5,
                "sha1": sha1,
                "md5_10m": md5_10m,
            },
        )
        logger.debug(f"upload_prepare响应: {prepare}")
        upload_id = (prepare or {}).get("upload_id")
        parts = (prepare or {}).get("parts") or []
        if not upload_id or not parts:
            logger.error(f"upload_prepare未返回上传任务: {prepare}")
            return None

        # 逐片PUT到预签名URL（COS）
        import aiohttp as _aiohttp
        async with _aiohttp.ClientSession() as put_session:
            for part in parts:
                index = int(part.get("index", 0))
                presigned = part.get("presigned_url") or ""
                block = int(part.get("block_size") or 0) or 5 * 1024 * 1024
                chunk = image_bytes[index * block:(index + 1) * block]
                chunk_md5 = hashlib.md5(chunk).hexdigest()
                async with put_session.put(presigned, data=chunk) as put_resp:
                    put_body = await put_resp.text()
                    if put_resp.status not in (200, 201, 206):
                        logger.error(f"分片PUT失败 index={index}: "
                                     f"{put_resp.status} {put_body[:200]}")
                        return None
                    logger.debug(f"分片PUT成功 index={index}: {put_body[:120]}")
                # 逐片确认
                await http.request(
                    Route("POST", "/v2/users/{user_id}/upload_part_finish",
                          user_id=openid),
                    json={
                        "upload_id": upload_id,
                        "part_index": index,
                        "block_size": str(len(chunk)),
                        "md5": chunk_md5,
                    },
                )

        # 合并：携带upload_id调用files接口
        media = await http.request(
            Route("POST", "/v2/users/{openid}/files", openid=openid),
            json={
                "file_type": 1,
                "upload_id": upload_id,
                "file_name": filename,
                "srv_send_msg": False,
            },
        )
        logger.debug(f"files合并响应: {media}")
        return media

    async def send_group_image(
        self,
        group_openid: str,
        image_bytes: bytes,
        msg_id: Optional[str] = None,
        msg_seq: int = 1,
        filename: str = "image.png",
    ) -> bool:
        """
        向单个群发送本地图片（multipart直传，与单聊files接口对称）

        Args:
            group_openid: 群openid
            image_bytes: 图片二进制数据
            msg_id: 回复的消息ID
            msg_seq: 回复序号
            filename: 文件名

        Returns:
            bool: 是否发送成功
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，群图片发送失败")
            return False

        try:
            form = aiohttp.FormData()
            form.add_field("file_type", "1")
            form.add_field("srv_send_msg", "false")
            if msg_id:
                form.add_field("msg_id", msg_id)
                form.add_field("msg_seq", str(msg_seq))
            form.add_field(
                "file_data", image_bytes, filename=filename,
                content_type="image/png" if filename.endswith(".png") else "image/jpeg",
            )
            route = Route("POST", "/v2/groups/{group_openid}/files",
                          group_openid=group_openid)
            media = await self._client.api._http.request(route, data=form)

            await self._client.api.post_group_message(
                group_openid=group_openid,
                msg_type=7,
                content="",
                media=media,
                msg_id=msg_id,
                msg_seq=msg_seq,
            )
            return True
        except Exception as e:
            logger.error(f"发送群图片失败: group={group_openid}, error={e}")
            return False

    async def send_group_media_data(self, group_openid: str, data: bytes,
                                    file_name: str = "dynamic.png",
                                    content: str = "") -> bool:
        """向单个群发送本地图片（二进制直传：file_data，≤5M）。

        官方v2群files接口支持二进制上传（file_data字段，base64编码入JSON），
        分片合并用于更大文件——动态推送的本地渲染卡片/截图（通常<1M）走直传。
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，本地图片发送失败")
            return False
        try:
            import base64
            route = Route(
                "POST", "/v2/groups/{group_openid}/files",
                group_openid=group_openid)
            res = await self._client.api._http.request(route, json={
                "file_type": 1,  # 图片
                "file_name": file_name,
                "file_data": base64.b64encode(data).decode("ascii"),
                "srv_send_msg": False,  # 仅取file_info，随后手动发消息
            })
            file_info = res.get("file_info") if isinstance(res, dict) else None
            if not file_info:
                logger.warning(f"群图片二进制上传未返回file_info: {res}")
                return False
            await self._client.api.post_group_message(
                group_openid=group_openid,
                msg_type=7,
                content=content,
                media={"file_info": file_info},
            )
            return True
        except Exception as e:
            logger.warning(f"发送群本地图片失败: group={group_openid}, error={e}")
            return False

    def push_media_data(self, data: bytes, content: str,
                        groups: Iterable[str],
                        file_name: str = "dynamic.png") -> bool:
        """多群推送本地图片（二进制直传；单群失败降级Markdown→文本）"""
        if self._loop is None or not self.started:
            logger.error("QQ官方机器人未启动，本地图片推送失败")
            return False

        async def _run() -> bool:
            from .push import live_markdown_content
            all_ok = True
            for group in groups:
                ok = await self.send_group_media_data(
                    group, data, file_name=file_name, content=content)
                if not ok:
                    # 降级：Markdown → 文本（图已不可达，纯文字保必达）
                    ok = await self.send_group_markdown(group, content)
                    if not ok:
                        ok = await self.send_group_message(group, content)
                if not ok:
                    all_ok = False
                await asyncio.sleep(0.5)
            return all_ok

        coro = _run()
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is self._loop:
            return self._enqueue_send(coro)
        asyncio.run_coroutine_threadsafe(coro, self._loop)
        return True

    async def notify_admins(self, admins: Iterable[str], content: str) -> int:
        """
        向全部管理员发送私聊提醒（Cookie失效等敏感信息仅走私聊）

        Args:
            admins: 管理员openid列表
            content: 文本内容

        Returns:
            int: 成功发送的管理员数量
        """
        if isinstance(admins, str):
            admins = [admins]  # 防御：误传单个openid字符串
        sent = 0
        for openid in admins:
            if await self.send_c2c_message(openid, content):
                sent += 1
            await asyncio.sleep(0.5)
        return sent
