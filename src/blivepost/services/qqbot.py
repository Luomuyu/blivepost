# -*- coding: utf-8 -*-
"""
QQ官方机器人推送客户端（基于botpy）
"""

import asyncio
import hashlib
import os
from typing import Any, Iterable, Optional, TYPE_CHECKING

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
    ):
        log_kwargs = {"log_config": log_config} if log_config else {}
        super().__init__(intents=intents, ext_handlers=ext_handlers, **log_kwargs)
        self._command_handler = command_handler
        self._c2c_handler = c2c_handler
        self._contact_handler = contact_handler

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

    消息发送入口push()是线程安全的：在事件循环内直接创建任务，
    在其他线程（如动态监控线程）中调度到主事件循环。
    """

    def __init__(self, app_id: str, app_secret: str):
        """
        初始化QQ官方机器人客户端

        Args:
            app_id: 机器人AppID
            app_secret: 机器人AppSecret
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self._me_cache: Optional[dict] = None
        self._client: Optional[_PushClient] = None
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def started(self) -> bool:
        """客户端是否正在运行"""
        return self._task is not None and not self._task.done()

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
            self._me_cache = {
                "id": getattr(me, "id", "") or "",
                "username": getattr(me, "username", "") or "",
                "avatar": getattr(me, "avatar", "") or "",
                "share_url": getattr(me, "share_url", "") or "",
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
        """停止botpy客户端（含start循环派生的ws/心跳/事件子任务）"""
        # 先取消start任务：botpy的start循环经ensure_future派生ws子任务且不保留
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

    async def send_group_message(self, group_openid: str, content: str) -> bool:
        """
        向单个群发送文本主动消息

        Args:
            group_openid: 群openid
            content: 文本内容

        Returns:
            bool: 是否发送成功
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，消息发送失败")
            return False

        try:
            await self._client.api.post_group_message(
                group_openid=group_openid,
                msg_type=0,
                content=content,
            )
            return True
        except Exception as e:
            logger.error(f"发送群消息失败: group={group_openid}, error={e}")
            return False

    async def send_group_media(self, group_openid: str, image_url: str, content: str = "") -> bool:
        """
        向单个群发送图片富媒体消息

        Args:
            group_openid: 群openid
            image_url: 图片URL（png/jpg）
            content: 附带文本

        Returns:
            bool: 是否发送成功（失败时调用方可回退文本）
        """
        if not self.started or self._client is None:
            logger.warning("botpy客户端未运行，富媒体消息发送失败")
            return False

        try:
            media = await self._client.api.post_group_file(
                group_openid=group_openid,
                file_type=1,
                url=image_url,
            )
            await self._client.api.post_group_message(
                group_openid=group_openid,
                msg_type=7,
                content=content or "",
                media=media,
            )
            return True
        except Exception as e:
            logger.warning(f"发送富媒体消息失败: group={group_openid}, error={e}")
            return False

    async def broadcast(self, groups: Iterable[str], content: str) -> bool:
        """
        向所有目标群发送同一消息

        Args:
            groups: 群openid列表
            content: 文本内容

        Returns:
            bool: 是否全部发送成功
        """
        success = True
        for group in groups:
            if not await self.send_group_message(group, content):
                success = False
            await asyncio.sleep(0.5)  # 避免发送过快
        return success

    async def _broadcast_media(self, groups: Iterable[str], image_url: str, content: str) -> bool:
        """
        向所有目标群发送富媒体消息（单群失败回退为文本）

        Args:
            groups: 群openid列表
            image_url: 图片URL
            content: 文本内容

        Returns:
            bool: 是否全部发送成功
        """
        success = True
        for group in groups:
            sent = await self.send_group_media(group, image_url, content)
            if not sent:
                sent = await self.send_group_message(group, content)
            if not sent:
                success = False
            await asyncio.sleep(0.5)  # 避免发送过快
        return success

    def push(self, content: str, groups: Iterable[str]) -> bool:
        """
        线程安全的推送入口（同步，立即返回）

        Args:
            content: 文本内容
            groups: 群openid列表

        Returns:
            bool: 是否成功调度发送任务
        """
        if self._loop is None or not self.started:
            logger.error("QQ官方机器人未启动，消息发送失败")
            return False

        coro = self.broadcast(groups, content)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is self._loop:
            asyncio.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        return True

    def push_media(self, image_url: str, content: str, groups: Iterable[str]) -> bool:
        """
        线程安全的富媒体推送入口（同步，立即返回；单群失败自动回退文本）

        Args:
            image_url: 图片URL
            content: 文本内容
            groups: 群openid列表

        Returns:
            bool: 是否成功调度发送任务
        """
        if self._loop is None or not self.started:
            logger.error("QQ官方机器人未启动，富媒体消息发送失败")
            return False

        coro = self._broadcast_media(groups, image_url, content)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None

        if current_loop is self._loop:
            asyncio.create_task(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        return True

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
