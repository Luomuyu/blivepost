# -*- coding: utf-8 -*-
"""
Vendor导入器 - 动态导入blivedm库
"""

from typing import Any

from ..utils.logger import get_logger


logger = get_logger("vendor.importer")


def import_blivedm() -> Any:
    """
    动态导入blivedm模块
    
    Returns:
        blivedm模块对象
    """
    try:
        import blivedm
        return blivedm
    except ImportError:
        logger.error("未找到blivedm库，请确保已通过VendorManager安装")
        raise ImportError(
            "blivedm库未安装。请使用 VendorManager.setup_blivedm() 安装，"
            "或手动从 https://github.com/xfgryujk/blivedm 下载"
        )


def import_web_models() -> Any:
    """
    动态导入blivedm的web模型模块
    
    Returns:
        web模型模块对象
    """
    try:
        from blivedm.models import web
        return web
    except ImportError:
        logger.error("未找到blivedm.models.web模块")
        raise ImportError(
            "blivedm.models.web模块未找到。请确保blivedm版本正确。"
        )


def get_blivedm_version() -> str:
    """
    获取blivedm版本
    
    Returns:
        版本字符串
    """
    try:
        blivedm = import_blivedm()
        return getattr(blivedm, "__version__", "unknown")
    except ImportError:
        return "not installed"
