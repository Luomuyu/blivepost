# -*- coding: utf-8 -*-
"""
botpy 私有属性适配层

qqbot.py 需直接调用 botpy 的内部 HTTP 通道（`client.api._http.request`）——
官方 botpy 未暴露通用请求接口，所有非预定义端点（素材管理/群角色查询/
二进制上传等）只能走此通道。botpy 任何版本升级若改变 `_http` 结构，
只需修改本模块，调用方代码零改动。

qq-botpy-v2 版本钉住区间：>=1.0.1,<2.0.0（见pyproject.toml）。
"""

from typing import Any, Optional

from ..utils.logger import get_logger

logger = get_logger("botpy_compat")

# 已验证的botpy内部结构签名（升级时需重新核对）
_EXPECTED_ATTRS = ("request",)


def get_http(client: Any) -> Optional[Any]:
    """安全提取 botpy 客户端的内部 HTTP 请求通道。

    Args:
        client: botpy Client实例（或任何有 .api._http.request 的对象）

    Returns:
        具备 async request(route, **kwargs) 的对象；结构不符返回None
        （调用方应降级或跳过该功能，不应崩溃）。
    """
    try:
        http = client.api._http
        # 验证必要方法存在
        for attr in _EXPECTED_ATTRS:
            if not hasattr(http, attr):
                logger.warning(
                    f"botpy内部HTTP通道缺少方法 {attr}——"
                    f"botpy版本可能已升级，请核对qq-botpy-v2版本区间")
                return None
        return http
    except AttributeError as e:
        logger.warning(f"botpy内部结构访问失败（版本不兼容？）: {e}")
        return None


async def api_request(client: Any, route, **kwargs) -> Optional[dict]:
    """经 botpy 内部通道发起 API 请求（统一入口）。

    Args:
        client: botpy Client实例
        route: botpy.http.Route实例
        **kwargs: 透传给 http.request（json=、data=等）

    Returns:
        dict: 响应JSON；通道不可用/请求失败返回None
    """
    http = get_http(client)
    if http is None:
        return None
    try:
        res = await http.request(route, **kwargs)
        return res if isinstance(res, dict) else {"_raw": res}
    except Exception as e:
        logger.warning(f"botpy API请求失败({getattr(route, 'path', route)}): {e}")
        return None
