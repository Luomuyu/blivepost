# -*- coding: utf-8 -*-
# Portions of this file are adapted from blivedm:
# https://github.com/xfgryujk/blivedm
# Copyright (c) 2018 xfgryujk
# Original license: MIT
# See licenses/blivedm-MIT.txt
#
# Additional modifications and original code:
# Copyright (c) 2026 Luomuyu
# Licensed under GPL-3.0 (see LICENSE)
"""
blivedm补丁 - 添加自定义消息处理
"""

from pathlib import Path
from typing import Any, Callable, Optional

import aiohttp

from .importer import import_blivedm
from . import models as vendor_models
from ..utils.logger import get_logger


logger = get_logger("vendor.patch")

# 未知命令输出文件路径
UNKNOWN_CMD_FILE = Path("unknowncmd.txt")


def _make_info_callback(method_name: str, message_cls: Any) -> Callable:
    """
    创建信息类型回调
    
    Args:
        method_name: 方法名
        message_cls: 消息类
        
    Returns:
        回调函数
    """
    def callback(self, client, command: dict):
        method = getattr(self, method_name)
        return method(client, message_cls.from_command(command))
    return callback


def _make_msg_callback(method_name: str, message_cls: Any) -> Callable:
    """
    创建消息类型回调
    
    Args:
        method_name: 方法名
        message_cls: 消息类
        
    Returns:
        回调函数
    """
    def callback(self, client, command: dict):
        method = getattr(self, method_name)
        return method(client, message_cls.from_command(command['data']))
    return callback


def _extend_all(module: Any, *names: str) -> None:
    """
    扩展模块的__all__
    
    Args:
        module: 模块对象
        names: 要添加的名称
    """
    current = set(getattr(module, "__all__", ()))
    current.update(names)
    module.__all__ = tuple(sorted(current))


def _write_unknown_cmd(room_id: int, cmd: str, command: dict) -> None:
    """
    将未知命令写入文件
    
    Args:
        room_id: 直播间ID
        cmd: 命令名
        command: 完整命令数据
    """
    try:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(UNKNOWN_CMD_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] room={room_id} cmd={cmd}\n")
            f.write(f"  {command}\n\n")
    except Exception as e:
        logger.error(f"写入未知命令文件失败: {e}")


# 需要从日志未知命令中移除的命令
CMDS_TO_REMOVE = [
    'LIVE',
    'PREPARING',
    'USER_TOAST_MSG',
]

# 需要添加到日志未知命令的命令（这些命令会被忽略，不打印日志）
CMDS_TO_ADD = {
    'INTERACT_WORD',
    'GUARD_BUY',
    'POPULARITY_RED_POCKET_WINNER_LIST',
    'RECOMMEND_CARD',
    'GOTO_BUY_FLOW',
    'GIFT_STAR_PROCESS',
    'COMMON_NOTICE_DANMAKU',
    'POPULAR_RANK_CHANGED',
    'DANMU_AGGREGATION',
    'POPULARITY_RED_POCKET_NEW',
    'LIKE_INFO_V3_CLICK',
    'WIDGET_GIFT_STAR_PROCESS',
    'USER_TASK_PROGRESS',
    'USER_TASK_PROGRESS_V2',
    'PK_BATTLE_PRE',
    'PK_BATTLE_START_NEW',
    'PK_BATTLE_START',
    'AREA_RANK_CHANGED',
    'GUARD_HONOR_THOUSAND',
    'RING_STATUS_CHANGE',
    'RING_STATUS_CHANGE_V2',
    'SPREAD_ORDER_OVER',
    'LITTLE_MESSAGE_BOX',
    'PLAYTOGETHER_ICON_CHANGE',
    'NEW_PK_START',
    'SPREAD_ORDER_START',
    'SPREAD_SHOW_FEET_V2',
    'SPREAD_SHOW_FEET',
    'ENTRY_EFFECT_MUST_RECEIVE',
    'SHOPPING_CART_SHOW',
    'SHOPPING_BUBBLES_STYLE',
    'ANCHOR_ECOMMERCE_STATUS',
    'CARD_MSG',
    'MESSAGEBOX_USER_GAIN_MEDAL',
    'ANCHOR_HELPER_DANMU',
    'MESSAGEBOX_USER_MEDAL_CHANGE',
    'LIKE_INFO_V3_NOTICE',
    'WEALTH_NOTIFY',
    'USER_PANEL_RED_ALARM',
    'BENEFIT_STATUS',
    'LIKE_GUIDE_USER',
    'DM_INTERACTION',
    'GIFT_BOARD_RED_DOT',
    'TRADING_SCORE',
    'LITTLE_TIPS',
    'ROOM_SKIN_MSG',
    'SYS_MSG',
    'SUPER_CHAT_ENTRANCE',
    'ROOM_ADMINS',
    'VOICE_JOIN_STATUS',
    'MULTI_VOICE_OPERATIN',
    'ROOM_SILENT_OFF',
    'SUPER_CHAT_MESSAGE_DELETE',
    'INTERACT_WORD_V2',
    'ONLINE_RANK_V3',
    'COMMON_ANIMATION',
    'USER_TOAST_MSG',
    'VOICE_JOIN_ROOM_COUNT_INFO',
    'VOICE_JOIN_LIST',
    'master_qn_strategy_chg',
    'RANK_CHANGED',
    'RANK_CHANGED_V2',
    'POPULAR_RANK_GUIDE_CARD',
    'WIDGET_WISH_INFO',
    'WIDGET_WISH_INFO_V2',
    'OTHER_SLICE_LOADING_RESULT',
    'PLAYURL_RELOAD',
    'HOT_ROOM_NOTIFY',
    'POPULARITY_RED_POCKET_V2_NEW',
    'POPULARITY_RED_POCKET_V2_START',
    'POPULARITY_RED_POCKET_V2_WINNER_LIST',
    'RANK_REM',
    'RECALL_DANMU_MSG',
    'LIVE_MULTI_VIEW_EVENT_CHANGE',
    'VOICE_JOIN_SWITCH',
    'VOICE_JOIN_SWITCH_V2',
    'FLOW_REWARD_CARD',
    'ANCHOR_LOT_NOTICE',
}

