# -*- coding: utf-8 -*-
"""
消息推送服务（通过QQ官方机器人botpy主动推送）
"""

import inspect
import re
from datetime import datetime
from typing import Any, List, Optional

from ..config.settings import Settings
from ..models.live import LiveRoomInfo, LiveStats
from ..utils.logger import get_logger


logger = get_logger("push")

# CQ码图片：提取file参数作为URL文本
_CQ_IMAGE_RE = re.compile(r"\[CQ:image,[^\]]*?file=([^\],]+)[^\]]*\]")
# 其余CQ码：直接移除（官方机器人不支持@全体成员等CQ功能）
_CQ_RE = re.compile(r"\[CQ:[^\]]+\]")


def _supports_kwarg(func, name: str) -> bool:
    """机器人客户端方法是否支持某关键字参数（自定义bot鸭子类型兼容）"""
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def live_enter_keyboard(room_id) -> dict:
    """
    开播推送的「进入直播间」按钮keyboard（对齐官方v2群消息接口文档）

    蓝色线框按钮、所有人可点击，type=0跳转按钮，data为bilibili直播
    协议链接（唤起B站客户端进入对应直播间）。

    字段依据官方文档（v2_groups_group_openid_messages.post）：
      - render_data.style: 0=灰色线框 1=蓝色线框 3=白底红字 4=蓝底白字
      - render_data.visited_label: 点击后按钮文字（无visited_color字段）
      - action.permission.type: 2=所有人
      - action.type: 0=跳转按钮（http或协议链接）

    Args:
        room_id: 完整直播间房间号

    Returns:
        dict: QQ官方keyboard结构
    """
    return {
        "content": {
            "rows": [{
                "buttons": [{
                    "id": "enter_live_room",
                    "render_data": {
                        "label": "进入直播间",
                        "visited_label": "跳转中…",
                        "style": 1,  # 1=蓝色线框
                    },
                    "action": {
                        "type": 0,  # 0=跳转按钮
                        "permission": {"type": 2},  # 2=所有人可点击
                        "data": f"bilibili://live/{room_id}",
                        "unsupport_tips": "当前QQ版本过低，无法跳转直播间",
                    },
                }]
            }]
        }
    }


def convert_cq_message(message: str) -> str:
    """
    将CQ码消息转换为官方机器人可发送的纯文本

    图片CQ码保留URL，其余CQ码（如@全体成员）移除。

    Args:
        message: 含CQ码的消息

    Returns:
        str: 纯文本消息
    """
    text = _CQ_IMAGE_RE.sub(lambda m: f"\n{m.group(1)}\n", message)
    text = _CQ_RE.sub("", text)
    return text.strip()


