# -*- coding: utf-8 -*-
"""
Vendor模块 - 管理第三方库依赖
"""

from .manager import VendorManager, VendorSpec, setup_blivedm
from .importer import import_blivedm, import_web_models
from .patch import apply_patch

__all__ = [
    "VendorManager",
    "VendorSpec",
    "setup_blivedm",
    "import_blivedm",
    "import_web_models",
    "apply_patch",
]
