# -*- coding: utf-8 -*-
"""
B站WBI签名（单一实现，BilibiliAPI与FingerprintService共用）
"""

import functools
import hashlib
import time
from typing import Optional, Tuple

import requests

from ..utils.logger import get_logger


logger = get_logger("wbi")

# WBI密钥混淆表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52
]

DEFAULT_HEADERS = {
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
}


def derive_wbi_key(img_url: str, sub_url: str) -> str:
    """
    从nav接口的图片URL派生WBI密钥

    Args:
        img_url: wbi_img.img_url
        sub_url: wbi_img.sub_url

    Returns:
        str: 32位WBI密钥
    """
    img_key = img_url.rsplit("/", 1)[1].split(".")[0]
    sub_key = sub_url.rsplit("/", 1)[1].split(".")[0]
    mixed_key = img_key + sub_key
    return functools.reduce(
        lambda s, i: s + mixed_key[i], MIXIN_KEY_ENC_TAB, ""
    )[:32]


def wbi_sign(params: str, w_key: str, wts: Optional[str] = None) -> str:
    """
    生成WBI签名

    Args:
        params: 已排序拼接的参数字符串
        w_key: WBI密钥
        wts: 时间戳，默认当前时间

    Returns:
        str: w_rid签名（密钥为空时返回空串）
    """
    if wts is None:
        wts = str(int(time.time()))
    if not w_key:
        return ""
    return hashlib.md5(
        (params + "&wts=" + wts + w_key).encode(encoding="utf-8")
    ).hexdigest()


class WbiSigner:
    """
    WBI签名器

    负责密钥的获取与缓存（密钥每日于UTC+8零点刷新）。
    内部使用同步requests，调用方在事件循环中时应通过to_thread调用。
    """

    def __init__(self, headers: Optional[dict] = None):
        """
        初始化签名器

        Args:
            headers: 请求头（默认使用Chrome UA）
        """
        self._headers = headers or dict(DEFAULT_HEADERS)
        self._w_key: Optional[str] = None
        self._w_key_expire: float = 0

    def get_w_key(self, force_refresh: bool = False) -> str:
        """
        获取WBI密钥（带缓存）

        Args:
            force_refresh: 是否强制刷新

        Returns:
            str: WBI密钥，获取失败返回空串
        """
        now = time.time()
        if not force_refresh and self._w_key and now < self._w_key_expire:
            return self._w_key

        try:
            response = requests.get(
                "https://api.bilibili.com/x/web-interface/nav",
                headers=self._headers,
                timeout=10
            )
            data = response.json()
            wbi_img = data["data"]["wbi_img"]
            self._w_key = derive_wbi_key(wbi_img["img_url"], wbi_img["sub_url"])
            # 下一个UTC+8零点刷新
            self._w_key_expire = now + 86400 - (now + 8 * 3600) % 86400
            return self._w_key
        except Exception as e:
            logger.error(f"获取WBI密钥失败: {e}")
            return ""

    def sign(self, params: str, wts: Optional[str] = None) -> str:
        """
        生成WBI签名（自动获取密钥）

        Args:
            params: 已排序拼接的参数字符串
            wts: 时间戳，默认当前时间

        Returns:
            str: w_rid签名
        """
        return wbi_sign(params, self.get_w_key(), wts)

    def sign_url_params(self, params: str) -> Tuple[str, str]:
        """
        签名URL参数并返回签名与时间戳（供 ?...&wts={wts}&w_rid={w_rid} 拼接）

        Args:
            params: 已排序拼接的参数字符串（不含wts/w_rid）

        Returns:
            Tuple[str, str]: (w_rid, wts)；密钥获取失败时w_rid为空串
        """
        wts = str(int(time.time()))
        return wbi_sign(params, self.get_w_key(), wts), wts
