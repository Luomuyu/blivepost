# -*- coding: utf-8 -*-
"""
QQ群命令处理器

处理用户在群里@机器人发送的指令：
  /绑定 ROOMID      绑定当前群与直播间（记录主播uid）
  /监控 ROOMID      开始监控并推送该直播间（每房间仅一个监控任务）
  /取消监控 ROOMID   取消监控推送该直播间
  /解绑 ROOMID      解除当前群与直播间的绑定
"""

import re

from ..utils.contact import extract_sender
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from botpy.message import GroupMessage

    from .bindings import BindingStore
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


logger = get_logger("commands")

# 指令格式：/命令 房间号（斜杠可省略，兼容全角斜杠与中文冒号；
# 用search而非match，兼容content中带@机器人前缀的情况）
_CMD_RE = re.compile(
    r"[/／]\s*(取消动态|绑定动态|取消监控|解绑|绑定|监控|sc榜|收入)\s*[:：\s]\s*(\d{2,})"
    r"(?:\s+((?:\d{8})|(?:\d{4}-\d{2}-\d{2})|[周月]))?\s*$",
    re.IGNORECASE,
)
# 无参数指令
_NOARG_RE = re.compile(r"[/／]\s*(帮助|指令|菜单|状态|绑定列表|汇总)(?:\s+(周|月))?\s*$")


def _parse_date(text: str):
    """解析YYYYMMDD或YYYY-MM-DD格式的日期，非法返回None"""
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


HELP_TEXT = (
    "支持的指令：\n"
    "/监控 房间号 - 监控该直播间并推送到本群\n"
    "/取消监控 房间号 - 取消向本群的推送\n"
    "提示：直播间的添加/删除请在Web面板操作"
)