# 要添加的命令回调
CMD_CALLBACK_DICT_ADD = {
    # 观看变更
    'WATCHED_CHANGE': _make_msg_callback('_on_watched_change', vendor_models.WatchedChange),
    # 未登录账号
    'LOG_IN_NOTICE': _make_info_callback('_on_not_login', vendor_models.LoginnoticeCmd),
    # 房间信息更改
    'ROOM_CHANGE': _make_msg_callback('_on_room_change', vendor_models.RoomChange),
    # 开播
    'LIVE': _make_info_callback('_on_live_start', vendor_models.LiveStart),
    # 下播
    'PREPARING': _make_info_callback('_on_live_preparing', vendor_models.LivePreparing),
    # 天选
    'ANCHOR_LOT_START': _make_msg_callback('_on_anchor_start', vendor_models.AnchorStart),
    # 红包
    'POPULARITY_RED_POCKET_START': _make_msg_callback('_on_redpocket_start', vendor_models.RedPocketStart),
    # 舰队成就
    'GUARD_ACHIEVEMENT_ROOM': _make_msg_callback('_on_guard_achievement', vendor_models.GuardAchievementRoom),
    # 被切
    'CUT_OFF': _make_info_callback('_on_cut_off', vendor_models.CutOff),
    # 警告
    'WARNING': _make_info_callback('_on_receive_warning', vendor_models.Warned),
    # 点赞变更
    'LIKE_INFO_V3_UPDATE': _make_msg_callback('_on_like_update', vendor_models.LikeUpdate),
    # PK开始
    'PK_BATTLE_PRE_NEW': _make_msg_callback('_on_pk_start', vendor_models.PKStart),
    # 有人被禁言
    'ROOM_BLOCK_MSG': _make_msg_callback('_on_room_block', vendor_models.RoomBlock),
    # 设置房管
    'room_admin_entrance': _make_info_callback('_on_admin_entrance', vendor_models.AdminEntrance),
    # 撤销房管
    'ROOM_ADMIN_REVOKE': _make_info_callback('_on_admin_revoke', vendor_models.AdminRevoke),
}

# 要删除的命令回调
CMD_CALLBACK_DICT_DEL = {
    'GUARD_BUY',
    'SUPER_CHAT_MESSAGE_DELETE',
}


