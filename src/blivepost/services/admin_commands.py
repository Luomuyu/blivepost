# -*- coding: utf-8 -*-
"""
管理员私聊命令处理器

处理用户私聊机器人发送的指令：
  /登录 - 管理员触发B站扫码登录，二维码图片私聊回复，成功后cookie写入账号池；
          普通用户回复无权限。
"""

import asyncio
import re
from typing import List, Optional, TYPE_CHECKING

from ..utils.contact import extract_sender
from ..utils.logger import get_logger
from ..utils.timeutil import ms_to_str, now_local, now_ms
from .bili_login import BiliLoginService, LoginResult, verify_nav
from .cookie_pool import CookiePool


if TYPE_CHECKING:
    from botpy.message import C2CMessage

    from ..database.service import DatabaseService
    from .qqbot import QQBotClient


logger = get_logger("admin_commands")

_LOGIN_RE = re.compile(r"^\s*[/／]?\s*登录\s*$")
_UID_RE = re.compile(r"^\s*[/／]?\s*(移除账号|恢复账号|删除账号)\s*[:：\s]\s*(\d{4,})\s*$")
_EXPORT_RE = re.compile(
    r"^\s*[/／]?\s*导出\s*[:：\s]\s*(\d{2,})(?:\s+((?:\d{8})|(?:\d{4}-\d{2}-\d{2})))?"
    r"(?:\s+(json))?\s*$",
    re.IGNORECASE,
)
_ME_RE = re.compile(r"^\s*[/／]?\s*我\s*$")

HELP_TEXT = (
    "支持的指令：\n"
    "/登录 - 扫码登录B站账号，Cookie写入账号池\n"
    "/账号 - 查看账号池状态\n"
    "/移除账号 UID - 从账号池删除账号\n"
    "/恢复账号 UID - 将失效账号恢复为可用（先验证登录态）\n"
    "/导出 房间号 [日期] [json] - 导出当日明细到exports目录 (默认CSV, 加json为JSON格式)\n"
    "/体积 - 查询数据库体积\n"
    "/我 - 查看你的openid"
)