class PushService:
    """消息推送服务"""

    def __init__(self, settings: Settings):
        """
        初始化推送服务

        Args:
            settings: 应用配置
        """
        self.settings = settings
        self.templates = settings.templates
        # QQBotClient实例，由外部（main）设置
        self.bot_client: Any = None
        # BindingStore实例，由外部（main）设置；提供后按绑定关系路由推送
        self.binding_store: Any = None
        # BilibiliAPI实例（子进程侧设置，开播/下播推送前查询房间信息）
        self.bilibili_api: Any = None
        # ContactStore实例（可选）：发送前过滤未开启消息接收的群
        self.contact_store: Any = None

    @staticmethod
    def _render(template: str, fields: dict) -> Optional[str]:
        """
        渲染自定义推送模板

        Args:
            template: 模板字符串（str.format占位符）
            fields: 占位符字段

        Returns:
            Optional[str]: 渲染结果；模板为空或渲染失败返回None（回退默认模板）
        """
        if not template:
            return None
        try:
            return template.format(**fields)
        except (KeyError, IndexError, ValueError) as e:
            logger.warning(f"推送模板渲染失败，回退默认模板: {e}")
            return None

    def _resolve_groups(self, room_id) -> List[str]:
        """
        解析推送目标群：绑定列表中该直播间处于监控状态的群openid

        Args:
            room_id: 直播间ID

        Returns:
            List[str]: 群openid列表（无绑定时为空）
        """
        if self.binding_store is None or room_id is None:
            return []
        try:
            rid = int(room_id)
        except (TypeError, ValueError):
            return []
        return self.binding_store.get_monitored_groups(rid)

    def send_message(self, message: str, room_id=None, keyboard: Optional[dict] = None) -> bool:
        """
        发送消息到目标群

        Args:
            message: 消息内容（可含CQ码，会自动转换为纯文本）
            room_id: 直播间ID（按绑定关系路由）
            keyboard: 按钮keyboard结构（可选，需机器人支持）

        Returns:
            bool: 是否成功调度发送
        """
        if self.bot_client is None:
            logger.error("未配置QQ官方机器人客户端，消息发送失败")
            return False

        groups = self._resolve_groups(room_id)
        if not groups:
            logger.warning(f"直播间{room_id}无监控中的绑定群，跳过发送")
            return False

        # 过滤未开启消息接收的群
        if self.contact_store is not None:
            groups = self.contact_store.filter_receivable_groups(groups)
            if not groups:
                logger.info(f"直播间{room_id}的目标群均未开启消息接收，跳过发送")
                return False

        content = convert_cq_message(message)
        if not content:
            logger.warning("消息内容为空，跳过发送")
            return False

        if _supports_kwarg(self.bot_client.push, "keyboard"):
            return self.bot_client.push(content, groups, keyboard=keyboard)
        return self.bot_client.push(content, groups)

    def push_media_message(self, image_url: str, content: str, room_id=None,
                           keyboard: Optional[dict] = None) -> bool:
        """
        发送图片富媒体消息（不支持时回退为文本+URL）

        Args:
            image_url: 图片URL
            content: 文本内容
            room_id: 直播间ID（按绑定关系路由）
            keyboard: 按钮keyboard结构（可选，回退文本时同样携带）

        Returns:
            bool: 是否成功调度发送
        """
        if self.bot_client is None:
            logger.error("未配置QQ官方机器人客户端，消息发送失败")
            return False

        groups = self._resolve_groups(room_id)
        if not groups:
            logger.warning(f"直播间{room_id}无监控中的绑定群，跳过发送")
            return False
        if self.contact_store is not None:
            groups = self.contact_store.filter_receivable_groups(groups)
            if not groups:
                return False

        if hasattr(self.bot_client, "push_media"):
            if _supports_kwarg(self.bot_client.push_media, "keyboard"):
                return self.bot_client.push_media(image_url, content, groups,
                                                  keyboard=keyboard)
            return self.bot_client.push_media(image_url, content, groups)
        return self.send_message(f"{content}\n{image_url}", room_id=room_id,
                                 keyboard=keyboard)

    def _live_start_text(self, room_info: LiveRoomInfo) -> str:
        """开播消息文本（优先使用自定义模板）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        url = f"https://live.bilibili.com/{room_info.room_id}"
        text = self._render(self.templates.live_start, {
            "name": room_info.name,
            "title": room_info.title,
            "room_id": room_info.room_id,
            "online": room_info.online,
            "time": now,
            "cover": room_info.cover,
            "url": url,
        })
        if text is not None:
            return text
        return (
            f"[{room_info.name}]的直播间开播啦！！！\n"
            f"标题：{room_info.title}\n"
            f"房间号：{room_info.room_id}\n"
            f"当前共有：{room_info.online}\n"
            f"直播地址：{url}\n"
            f"推送时间：{now}"
        )

    def _live_start_image(self, room_info: LiveRoomInfo) -> Optional[str]:
        """开播消息封面图URL"""
        return room_info.cover or None

    def _live_end_text(self, room_info: LiveRoomInfo, stats: LiveStats) -> str:
        """下播消息文本（优先使用自定义模板）"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        url = f"https://live.bilibili.com/{room_info.room_id}"
        text = self._render(self.templates.live_end, {
            "name": room_info.name,
            "title": room_info.title,
            "room_id": room_info.room_id,
            "online": room_info.online,
            "time": now,
            "cover": room_info.cover,
            "url": url,
            "like_count": stats.like_count,
            "duration": stats.duration_str,
            "danmaku_count": stats.danmaku_count,
            "gold_count": stats.gold_count,
            "ship_count": stats.ship_count,
            "sc_count": stats.sc_count,
        })
        if text is not None:
            return text
        return (
            f"[{room_info.name}]的直播间下播了。。。\n"
            f"标题：{room_info.title}\n"
            f"房间号：{room_info.room_id}\n"
            f"当前共有：{room_info.online}\n"
            f"点赞总数：{stats.like_count}\n"
            f"直播地址：{url}\n"
            f"直播时长：{stats.duration_str}\n"
            f"推送时间：{now}"
        )

    def push_live_start(self, room_info: LiveRoomInfo) -> bool:
        """
        推送开播消息（有封面时优先富媒体，回退文本；携带「进入直播间」按钮）

        Args:
            room_info: 直播间信息

        Returns:
            bool: 是否发送成功
        """
        keyboard = live_enter_keyboard(room_info.room_id)
        cover = self._live_start_image(room_info)
        if cover and self.bot_client is not None:
            return self.push_media_message(cover, self._live_start_text(room_info),
                                           room_id=room_info.room_id, keyboard=keyboard)
        message = self._live_start_text(room_info) + f"\n封面：{cover}" if cover \
            else self._live_start_text(room_info)
        return self.send_message(message, room_id=room_info.room_id, keyboard=keyboard)

    def push_live_end(
        self,
        room_info: LiveRoomInfo,
        stats: LiveStats
    ) -> bool:
        """
        推送下播消息（有封面时优先富媒体，回退文本）

        Args:
            room_info: 直播间信息
            stats: 直播统计数据

        Returns:
            bool: 是否发送成功
        """
        text = self._live_end_text(room_info, stats)
        if room_info.cover and self.bot_client is not None:
            return self.push_media_message(room_info.cover, text,
                                           room_id=room_info.room_id)
        return self.send_message(text, room_id=room_info.room_id)

    def notify_not_login(self, room_id) -> bool:
        """
        上报登录凭证失效（基类为空实现，由子进程的QueuePushService覆写为队列上报）

        Args:
            room_id: 直播间ID

        Returns:
            bool: 是否成功上报
        """
        del room_id
        return False

    def push_anchor(
        self,
        room_id: str,
        award_name: str,
        danmu: str,
        require_text: str
    ) -> bool:
        """推送天选抽奖"""
        text = self._render(self.templates.anchor, {
            "room_id": room_id, "award": award_name, "danmu": danmu, "require": require_text,
        })
        if text is None:
            text = (
                f"直播间{room_id}天选抽奖："
                f"礼物:{award_name}，弹幕:{danmu}，要求:{require_text}"
            )
        return self.send_message(text, room_id=room_id)

    def push_guard_achievement(
        self,
        room_id: str,
        content: str
    ) -> bool:
        """推送大航海成就"""
        text = self._render(self.templates.guard_achievement, {
            "room_id": room_id, "content": content,
        })
        if text is None:
            text = f"直播间{room_id}获得大航海成就：{content}"
        return self.send_message(text, room_id=room_id)

    def push_room_block(self, room_id: str, user_name: str, uid: int) -> bool:
        """推送禁言事件"""
        return self.send_message(
            f"System：{room_id} {user_name}[{uid}]被禁言", room_id=room_id
        )

    def push_admin_change(self, room_id: str, uid: int, revoked: bool) -> bool:
        """推送房管变更事件"""
        action = "撤销房管" if revoked else "被设为房管"
        return self.send_message(f"System：{room_id} [{uid}]{action}", room_id=room_id)

    def push_room_change(self, room_id: str, title: str,
                         parent_area: str, area: str) -> bool:
        """推送房间信息变更"""
        return self.send_message(
            f"System：{room_id}直播间信息更新：标题: {title}，分区: {parent_area}-{area}",
            room_id=room_id,
        )

    def push_cut_off(self, room_id: str, reason: str) -> bool:
        """推送直播切断"""
        text = self._render(self.templates.cut_off, {
            "room_id": room_id, "reason": reason,
        })
        if text is None:
            text = f"System：{room_id}直播被切断，原因：{reason}"
        return self.send_message(text, room_id=room_id)

    def push_warning(self, room_id: str, reason: str) -> bool:
        """推送警告"""
        text = self._render(self.templates.warning, {
            "room_id": room_id, "reason": reason,
        })
        if text is None:
            text = f"System：{room_id}收到警告，原因：{reason}"
        return self.send_message(text, room_id=room_id)