def apply_patch(ignore_cmds: Optional[list] = None) -> None:
    """
    应用blivedm补丁
    
    这个函数会修改blivedm的内部结构，添加自定义消息处理
    
    Args:
        ignore_cmds: 用户自定义的忽略命令列表
    """
    blivedm = import_blivedm()
    
    def _patched_handle(self, client, command: dict):
        """
        打补丁的 handle 方法，记录未知命令到文件
        """
        cmd = command.get('cmd', '')
        pos = cmd.find(':')  # 2019-5-29 B站弹幕升级新增了参数
        if pos != -1:
            cmd = cmd[:pos]

        if cmd not in self._CMD_CALLBACK_DICT:
            # 只有第一次遇到未知cmd时打日志并写入文件
            if cmd not in blivedm.handlers.logged_unknown_cmds:
                logger.warning('room=%d unknown cmd=%s, command=%s', client.room_id, cmd, command)
                blivedm.handlers.logged_unknown_cmds.add(cmd)
                # 写入未知命令到文件
                _write_unknown_cmd(client.room_id, cmd, command)
            return

        callback = self._CMD_CALLBACK_DICT[cmd]
        if callback is not None:
            callback(self, client, command)
    
    # 替换 handle 方法
    blivedm.handlers.BaseHandler.handle = _patched_handle
    
    # 从日志未知命令中移除
    for cmd in CMDS_TO_REMOVE:
        if cmd in blivedm.handlers.logged_unknown_cmds:
            blivedm.handlers.logged_unknown_cmds.remove(cmd)
    
    # 添加到日志未知命令（这些命令会被忽略）
    blivedm.handlers.logged_unknown_cmds.update(CMDS_TO_ADD)
    
    # 添加用户自定义的忽略命令
    if ignore_cmds:
        blivedm.handlers.logged_unknown_cmds.update(ignore_cmds)
        logger.info(f"已添加用户自定义忽略命令: {ignore_cmds}")
    
    # 扩展web模型的__all__
    _extend_all(
        blivedm.models.web,
        'UserToastV2Message',
        'RoomChange',
        'LiveStart',
        'LivePreparing',
        'AnchorStart',
        'RedPocketStart',
        'GuardAchievementRoom',
        'CutOff',
        'Warned',
        'LikeUpdate',
        'PKStart',
        'RoomBlock',
        'AdminEntrance',
        'AdminRevoke',
    )
    
    # 添加命令回调
    for cmd, callback in CMD_CALLBACK_DICT_ADD.items():
        if cmd not in CMD_CALLBACK_DICT_DEL:
            blivedm.handlers.BaseHandler._CMD_CALLBACK_DICT[cmd] = callback
    
    # 删除命令回调
    for cmd in CMD_CALLBACK_DICT_DEL:
        if cmd in blivedm.handlers.BaseHandler._CMD_CALLBACK_DICT:
            del blivedm.handlers.BaseHandler._CMD_CALLBACK_DICT[cmd]

    _apply_heartbeat_watchdog(blivedm)

    logger.info("blivedm补丁已应用")


# 连续心跳失败阈值：blivedm心跳间隔30秒，3次约90秒判定连接僵死
HEARTBEAT_FAIL_EXIT = 3


def _apply_heartbeat_watchdog(blivedm) -> None:
    """
    心跳失败看门狗补丁

    blivedm的_send_heartbeat对连接异常只记WARNING且定时器永远重排——
    传输层已死时进程存活、join()不返回，监控静默失效。此补丁统计连续
    失败次数，达阈值后stop()客户端：join()返回→子进程结束→主进程
    watcher自动重建监控。
    """
    import blivedm.clients.ws_base as ws_base

    ws_cls = ws_base.WebSocketClientBase
    if getattr(ws_cls, "_heartbeat_watchdog_patched", False):
        return

    async def _patched_send_heartbeat(self):
        if self._websocket is None or self._websocket.closed:
            return
        try:
            await self._websocket.send_bytes(
                self._make_packet({}, ws_base.Operation.HEARTBEAT))
            self._heartbeat_fail_count = 0
        except (ConnectionResetError, aiohttp.ClientConnectionError) as e:
            self._heartbeat_fail_count = getattr(self, "_heartbeat_fail_count", 0) + 1
            logger.warning(
                'room=%s _send_heartbeat() failed (%d/%d): %r',
                self.room_id, self._heartbeat_fail_count, HEARTBEAT_FAIL_EXIT, e)
            if self._heartbeat_fail_count >= HEARTBEAT_FAIL_EXIT:
                self._heartbeat_fail_count = 0
                logger.warning(
                    'room=%s 连续%d次心跳失败，弹幕连接已失效，'
                    '停止客户端以触发监控自动重建',
                    self.room_id, HEARTBEAT_FAIL_EXIT)
                # 置空websocket终止心跳定时器重排，stop()取消网络任务
                # 使join()返回，子进程随之结束
                self._websocket = None
                try:
                    self.stop()
                except Exception:
                    logger.exception('room=%s 心跳看门狗停止客户端失败', self.room_id)
        except Exception:  # noqa
            logger.exception('room=%s _send_heartbeat() failed:', self.room_id)

    ws_cls._send_heartbeat = _patched_send_heartbeat
    ws_cls._heartbeat_watchdog_patched = True
    logger.info("心跳失败看门狗已启用（连续%d次失败自动重建监控）", HEARTBEAT_FAIL_EXIT)