class CommandHandler:
    """QQ群命令处理器"""

    def __init__(
        self,
        binding_store: "BindingStore",
        monitor_manager: "MonitorManager",
        db_service: Optional["DatabaseService"] = None,
        group_admin_openids: Optional[list] = None,
        dynamic_manager=None
    ):
        """
        初始化命令处理器

        Args:
            binding_store: 群绑定存储
            monitor_manager: 多房间监控管理器
            db_service: 数据库服务（统计查询用，可选）
        """
        self.binding_store = binding_store
        self.monitor_manager = monitor_manager
        self.db_service = db_service
        # 群控制指令名单（管理员=Admin.openids ∪ Admin.main_openids，外部合并传入）；空=不限制
        self.group_admin_openids = [str(o) for o in (group_admin_openids or [])]
        self.dynamic_manager = dynamic_manager

    async def handle(self, message: "GroupMessage") -> Optional[str]:
        """
        处理群@消息

        Args:
            message: botpy群消息

        Returns:
            Optional[str]: 回复内容，非命令消息返回None
        """
        content = (message.content or "").strip()
        group_openid = message.group_openid

        # 无参数指令
        noarg = _NOARG_RE.search(content)
        if noarg is not None:
            cmd = noarg.group(1)
            logger.info(f"收到群命令: {cmd} (group={group_openid})")
            if cmd in ("帮助", "指令", "菜单"):
                return HELP_TEXT
            elif cmd == "汇总":
                return await self._summary(group_openid,
                                           {"周": 7, "月": 30}.get(noarg.group(2)))
            elif cmd == "状态":
                return await self._status(group_openid)
            else:  # 绑定列表
                return await self._my_bindings(group_openid)

        # 带房间号指令
        match = _CMD_RE.search(content)

        if match is None:
            return None

        cmd, room_str = match.group(1), match.group(2)
        room_id = int(room_str)
        query_day = _parse_date(match.group(3)) if match.group(3) else None
        logger.info(f"收到群命令: {cmd} {room_id} (group={group_openid})")

        sender = extract_sender(message.author)
        control_cmds = ("监控", "取消监控", "绑定动态", "取消动态")
        if cmd in control_cmds and not self._group_allowed(sender):
            return (
                "无权限：仅机器人管理员可执行控制指令（查询指令不受限）\n"
                f"如你是管理员，请让机器人所有者将此openid加入配置Admin.openids：{sender or '未知'}"
            )

        try:
            if cmd == "绑定动态":
                return await self._bind_dynamic(group_openid, room_id)
            elif cmd == "取消动态":
                return await self._unbind_dynamic(group_openid, room_id)
            elif cmd == "收入":
                range_days = {"周": 7, "月": 30}.get(match.group(3))
                return await self._income(group_openid, room_id, query_day, range_days)
            elif cmd.lower() == "sc榜":
                return await self._sc_board(group_openid, room_id, day=query_day)
            elif cmd == "监控":
                return await self._monitor(group_openid, room_id)
            else:  # 取消监控
                return await self._unmonitor(group_openid, room_id)
        except Exception as e:
            logger.error(f"执行命令 {cmd} {room_id} 失败: {e}")
            return f"命令执行失败：{e}"

    def _group_allowed(self, sender: str) -> bool:
        """群控制指令权限：管理员名单未配置时不限制；无法识别发送者且已配置名单时拒绝"""
        if not self.group_admin_openids:
            return True
        return bool(sender) and sender in self.group_admin_openids

    async def _save_binding_snapshot(self, real_id: int, room_info) -> None:
        """绑定信息入库：get_status_info_by_uids全量快照写streamer/live_room两表；
        快照失败时退回get_room_info已知字段，均不阻断绑定流程"""
        if self.db_service is None or not room_info.uid:
            return
        import asyncio as _asyncio

        from .bilibili import BilibiliAPI
        try:
            status = await _asyncio.to_thread(
                BilibiliAPI().get_status_info_by_uids, [room_info.uid])
            await self.db_service.apply_status_info(status)
            return
        except Exception as e:
            logger.warning(f"绑定快照批量查询失败(房间{real_id})，退回已知字段: {e}")
        try:
            await self.db_service.save_streamer_info(
                room_info.uid, uname=room_info.name, room_id=real_id)
            await self.db_service.save_live_room_info(
                real_id, uid=room_info.uid, title=room_info.title,
                cover=room_info.cover)
        except Exception as e:
            logger.warning(f"绑定信息入库失败(房间{real_id}): {e}")

    async def _monitor(self, group_openid: str, room_id: int) -> str:
        """
        监控直播间：登记房间（直播间注册表）并添加本群为推送目标，
        启动监控子进程（房间号归一化为真实房间号）
        """
        room_info = await self.monitor_manager.get_room_info(room_id)
        real_id = int(room_info.room_id) or room_id
        await self._save_binding_snapshot(real_id, room_info)
        await self.binding_store.bind(group_openid, real_id, room_info.uid)
        await self.binding_store.set_monitored(group_openid, real_id, True)
        await self.monitor_manager.ensure(real_id)

        reply = f"已开始监控直播间{real_id}（主播：{room_info.name}），推送将发送到本群"
        if real_id != room_id:
            reply += f"\n（房间号已归一化，输入的{room_id}为短号）"
        return reply

    async def _unmonitor(self, group_openid: str, room_id: int) -> str:
        """取消推送：移除本群推送目标（无其他推送群时停止监控子进程）"""
        room_info = await self.monitor_manager.get_room_info(room_id)
        real_id = int(room_info.room_id) or room_id

        await self.binding_store.unbind(group_openid, real_id)
        extra = ""
        if not self.binding_store.get_monitored_groups(real_id):
            stopped = await self.monitor_manager.remove(real_id)
            if stopped:
                extra = "\n该直播间已无推送群，监控已停止"
        return f"已取消监控直播间{real_id}，不再向本群推送{extra}"

    async def _status(self, group_openid: str) -> str:
        """查询本群监控中的直播间及其监控任务状态"""
        monitored = [
            b for b in self.binding_store.get_group_bindings(group_openid)
            if b.monitored
        ]
        if not monitored:
            return "本群当前没有监控中的直播间，发送 /帮助 查看指令"

        lines = ["本群监控中的直播间："]
        for b in monitored:
            state = "监控运行中" if self.monitor_manager.has(b.room_id) else "监控未运行（等待恢复）"
            lines.append(f"· 房间{b.room_id}（主播UID:{b.uid}）- {state}")
        lines.append(f"\n全局监控房间数：{len(self.monitor_manager.room_ids())}")
        return "\n".join(lines)

    async def _my_bindings(self, group_openid: str) -> str:
        """查询本群推送的直播间列表"""
        bindings = self.binding_store.get_group_bindings(group_openid)
        if not bindings:
            return "本群当前没有推送中的直播间，发送 /帮助 查看指令"

        lines = ["本群推送的直播间："]
        for b in bindings:
            mon = "监控中" if b.monitored else "未监控"
            lines.append(f"· 房间{b.room_id}（主播UID:{b.uid}）- {mon}")
        return "\n".join(lines)

    @staticmethod
    def help_text() -> str:
        """帮助文本"""
        return HELP_TEXT

    async def _bind_dynamic(self, group_openid: str, up_uid: int) -> str:
        """绑定群与UP主动态（启动轮询进程）"""
        if self.dynamic_manager is None or not self.dynamic_manager.enabled:
            return "动态监控未启用，请联系机器人所有者在配置中开启（Setting.dynamic_monitor）"

        is_new = await self.binding_store.bind_dynamic(group_openid, up_uid)
        created, _ = await self.dynamic_manager.ensure(up_uid)
        name = self.binding_store.get_dynamic_up_name(up_uid)
        name_text = f"（{name}）" if name else ""
        if is_new:
            return f"已订阅UP主{up_uid}{name_text}的动态，更新将推送到本群"
        return f"UP主{up_uid}{name_text}的动态已在订阅中，本群推送已开启"

    async def _unbind_dynamic(self, group_openid: str, up_uid: int) -> str:
        """解除动态订阅（最后一个订阅群取消后停止轮询进程）"""
        if self.dynamic_manager is None:
            return "动态监控功能不可用"

        removed = await self.binding_store.unbind_dynamic(group_openid, up_uid)
        if not removed:
            return f"本群未订阅UP主{up_uid}的动态"

        extra = ""
        if not self.binding_store.get_dynamic_groups(up_uid):
            stopped = await self.dynamic_manager.remove(up_uid)
            if stopped:
                extra = "\n该UP主已无订阅群，动态轮询已停止"
        return f"已取消订阅UP主{up_uid}的动态{extra}"

    async def _summary(self, group_openid: str, range_days: Optional[int] = None) -> str:
        """跨房间汇总报表（当前所有监控中的房间合计）"""
        if self.db_service is None:
            return "统计功能不可用（数据库未连接）"

        room_ids = self.binding_store.get_monitored_room_ids()
        if not room_ids:
            return "当前没有监控中的房间"

        if range_days is not None:
            end = datetime.now()
            start = end - timedelta(days=range_days)
            s = await self.db_service.get_daily_summary(
                room_ids, _window=(start, end))
            scope = f"近{range_days}天"
        else:
            s = await self.db_service.get_daily_summary(room_ids)
            scope = "今日"
        return (
            f"全部监控房间{scope}汇总（共{len(room_ids)}个房间）：\n"
            f"· 弹幕：{s['danmaku_count']}条\n"
            f"· 礼物：{s['gift_gold']}元（{s['gift_count']}次）\n"
            f"· 上舰：{s['ship_total']}元（{s['ship_count']}次）\n"
            f"· SC：{s['sc_total']}元（{s['sc_count']}次）\n"
            f"· 红包：{s['red_pocket']}电池"
        )

    async def _resolve_query_room(self, room_id: int):
        """
        解析查询命令的目标房间（归一化短号）

        Returns:
            Optional[int]: 真实房间号；房间不存在返回None
        """
        try:
            room_info = await self.monitor_manager.get_room_info(room_id)
        except Exception:
            return None
        return int(room_info.room_id) or room_id

    async def _income(self, group_openid: str, room_id: int,
                      day=None, range_days: Optional[int] = None) -> str:
        """查询直播间收入汇总（默认今日；可指定日期，或"周"近7天/"月"近30天）"""
        if self.db_service is None:
            return "统计功能不可用（数据库未连接）"

        rid = await self._resolve_query_room(room_id)
        if rid is None:
            return f"直播间{room_id}不存在或查询失败"

        if range_days is not None:
            end = datetime.now()
            start = end - timedelta(days=range_days)
            s = await self.db_service.get_summary_range(rid, start, end)
        else:
            s = await self.db_service.get_daily_summary(rid, day=day)
        return (
            f"直播间{rid} {s['day']} 统计：\n"
            f"· 弹幕：{s['danmaku_count']}条\n"
            f"· 礼物：{s['gift_gold']}元（{s['gift_count']}次）\n"
            f"· 上舰：{s['ship_total']}元（{s['ship_count']}次）\n"
            f"· SC：{s['sc_total']}元（{s['sc_count']}次）\n"
            f"· 红包：{s['red_pocket']}电池"
        )

    async def _sc_board(self, group_openid: str, room_id: int,
                        limit: int = 5, day=None) -> str:
        """查询直播间SC排行（默认今日，可指定日期YYYYMMDD或YYYY-MM-DD）"""
        if self.db_service is None:
            return "统计功能不可用（数据库未连接）"

        rid = await self._resolve_query_room(room_id)
        if rid is None:
            return f"直播间{room_id}不存在或查询失败"

        rows = await self.db_service.get_top_super_chat(rid, day=day, limit=limit)
        if not rows:
            return f"直播间{rid}当日暂无SuperChat记录"

        lines = [f"直播间{rid}SC排行（{day.strftime('%Y-%m-%d') if day else '今日'}）："]
        for i, (uname, price, message) in enumerate(rows, 1):
            preview = message if len(message) <= 20 else message[:20] + "…"
            lines.append(f"{i}. {uname} ¥{price}：{preview}")
        return "\n".join(lines)
