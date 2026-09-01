# -*- coding: utf-8 -*-
"""
环境变量与.env文件配置覆盖

优先级（高→低）：真实环境变量 > .env文件 > config.yaml > 默认值。
支持的键（前缀BLIVEPOST_，详见ENV_MAP）：
    BLIVEPOST_QQBOT_APP_ID / BLIVEPOST_QQBOT_APP_SECRET
    BLIVEPOST_ADMIN_OPENIDS / BLIVEPOST_ADMIN_MAIN_OPENIDS  (逗号分隔)
    BLIVEPOST_SESSDATA
    BLIVEPOST_WEB_TOKEN / BLIVEPOST_WEB_PORT
    BLIVEPOST_DB_TYPE / BLIVEPOST_DB_HOST / BLIVEPOST_DB_PORT
    BLIVEPOST_DB_USER / BLIVEPOST_DB_PASSWORD / BLIVEPOST_DB_NAME
    BLIVEPOST_REDIS_HOST / BLIVEPOST_REDIS_PORT / BLIVEPOST_REDIS_PASSWORD
"""

import os
from pathlib import Path
from typing import Dict, Optional


ENV_MAP: Dict[str, str] = {
    "BLIVEPOST_QQBOT_APP_ID": "qqbot.app_id",
    "BLIVEPOST_QQBOT_APP_SECRET": "qqbot.app_secret",
    "BLIVEPOST_ADMIN_OPENIDS": "admin.openids",
    "BLIVEPOST_ADMIN_MAIN_OPENIDS": "admin.main_openids",
    "BLIVEPOST_SESSDATA": "auth.sessdata",
    "BLIVEPOST_WEB_TOKEN": "message.web_token",
    "BLIVEPOST_WEB_PORT": "message.web_port",
    "BLIVEPOST_DB_TYPE": "database.type",
    "BLIVEPOST_DB_HOST": "database.host",
    "BLIVEPOST_DB_PORT": "database.port",
    "BLIVEPOST_DB_USER": "database.user",
    "BLIVEPOST_DB_PASSWORD": "database.password",
    "BLIVEPOST_DB_NAME": "database.database",
    "BLIVEPOST_DB_SQLITE_PATH": "database.sqlite_path",
    "BLIVEPOST_REDIS_HOST": "redis.host",
    "BLIVEPOST_REDIS_PORT": "redis.port",
    "BLIVEPOST_REDIS_PASSWORD": "redis.password",
}


def load_dotenv(path: str = ".env") -> Dict[str, str]:
    """
    读取.env文件中的KEY=VALUE对（不覆盖已存在的环境变量）

    Args:
        path: .env文件路径，文件不存在返回空dict

    Returns:
        Dict[str, str]: 解析出的键值对
    """
    result: Dict[str, str] = {}
    env_file = Path(path)
    if not env_file.exists():
        return result
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key:
                continue
            result[key] = value
            # .env不覆盖真实环境变量
            os.environ.setdefault(key, value)
    except Exception as e:
        from ..utils.logger import get_logger
        get_logger("env").warning(f"读取.env失败: {e}")
    return result


def _set_by_path(settings, dotted: str, value) -> None:
    """按'a.b.c'路径设置settings对象的嵌套属性"""
    parts = dotted.split(".")
    obj = settings
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def _coerce(dotted: str, raw: str):
    """按目标字段当前类型转换字符串值"""
    if "openids" in dotted:
        return [v.strip() for v in raw.split(",") if v.strip()]
    return raw


def apply_env_overrides(settings, environ: Optional[Dict[str, str]] = None) -> int:
    """
    将环境变量覆盖到Settings（仅覆盖有值且非空的键）

    Args:
        settings: Settings实例
        environ: 环境变量来源（默认os.environ，测试可注入）

    Returns:
        int: 实际覆盖的键数量
    """
    environ = os.environ if environ is None else environ
    applied = 0
    for env_key, dotted in ENV_MAP.items():
        raw = environ.get(env_key)
        if raw is None or raw == "":
            continue
        try:
            current = None
            obj = settings
            for part in dotted.split("."):
                obj = getattr(obj, part)
                current = obj
            if isinstance(current, int) and not isinstance(current, bool):
                _set_by_path(settings, dotted, int(raw))
            else:
                _set_by_path(settings, dotted, _coerce(dotted, raw))
            applied += 1
        except (AttributeError, ValueError) as e:
            from ..utils.logger import get_logger
            get_logger("env").warning(f"环境变量{env_key}覆盖失败: {e}")
    return applied
