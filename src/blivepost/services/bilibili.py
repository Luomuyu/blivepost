# -*- coding: utf-8 -*-
"""
B站API服务模块
"""

import requests

from typing import Optional

from ..models.live import LiveRoomInfo
from ..utils.logger import get_logger

logger = get_logger("bilibili")


class BilibiliAPI:
    """B站API服务"""

    HEADERS = {
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        )
    }

    def __init__(self, sessdata: str = "", fp_cookie: str = ""):
        """
        初始化B站API服务

        Args:
            sessdata: B站登录凭证
            fp_cookie: 完整的指纹cookie（包含buvid、fingerprint等）
        """
        self.sessdata = sessdata
        self.fp_cookie = fp_cookie

    def set_fp_cookie(self, fp_cookie: str) -> None:
        """
        设置指纹cookie

        Args:
            fp_cookie: 完整的指纹cookie
        """
        self.fp_cookie = fp_cookie

    def get_room_info(self, room_id: str) -> LiveRoomInfo:
        """
        获取直播间信息

        注意：内部使用同步requests，事件循环中调用时请配合asyncio.to_thread。

        Args:
            room_id: 直播间ID（短号/长号均可）

        Returns:
            LiveRoomInfo: 直播间信息

        Raises:
            ValueError: 获取信息失败时抛出
        """
        # 直播间信息：uid/长号/标题/封面/观看人数（无需签名，无风控）
        info_url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={room_id}"
        response = requests.get(url=info_url, headers=self.HEADERS, timeout=10)
        data = response.json()

        if data.get("code", 0) != 0:
            raise ValueError(f"获取直播间信息失败: {data.get('message', '未知错误')}")

        info = data["data"]
        uid = info["uid"]

        # 获取用户信息（用户名片，仅取昵称）
        user_info = self._get_user_info(uid)

        return LiveRoomInfo(
            uid=uid,
            name=user_info.get("name", ""),
            title=info.get("title", ""),
            cover=info.get("user_cover", "") or info.get("keyframe", ""),
            room_id=info.get("room_id", 0),
            online=f'{info.get("online", 0)}人看过',
        )

    def get_live_status(self, room_id) -> Optional[bool]:
        """
        查询直播间是否开播（轻量接口，仅查直播状态）

        Args:
            room_id: 直播间ID

        Returns:
            Optional[bool]: 是否开播；查询失败返回None
        """
        try:
            url = f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}"
            response = requests.get(url=url, headers=self.HEADERS, timeout=10)
            data = response.json()
            if data.get("code", -1) != 0:
                logger.warning(f"查询直播间{room_id}状态失败: {data.get('message', '未知错误')}")
                return None
            return data.get("data", {}).get("live_status") == 1
        except Exception as e:
            logger.warning(f"查询直播间{room_id}状态异常: {e}")
            return None

    def get_user_name(self, uid: int) -> str:
        """
        获取用户昵称（用户名片接口，免签名无风控；失败返回空串）

        Args:
            uid: 用户ID

        Returns:
            str: 昵称，获取失败为空串
        """
        try:
            return str(self._get_user_info(uid).get("name") or "")
        except Exception as e:
            logger.warning(f"查询用户{uid}昵称失败: {e}")
            return ""

    def _get_user_info(self, uid: int) -> dict:
        """
        获取用户信息（用户名片接口）

        x/space/wbi/acc/info风控严格（-352风控校验失败频发），改用无需
        Wbi签名与登录态的x/web-interface/card；调用方仅消费昵称。

        Args:
            uid: 用户ID

        Returns:
            dict: 用户名片（card对象）

        Raises:
            ValueError: 获取失败时抛出
        """
        url = f"https://api.bilibili.com/x/web-interface/card?mid={uid}"
        response = requests.get(url=url, headers=self.HEADERS, timeout=10)
        data = response.json()

        if data.get("code", 0) != 0:
            raise ValueError(f"获取用户信息失败: {data.get('message', '未知错误')}")

        return data.get("data", {}).get("card", {})