class AdminCommandHandler:
    """管理员私聊命令处理器"""

    def __init__(
        self,
        cookie_pool: CookiePool,
        admin_openids: List[str],
        bot_client: Optional["QQBotClient"] = None,
        db_service: Optional["DatabaseService"] = None,
        monitor_manager=None,
        main_admin_openids: Optional[List[str]] = None,
    ):
        """
        初始化私聊命令处理器

        Args:
            cookie_pool: Cookie账号池
            admin_openids: 管理员用户openId列表
            bot_client: QQ机器人客户端（发送二维码图片用，可后续设置）
            db_service: 数据库服务（账号删除持久化用）
            monitor_manager: 监控管理器（/账号关联房间展示用）
            main_admin_openids: 主管理员openId列表（与admin_openids共同构成命令权限）
        """
        self.cookie_pool = cookie_pool
        # 私聊命令权限 = Admin.openids ∪ Admin.main_openids
        self.admin_openids = [str(o) for o in admin_openids]
        self.main_admin_openids = [str(o) for o in (main_admin_openids or [])]
        self._command_allowed = list(dict.fromkeys(
            self.admin_openids + self.main_admin_openids))
        self.bot_client = bot_client
        self.db_service = db_service
        self.monitor_manager = monitor_manager
        self._login_task: Optional[asyncio.Task] = None

    def user_level(self, openid: str) -> int:
        """
        计算用户权限级别（权限组逐级包含）

        Returns:
            int: 2=主管理员（main_openids）、1=管理员（openids）、0=默认用户
        """
        if openid in self.main_admin_openids:
            return 2
        if openid in self.admin_openids:
            return 1
        return 0

    def is_admin(self, openid: str) -> bool:
        """判断是否管理员（openids与main_openids名单内均可，级别≥1）"""
        return self.user_level(openid) >= 1

    def is_main_admin(self, openid: str) -> bool:
        """判断是否主管理员（级别≥2；main_openids为空时回落到openids名单）"""
        if self.main_admin_openids:
            return openid in self.main_admin_openids
        return openid in self.admin_openids

    async def handle(self, message: "C2CMessage") -> None:
        """
        处理私聊消息

        Args:
            message: botpy私聊消息
        """
        content = (message.content or "").strip()
        # sender提取：union_openid优先（跨场景一致），回退user_openid
        sender = extract_sender(message.author)
        logger.info(f"收到私聊消息 (user={sender}): {content[:50]}")

        # /我：所有人可用，回复发送者openid（便于收集配置管理员名单）
        if _ME_RE.match(content) is not None:
            await message.reply(content=f"你的openid：{sender or '未获取到'}")
            return

        if not self.is_admin(sender):
            if _LOGIN_RE.match(content) or _UID_RE.search(content) or "/账号" in content:
                await message.reply(content="无权限：仅管理员可执行此操作")
            return

        if _LOGIN_RE.match(content) is not None:
            await self._start_login(message, sender)
            return

        uid_match = _UID_RE.search(content)
        if uid_match is not None:
            cmd, uid = uid_match.group(1), int(uid_match.group(2))
            if cmd in ("移除账号", "删除账号"):
                await message.reply(content=await self._remove_account(uid))
            else:
                await message.reply(content=await self._reactivate_account(uid))
            return

        export_match = _EXPORT_RE.match(content)
        if export_match is not None:
            day = None
            if export_match.group(2):
                for fmt in ("%Y%m%d", "%Y-%m-%d"):
                    try:
                        from datetime import datetime as _dt
                        day = _dt.strptime(export_match.group(2), fmt)
                        break
                    except ValueError:
                        continue
            fmt = (export_match.group(3) or "csv").lower()
            await message.reply(content="正在导出，请稍候...")
            await message.reply(content=await self._export_room(
                int(export_match.group(1)), day, fmt=fmt))
            return

        if re.search(r"[/／]\s*体积\s*$", content):
            await message.reply(content="正在统计，请稍候...")
            await message.reply(content=await self._db_size_report())
            return

        if re.search(r"[/／]\s*账号\s*$", content):
            await message.reply(content=self._pool_status())
            return

    async def _start_login(self, message: "C2CMessage", sender: str) -> None:
        """启动扫码登录流程（同一时间仅允许一个）"""
        if self._login_task is not None and not self._login_task.done():
            await message.reply(content="已有登录流程进行中，请等待完成或超时后再试")
            return

        if self.bot_client is None:
            await message.reply(content="机器人未就绪，请稍后重试")
            return
        bot = self.bot_client

        await message.reply(
            content="正在生成B站登录二维码，请使用B站APP扫码（180秒内有效）..."
        )

        openid = sender
        msg_id = message.id
        bot = self.bot_client

        # 同一msg_id下msg_seq必须递增（重复组合会被平台拒绝）
        seq_counter = {"n": 1}

        def next_seq() -> int:
            seq_counter["n"] += 1
            return seq_counter["n"]

        async def on_qrcode(png: bytes, qrcode_url: str) -> None:
            """二维码回调：图片私聊发给管理员；失败回退为文本链接（电脑打开扫码）"""
            sent = await bot.send_c2c_image(
                openid, png, msg_id=msg_id, filename="bili_qrcode.png", msg_seq=next_seq()
            )
            if not sent:
                await bot.send_c2c_message(
                    openid,
                    "二维码图片发送失败。可在电脑浏览器打开以下链接显示二维码后扫码：\n"
                    + qrcode_url,
                    msg_id=msg_id, msg_seq=next_seq()
                )

        service = BiliLoginService(on_qrcode=on_qrcode, timeout=180)
        self._login_task = asyncio.create_task(
            self._run_login(service, openid, next_seq=next_seq), name="bili-login"
        )

    async def _run_login(self, service: BiliLoginService, openid: str,
                         next_seq=None) -> None:
        """执行登录并回复结果"""
        bot = self.bot_client
        try:
            result = await service.login()
        except Exception as e:
            logger.error(f"扫码登录流程异常: {e}")
            if bot:
                seq = next_seq() if next_seq else 1
                await bot.send_c2c_message(openid, f"登录流程异常：{e}", msg_seq=seq)
            return

        await self._finish_login(result, openid, next_seq=next_seq)

    async def _finish_login(self, result: LoginResult, openid: str,
                            next_seq=None) -> None:
        """登录结果处理：写入账号池并回复管理员"""
        bot = self.bot_client
        if bot is None:
            return
        seq = next_seq() if next_seq else 1
        if result.timeout:
            await bot.send_c2c_message(
                openid, "二维码已超时失效，请重新发送 /登录", msg_seq=seq
            )
            return

        if not result.ok:
            await bot.send_c2c_message(
                openid, f"登录失败：{result.error or '未知错误'}，请重新发送 /登录 重试",
                msg_seq=seq,
            )
            return

        await self.cookie_pool.upsert(
            uid=result.uid,
            nickname=result.nickname,
            cookie_string=result.cookie_string,
            sessdata=result.sessdata,
            bili_jct=result.bili_jct,
            ac_time_value=result.ac_time_value,
            sessdata_expires_at=result.sessdata_expires_at,
        )
        await bot.send_c2c_message(
            openid,
            f"登录成功：{result.nickname}（UID:{result.uid}）\n"
            f"Cookie已写入账号池（共{len(result.cookies)}项）"
        )

    @property
    def busy(self) -> bool:
        """是否有登录流程进行中"""
        return self._login_task is not None and not self._login_task.done()

    async def stop(self) -> None:
        """取消进行中的登录流程（安全关闭时调用，避免任务与playwright资源残留）"""
        if self._login_task is not None and not self._login_task.done():
            self._login_task.cancel()
            try:
                await self._login_task
            except (asyncio.CancelledError, Exception):
                pass
        self._login_task = None

    def _pool_status(self) -> str:
        """账号池状态文本（含关联房间与最近使用时间）"""
        accounts = self.cookie_pool.accounts
        if not accounts:
            return "账号池为空，私聊发送 /登录 添加账号"

        active = [a for a in accounts if a.status == "active"]
        lines = [f"账号池共 {len(accounts)} 个账号（可用 {len(active)}）："]
        now = now_ms()
        for a in accounts:
            if a.sessdata_expires_at is not None:
                days = (a.sessdata_expires_at - now) // 86_400_000
                expire_text = f"约{days}天后过期"
            else:
                expire_text = "过期时间未知"
            state = "可用" if a.status == "active" else f"已失效({a.last_error or '未知原因'})"
            line = f"· {a.uid} {a.nickname} - {state}，{expire_text}"
            # 关联房间（账号亲和映射反查）
            if self.monitor_manager is not None:
                rooms = self.monitor_manager.get_account_rooms(a.uid)
                if rooms:
                    line += "\n  关联房间：" + "、".join(str(r) for r in rooms)
            if a.last_used_at is not None:
                line += f"\n  最近使用：{ms_to_str(a.last_used_at, '%m-%d %H:%M')}"
            lines.append(line)
        return "\n".join(lines)

    async def _remove_account(self, uid: int) -> str:
        """从账号池删除账号（内存+数据库）"""
        removed = self.cookie_pool.remove(uid)
        if removed is None:
            return f"账号 {uid} 不在账号池中"

        if self.db_service is not None:
            await self.db_service.delete_cookie_account(uid)
        active = len(self.cookie_pool.active_accounts())
        return f"账号 {uid}（{removed.nickname}）已从账号池删除\n当前可用账号：{active}个"

    async def _reactivate_account(self, uid: int) -> str:
        """恢复失效账号（先通过nav验证登录态仍有效）"""
        account = self.cookie_pool.get_account(uid)
        if account is None:
            return f"账号 {uid} 不在账号池中"
        if account.status == "active":
            return f"账号 {uid}（{account.nickname}）本来就是可用状态"

        ok, _, uname = await asyncio.to_thread(verify_nav, account.cookie_string)
        if not ok:
            return (
                f"账号 {uid} 的Cookie已无法通过登录态验证，无法恢复。\n"
                f"请使用该账号重新 /登录"
            )

        await self.cookie_pool.reactivate(uid)
        return f"账号 {uid}（{uname or account.nickname}）已恢复为可用"

    async def _export_room(self, room_id: int, day=None, fmt: str = "csv") -> str:
        """导出房间当日明细到exports目录（CSV带BOM可直接Excel打开，或JSON）"""
        if self.db_service is None:
            return "导出功能不可用（数据库未连接）"

        import csv
        from pathlib import Path

        day = day or now_local()
        date_text = day.strftime("%Y%m%d")
        data = await self.db_service.export_day_records(room_id, day=day)

        total = sum(len(v) for v in data.values())
        if total == 0:
            return f"直播间{room_id} {date_text} 无任何记录"

        out_dir = Path("exports")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"room_{room_id}_{date_text}.{fmt}"

        headers = {
            "danmaku": ["时间", "UID", "用户名", "弹幕内容", "类型"],
            "gift": ["时间", "UID", "用户名", "礼物名", "数量", "总价(瓜子)", "瓜子类型"],
            "super_chat": ["时间", "UID", "用户名", "金额(元)", "留言内容"],
            "guard": ["时间", "UID", "用户名", "舰长等级", "礼物名", "单价(瓜子)", "数量"],
            "red_pocket": ["时间", "发送者UID", "发送者名", "总价值(电池)"],
        }

        if fmt == "json":
            import json
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
            return (
                f"导出完成：直播间{room_id} {date_text} 共{total}条记录\n"
                f"文件：{path}"
            )

        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([f"直播间{room_id} {day.strftime('%Y-%m-%d')} 明细导出"])
            for section in ("danmaku", "gift", "super_chat", "guard", "red_pocket"):
                rows = data.get(section, [])
                writer.writerow([])
                writer.writerow([f"== {section} ({len(rows)}条) =="])
                writer.writerow(headers[section])
                writer.writerows(rows)

        total = sum(len(v) for v in data.values())
        return (
            f"导出完成：直播间{room_id} {date_text} 共{total}条记录\n"
            f"文件：{path}"
        )

    async def _db_size_report(self) -> str:
        """数据库体积与各表行数报告（数据治理）"""
        import os as _os

        svc = self.db_service
        path = None
        if svc is not None:
            db_manager = getattr(svc, "db_manager", None)
            path = getattr(db_manager, "db_path", None)
        if not path or not _os.path.exists(path):
            return "体积统计不可用（非SQLite数据库或文件不存在）"

        size_mb = _os.path.getsize(path) / (1024 * 1024)
        assert svc is not None
        rows = await svc.get_table_counts()
        total = sum(n for _, n in rows)
        lines = [f"数据库体积：{size_mb}MB，总记录：{total}条", "各表行数（降序）："]
        for name, count in rows:
            if count:
                lines.append(f"· {name}：{count}条")
        empty = [name for name, count in rows if not count]
        if empty:
            lines.append("· 空表：" + "、".join(empty))
        return "\n".join(lines)
