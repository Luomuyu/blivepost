# -*- coding: utf-8 -*-
"""
B站API服务模块
"""

import time
from typing import Optional

import requests

from ..models.live import LiveRoomInfo
from ..utils.logger import get_logger
from .wbi import WbiSigner


logger = get_logger("bilibili")


class BilibiliAPI:
    """B站API服务"""

    HEADERS = {
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
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
        self._wbi = WbiSigner(headers=self.HEADERS)

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
            room_id: 直播间ID

        Returns:
            LiveRoomInfo: 直播间信息

        Raises:
            ValueError: 获取信息失败时抛出
        """
        # 获取UID
        init_url = f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}"
        response = requests.get(url=init_url, headers=self.HEADERS, timeout=10)
        data = response.json()

        if data.get("code", 0) != 0:
            raise ValueError(f"获取直播间信息失败: {data.get('message', '未知错误')}")

        uid = data["data"]["uid"]

        # 获取用户信息
        user_info = self._get_user_info(uid)

        return LiveRoomInfo(
            uid=uid,
            name=user_info.get("name", ""),
            title=user_info.get("live_room", {}).get("title", ""),
            cover=user_info.get("live_room", {}).get("cover", ""),
            room_id=user_info.get("live_room", {}).get("roomid", 0),
            online=user_info.get("live_room", {}).get("watched_show", {}).get("text_large", "0"),
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

    def _get_user_info(self, uid: int) -> dict:
        """
        获取用户信息（WBI签名，密钥失效自动刷新重试一次）

        Args:
            uid: 用户ID

        Returns:
            dict: 用户信息
        """
        w_data = f"mid={uid}&platform=web&token=&web_location=1550101"
        wts = str(int(time.time()))
        w_rid = self._wbi.sign(w_data, wts)

        url = (
            f"https://api.bilibili.com/x/space/wbi/acc/info?{w_data}"
            f"&w_rid={w_rid}&wts={wts}"
        )

        headers = self.HEADERS.copy()
        headers["referer"] = f"https://space.bilibili.com/{uid}"

        # 使用完整的fp_cookie，否则使用sessdata
        if self.fp_cookie:
            headers["cookie"] = self.fp_cookie
        elif self.sessdata:
            headers["cookie"] = f"SESSDATA={self.sessdata}"

        response = requests.get(url=url, headers=headers, timeout=10)
        data = response.json()

        # 处理WBI密钥失效
        if data.get("code") == -403:
            logger.warning("WBI密钥失效，重新获取")
            w_rid = self._resign(w_data, wts)
            url = (
                f"https://api.bilibili.com/x/space/wbi/acc/info?{w_data}"
                f"&w_rid={w_rid}&wts={wts}"
            )
            response = requests.get(url=url, headers=headers, timeout=10)
            data = response.json()

        if data.get("code", 0) != 0:
            raise ValueError(f"获取用户信息失败: {data.get('message', '未知错误')}")

        return data.get("data", {})

    def _resign(self, w_data: str, wts: str) -> str:
        """强制刷新密钥后重新签名"""
        self._wbi.get_w_key(force_refresh=True)
        return self._wbi.sign(w_data, wts)
