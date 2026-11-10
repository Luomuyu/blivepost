# -*- coding: utf-8 -*-
"""
QQ群命令处理器

处理用户在群里@机器人发送的指令：
  /监控 ROOMID      开始监控并推送该直播间（每房间仅一个监控任务）
  /取消监控 ROOMID   取消监控推送该直播间
  /推送设置 [...]   查看/修改本群对绑定房间的独立推送开关

（绑定/解绑指令已移除，直播间的添加/删除请在Web面板操作）
"""

import re

from ..utils.contact import extract_sender
from datetime import datetime, timedelta

from ..utils.timeutil import now_local
from typing import Dict, Optional, Tuple, TYPE_CHECKING

from ..database.service_admin import GROUP_PUSH_SWITCHES
from ..utils.logger import get_logger


if TYPE_CHECKING:
    from botpy.message import GroupMessage

    from .bindings import BindingStore
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


logger = get_logger("commands")

# 指令格式：/命令 房间号（斜杠可省略，兼容全角斜杠与中文冒号；
# 用search而非match，兼容content中带@机器人前缀的情况）
# 注意：绑定/解绑指令已移除（Web面板操作），正则不得再捕获，
# 否则无对应分支会落入else被当作"取消监控"反向执行
_CMD_RE = re.compile(
    r"[/／]\s*(取消动态|绑定动态|取消监控|监控|sc榜|收入)\s*[:：\s]\s*(\d{2,10})"
    r"(?:\s+((?:\d{8})|(?:\d{4}-\d{2}-\d{2})|[周月]))?\s*$",
    re.IGNORECASE,
)
# 无参数指令
_NOARG_RE = re.compile(r"[/／]\s*(帮助|指令|菜单|状态|绑定列表|汇总)(?:\s+(周|月))?\s*$")
# 推送设置指令：/推送设置 [房间号 [开关名 [开|关]]]|[房间号 恢复]
# （房间号须为本群已绑定房间；开关名支持中文别名；权限同控制指令）
_PUSHSET_RE = re.compile(
    r"[/／]\s*推送设置(?:\s+(\d{2,})(?:\s+(\S+)(?:\s+(开启?|关闭?))?)?)?\s*$")
# 艾特全体模式指令：/艾特全体 每日|仅 房间号,房间号|关闭|查看
_ATALL_RE = re.compile(
    r"[/／]\s*艾特全体(?:\s+(每日|关闭?|查看|仅\s*[\d,，\s]+))?\s*$")

# 推送开关元数据：内部名 -> (显示名, 概览徽标简写, 可输入别名)；
# 键与GROUP_PUSH_SWITCHES一致（动态监控为全局项，不随房间）
_PUSH_SWITCH_META: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    "anchor": ("天选时刻", "天选", ("天选", "天选时刻")),
    "guard_achievement": ("大航海成就", "航海", ("大航海", "大航海成就")),
    "system_event": ("系统事件", "系统", ("系统事件",)),
    "room_change": ("房间信息", "房变", ("房间信息", "房间变更")),
    "daily_report": ("每日结算", "日报", ("每日结算", "日报")),
    "weekly_report": ("每周结算", "周报", ("每周结算", "周报")),
    "monthly_report": ("每月结算", "月报", ("每月结算", "月报")),
}
_PUSH_SWITCH_ALIASES = {alias: name
                        for name, (_, _, aliases) in _PUSH_SWITCH_META.items()
                        for alias in aliases}


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
    "【查询（所有人可用）】\n"
    "/状态 - 本群监控的直播间与动态订阅\n"
    "/绑定列表 - 本群全部绑定关系\n"
    "/收入 房间号 [日期|周|月] - 查询收入汇总\n"
    "/SC榜 房间号 [日期] - SC金额排行\n"
    "/汇总 [周|月] - 全部房间合计\n"
    "【控制（需管理员）】\n"
    "/监控 房间号 - 监控该直播间并推送到本群\n"
    "/取消监控 房间号 - 取消向本群的推送\n"
    "/绑定动态 房间号或UID - 订阅UP主动态\n"
    "/取消动态 房间号或UID - 取消订阅\n"
    "/推送设置 [房间号] - 查看本群推送开关\n"
    "/艾特全体 查看|每日|仅 房间|关闭 - 本群@全体模式\n"
    "提示：直播间的添加/删除请在Web面板操作"
)

# 命令执行异常时的统一回复（内部异常详情只进日志，不回显群内）
GENERIC_CMD_FAILURE = "命令执行失败，请稍后重试或联系管理员"


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

        # 推送设置指令（查看/修改本群对绑定房间的独立推送开关，权限同控制指令）
        pset = _PUSHSET_RE.search(content)
        if pset is not None:
            room_str, arg, state = pset.group(1), pset.group(2), pset.group(3)
            parts = " ".join(x for x in (room_str, arg, state) if x)
            logger.info(f"收到群命令: 推送设置 {parts} (group={group_openid})")
            denied = self._control_denied(extract_sender(message.author))
            if denied is not None:
                return denied
            room_id = int(room_str) if room_str else None
            try:
                return await self._push_setting(group_openid, room_id, arg, state)
            except Exception as e:
                # 异常详情仅记日志，群内回显通用文案（不泄漏内部错误信息）
                logger.error(f"执行命令 推送设置 {parts} 失败: {e}")
                return GENERIC_CMD_FAILURE

        # 艾特全体模式指令（本群at_all配置，权限同控制指令）
        aam = _ATALL_RE.search(content)
        if aam is not None:
            arg = aam.group(1) or "查看"
            logger.info(f"收到群命令: 艾特全体 {arg} (group={group_openid})")
            denied = self._control_denied(extract_sender(message.author))
            if denied is not None:
                return denied
            try:
                return await self._at_all_mode(group_openid, arg)
            except Exception as e:
                logger.error(f"执行命令 艾特全体 {arg} 失败: {e}")
                return GENERIC_CMD_FAILURE

        # 带房间号指令
        match = _CMD_RE.search(content)

        if match is None:
            return self._suggest_command(content)

        cmd, room_str = match.group(1), match.group(2)
        room_id = int(room_str)
        query_day = _parse_date(match.group(3)) if match.group(3) else None
        logger.info(f"收到群命令: {cmd} {room_id} (group={group_openid})")

        sender = extract_sender(message.author)
        control_cmds = ("监控", "取消监控", "绑定动态", "取消动态")
        if cmd in control_cmds:
            denied = self._control_denied(sender)
            if denied is not None:
                return denied

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
            # 异常详情仅记日志，群内回显通用文案（不泄漏内部错误信息）
            logger.error(f"执行命令 {cmd} {room_id} 失败: {e}")
            return GENERIC_CMD_FAILURE

    def _group_allowed(self, sender: str) -> bool:
        """群控制指令权限：管理员名单未配置时不限制；无法识别发送者且已配置名单时拒绝"""
        if not self.group_admin_openids:
            return True
        return bool(sender) and sender in self.group_admin_openids

    def _control_denied(self, sender: str) -> Optional[str]:
        """控制指令权限判定；放行返回None，拒绝返回统一文案"""
        if self._group_allowed(sender):
            return None
        return (
            "无权限：仅机器人管理员可执行控制指令（查询指令不受限）\n"
            f"如你是管理员，请让机器人所有者将此openid加入配置Admin.openids：{sender or '未知'}"
        )

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

    async def _global_switch_states(self) -> Dict[str, bool]:
        """全局推送开关状态（仅本群设置可覆盖的7项；行缺失默认关）"""
        switches = await self.db_service.get_push_switches() \
            if self.db_service is not None else {}
        return {s: bool(switches.get(s, {}).get("enabled", False))
                for s in GROUP_PUSH_SWITCHES}

    async def _at_all_mode(self, group_openid: str, arg: str) -> str:
        """查看/设置本群的艾特全体模式（daily/rooms/off）"""
        if self.db_service is None:
            return "此功能需要数据库支持"

        arg = arg.strip()
        if arg in ("查看", ""):
            cfg = await self.db_service.get_group_at_all_mode(group_openid)
            mode = cfg["mode"]
            desc = {"daily": "每日首次开播提醒艾特全体",
                    "rooms": f"仅指定房间开播时艾特（{cfg['room_ids'] or '未设置'}）",
                    "off": "已关闭艾特全体"}[mode]
            return (f"本群艾特全体：{desc}\n"
                    f"修改：/艾特全体 每日 | /艾特全体 仅 房间号[,房间号] | /艾特全体 关闭")

        if arg == "每日":
            await self.db_service.set_group_at_all_mode(group_openid, "daily")
            return "已设置：每日首次开播提醒时艾特全体"

        if arg.startswith("关"):
            await self.db_service.set_group_at_all_mode(group_openid, "off")
            return "已关闭本群艾特全体（开播提醒不再@全体成员）"

        if arg.startswith("仅"):
            room_part = arg[1:].strip().replace("，", ",").replace(" ", "")
            # int()可解析全角数字但直接存库会与str(room_id)永不匹配——
            # 统一经int()归一化回半角；限制条数防超room_ids列宽(String(500))
            rooms = []
            for r in room_part.split(","):
                r = r.strip()
                if not r or len(r) > 10:
                    continue
                try:
                    rooms.append(str(int(r)))
                except ValueError:
                    continue
            rooms = rooms[:30]
            if not rooms:
                return "用法：/艾特全体 仅 房间号[,房间号]（多个房间用逗号分隔，最多30个）"
            room_ids = ",".join(rooms)
            await self.db_service.set_group_at_all_mode(
                group_openid, "rooms", room_ids)
            return f"已设置：仅房间 {room_ids} 开播时艾特全体"

        return "用法：/艾特全体 查看 | 每日 | 仅 房间号[,房间号] | 关闭"

    async def _push_setting(self, group_openid: str, room_id: Optional[int],
                            arg: Optional[str] = None,
                            state: Optional[str] = None) -> str:
        """本群推送设置：概览 / 房间明细 / 单项开|关 / 恢复跟随全局"""
        if self.db_service is None:
            return "推送设置功能不可用（数据库未连接）"

        bindings = self.binding_store.get_group_bindings(group_openid)
        if not bindings:
            return "本群当前没有推送中的直播间，发送 /帮助 查看指令"

        if room_id is None:  # 概览：每绑定房间一行开关徽标
            glob = await self._global_switch_states()
            lines = ["本群推送设置概览："]
            for rid in sorted(b.room_id for b in bindings):
                own = await self.db_service.get_group_push_setting(rid, group_openid)
                custom = own is not None and any(
                    own[s] != glob[s] for s in GROUP_PUSH_SWITCHES)
                badges = " ".join(
                    f"{_PUSH_SWITCH_META[s][1]}：{'开' if (own or {}).get(s, glob[s]) else '关'}"
                    for s in GROUP_PUSH_SWITCHES)
                lines.append(f"· 房间{rid}{'（独立）' if custom else ''} {badges}")
            lines.append("明细：/推送设置 房间号；修改：/推送设置 房间号 开关名 开|关；"
                         "恢复跟随全局：/推送设置 房间号 恢复")
            return "\n".join(lines)

        # 指定房间：须为本群已绑定房间（短号归一化后再判定）
        binding = self.binding_store.get_binding(group_openid, room_id)
        if binding is None:
            real_id = await self._resolve_query_room(room_id)
            if real_id is not None:
                room_id = real_id
                binding = self.binding_store.get_binding(group_openid, room_id)
        if binding is None:
            return f"房间{room_id}不是本群绑定的直播间，发送 /绑定列表 查看"

        if arg is None:  # 明细：逐项 名称 开/关（跟随全局）
            own = await self.db_service.get_group_push_setting(room_id, group_openid)
            glob = await self._global_switch_states()
            lines = [f"房间{room_id}本群推送设置："]
            for s in GROUP_PUSH_SWITCHES:
                on = (own or {}).get(s, glob[s])
                follow = own is None or own[s] == glob[s]
                lines.append(f"· {_PUSH_SWITCH_META[s][0]}：{'开' if on else '关'}"
                             + ("（跟随全局）" if follow else ""))
            lines.append(f"修改：/推送设置 {room_id} 开关名 开|关；"
                         f"恢复跟随全局：/推送设置 {room_id} 恢复")
            return "\n".join(lines)

        if arg == "恢复":
            removed = await self.db_service.reset_group_push_setting(
                room_id, group_openid)
            if not removed:
                return f"房间{room_id}本群无独立推送设置（已在跟随全局）"
            return f"已清除房间{room_id}本群独立推送设置，恢复跟随全局"

        switch = _PUSH_SWITCH_ALIASES.get(arg)
        if switch is None:
            if arg in ("开", "关", "开启", "关闭"):
                # 漏写开关名（"/推送设置 房间号 开"，开/关落到arg位置）：
                # 给出用法引导而非"未知开关：开"
                return ("缺少开关名，用法：/推送设置 房间号 开关名 开|关\n"
                        f"示例：/推送设置 {room_id} 天选 开\n"
                        "可用开关：天选/天选时刻、大航海/大航海成就、系统事件、"
                        "房间信息/房间变更、每日结算/日报、每周结算/周报、每月结算/月报")
            return ("未知开关：" + arg + "\n可用开关：天选/天选时刻、大航海/大航海成就、"
                    "系统事件、房间信息/房间变更、每日结算/日报、每周结算/周报、每月结算/月报")
        if state is None:
            return f"请指定开或关，如：/推送设置 {room_id} {arg} 开"

        enabled = state.startswith("开")
        # 行不存在时先按全局值播种，避免其余开关被默认置关
        if await self.db_service.get_group_push_setting(room_id, group_openid) is None:
            glob = await self._global_switch_states()
            await self.db_service.seed_group_push_settings(
                room_id, [group_openid], glob)
        ok = await self.db_service.set_group_push_setting(
            room_id, group_openid, switch, enabled)
        if not ok:
            return "设置失败，请稍后重试"
        label = _PUSH_SWITCH_META[switch][0]
        return (f"房间{room_id}本群{label}已设为{'开' if enabled else '关'}"
                f"（独立生效，恢复：/推送设置 {room_id} 恢复）")

    async def _status(self, group_openid: str) -> str:
        """查询本群监控中的直播间与动态订阅状态"""
        monitored = [
            b for b in self.binding_store.get_group_bindings(group_openid)
            if b.monitored
        ]
        dyn_ups = self.binding_store.get_group_dynamic_ups(group_openid) \
            if self.binding_store else []
        if not monitored and not dyn_ups:
            return "本群当前没有监控中的直播间与动态订阅，发送 /帮助 查看指令"

        lines = []
        if monitored:
            lines.append("本群监控中的直播间：")
            for b in monitored:
                state = "监控运行中" if self.monitor_manager.has(b.room_id) else "监控未运行（等待恢复）"
                lines.append(f"· 房间{b.room_id}（主播UID:{b.uid}）- {state}")
            lines.append(f"全局监控房间数：{len(self.monitor_manager.room_ids())}")
        if dyn_ups:
            lines.append("本群订阅的UP主动态：")
            for uid in dyn_ups:
                name = self.binding_store.get_dynamic_up_name(uid) or ""
                name_text = f"（{name}）" if name else ""
                if self.dynamic_manager is not None and self.dynamic_manager.has(uid):
                    state = "轮询运行中"
                else:
                    state = "轮询未运行"
                lines.append(f"· UP主{uid}{name_text} - {state}")
            if not await self._dyn_switch_on():
                lines.append("⚠️ 全局动态监控推送开关当前为关（后台-推送设置），"
                             "新动态不会推送到群")
        return "\n".join(lines)

    async def _dyn_switch_on(self) -> bool:
        """运行时动态监控开关状态（DB优先；无DB回落config默认即manager.enabled）"""
        if self.db_service is not None:
            try:
                if self.dynamic_manager is not None:
                    return await self.db_service.get_push_switch_enabled(
                        "dynamic_monitor", default=self.dynamic_manager.enabled)
                return await self.db_service.get_push_switch_enabled("dynamic_monitor")
            except Exception:
                pass
        return bool(self.dynamic_manager.enabled) if self.dynamic_manager else False

    async def _my_bindings(self, group_openid: str) -> str:
        """查询本群推送的直播间与动态订阅列表"""
        bindings = self.binding_store.get_group_bindings(group_openid)
        dyn_ups = self.binding_store.get_group_dynamic_ups(group_openid) \
            if self.binding_store else []
        if not bindings and not dyn_ups:
            return "本群当前没有推送中的直播间与动态订阅，发送 /帮助 查看指令"

        lines = []
        if bindings:
            lines.append("本群推送的直播间：")
            for b in bindings:
                mon = "监控中" if b.monitored else "未监控"
                lines.append(f"· 房间{b.room_id}（主播UID:{b.uid}）- {mon}")
        if dyn_ups:
            lines.append("本群订阅的UP主动态：")
            for uid in dyn_ups:
                name = self.binding_store.get_dynamic_up_name(uid) or ""
                name_text = f"（{name}）" if name else ""
                lines.append(f"· UP主{uid}{name_text}")
        return "\n".join(lines)

    _KNOWN_CMDS = ("监控", "取消监控", "绑定动态", "取消动态", "推送设置",
                   "艾特全体", "收入", "sc榜", "状态", "绑定列表", "汇总", "帮助")

    @classmethod
    def _suggest_command(cls, content: str) -> Optional[str]:
        """未匹配指令的模糊提示：/监空 → "你是不是想用 /监控"（仅对/开头）"""
        import difflib
        stripped = content.strip()
        if not stripped.startswith(("/", "／")):
            return None
        # 提取指令词（去掉/和后续参数）；先sub再取词——单独"/"经sub后
        # split为空列表，直接索引会IndexError
        parts = re.sub(r"^[/／]\s*", "", stripped).split()
        word = parts[0] if parts else ""
        if not word or len(word) < 2:
            return None
        matches = difflib.get_close_matches(word, cls._KNOWN_CMDS, n=1, cutoff=0.5)
        if matches:
            return f"你是不是想用 /{matches[0]}？发送 /帮助 查看全部指令"
        return None

    @staticmethod
    def help_text() -> str:
        """帮助文本"""
        return HELP_TEXT

    async def _bind_dynamic(self, group_openid: str, up_uid: int) -> str:
        """绑定群与UP主动态（启动轮询进程；输入房间号时自动归一化为UP的UID）"""
        if self.dynamic_manager is None or not self.dynamic_manager.enabled:
            return "动态监控未启用，请联系机器人所有者在配置中开启（Setting.dynamic_monitor）"

        note = ""
        # 房间号归一化：填了房间号时换算成主播UID（2026-11-10真机实测
        # 用户以房间号订阅，轮询的却是错误UID——feed查询空/风控）。
        # 碰撞守卫：输入恰好等于已有订阅UP的UID时不归一化（UID与房间号
        # 同数值时优先按UID意图处理，回显解析后UID可见可纠错）
        known_up_uids = set(
            getattr(self.binding_store, "get_dynamic_up_uids", lambda: [])()) \
            if self.binding_store else set()
        if up_uid not in known_up_uids and self.db_service is not None:
            try:
                room_uid = await self.db_service.get_room_uid(up_uid)
            except Exception:
                room_uid = 0
            if room_uid:
                note = f"\n（输入的{up_uid}是房间号，已自动换算为UP主UID）"
                up_uid = room_uid

        is_new = await self.binding_store.bind_dynamic(group_openid, up_uid)
        name = self.binding_store.get_dynamic_up_name(up_uid)
        name_text = f"（{name}）" if name else ""
        # 运行时开关门控（2026-11-10真机反馈）：config开关只决定指令可用性，
        # 后台"推送设置-动态监控"关时不得启动轮询——否则面板显示"轮询中"
        # 与开关状态矛盾，且推送被静默丢弃
        if not await self._dyn_switch_on():
            return (f"已订阅UP主{up_uid}{name_text}的动态{note}\n"
                    f"⚠️ 全局动态监控推送开关当前为关（后台-推送设置），"
                    f"开启后自动开始轮询推送")
        # 用户主动订阅：首轮无条件基线化——防止取消订阅后重新订阅时，
        # 停订期间的新动态被一次性补推（进程已存活时参数无效果）
        created, _ = await self.dynamic_manager.ensure(up_uid, baseline_first=True)
        if is_new:
            return f"已订阅UP主{up_uid}{name_text}的动态，更新将推送到本群{note}"
        return f"UP主{up_uid}{name_text}的动态已在订阅中，本群推送已开启{note}"

    async def _unbind_dynamic(self, group_openid: str, up_uid: int) -> str:
        """解除动态订阅（最后一个订阅群取消后停止轮询进程；房间号自动归一化）"""
        if self.dynamic_manager is None:
            return "动态监控功能不可用"

        # 与绑定同款房间号归一化（用户常以房间号操作）；已是订阅UP的UID
        # 时不归一化（UID==房间号碰撞优先按UID意图处理）
        known_up_uids = set(
            getattr(self.binding_store, "get_dynamic_up_uids", lambda: [])()) \
            if self.binding_store else set()
        if up_uid not in known_up_uids and self.db_service is not None:
            try:
                room_uid = await self.db_service.get_room_uid(up_uid)
            except Exception:
                room_uid = 0
            if room_uid:
                up_uid = room_uid

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
            end = now_local()
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
            f"· 礼物：{s['gift_gold']:.2f}元（{s['gift_count']}次）\n"
            f"· 上舰：{s['ship_total']:.2f}元（{s['ship_count']}次）\n"
            f"· SC：{s['sc_total']:.2f}元（{s['sc_count']}次）\n"
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
            end = now_local()
            start = end - timedelta(days=range_days)
            s = await self.db_service.get_summary_range(rid, start, end)
        else:
            s = await self.db_service.get_daily_summary(rid, day=day)
        return (
            f"直播间{rid} {s['day']} 统计：\n"
            f"· 弹幕：{s['danmaku_count']}条\n"
            f"· 礼物：{s['gift_gold']:.2f}元（{s['gift_count']}次）\n"
            f"· 上舰：{s['ship_total']:.2f}元（{s['ship_count']}次）\n"
            f"· SC：{s['sc_total']:.2f}元（{s['sc_count']}次）\n"
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
