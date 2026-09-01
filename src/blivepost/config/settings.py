# -*- coding: utf-8 -*-
"""
配置管理模块
"""

from dataclasses import dataclass, field
from typing import List, Optional
from pathlib import Path

import yaml


def _as_str_list(value) -> List[str]:
    """配置值转字符串列表（兼容单个字符串）"""
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value or []]


@dataclass
class QQBotConfig:
    """QQ官方机器人配置（botpy）"""
    app_id: str = ""
    app_secret: str = ""


@dataclass
class LiveConfig:
    """直播间配置"""
    room_id: str = ""


@dataclass
class MessageSettings:
    """
    消息推送设置

    弹幕/礼物/舰长/SC/红包仅入库不推送；开播/下播始终推送不设开关。
    """
    anchor: bool = False
    guard_achievement: bool = True
    system_event: bool = True
    room_change: bool = False
    dynamic_monitor: bool = False
    daily_report: bool = False
    weekly_report: bool = False
    monthly_report: bool = False
    web_panel: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = 8080
    web_token: str = ""
    ignore_cmds: List[str] = field(default_factory=list)


@dataclass
class RedisConfig:
    """Redis配置"""
    host: str = "localhost"
    port: int = 6379
    password: str = ""
    db: int = 0


@dataclass
class DatabaseConfig:
    """数据库配置"""
    type: str = "sqlite"
    sqlite_path: str = "data/blivepost.db"
    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "blivepost"


@dataclass
class AuthConfig:
    """认证配置"""
    sessdata: str = ""


@dataclass
class AdminConfig:
    """管理员配置（私聊命令权限与通知分级）"""
    openids: List[str] = field(default_factory=list)
    # 主管理员：接收Cookie失效等敏感通知；未配置时回落到openids全部管理员
    main_openids: List[str] = field(default_factory=list)
    # 群控制指令（绑定/监控/取消监控/解绑）由openids与main_openids合并名单控制


@dataclass
class TemplatesConfig:
    """推送文案模板（空串使用内置默认模板，占位符见push.py各方法）"""
    live_start: str = ""
    live_end: str = ""
    anchor: str = ""
    guard_achievement: str = ""
    cut_off: str = ""
    warning: str = ""
    daily_report: str = ""


@dataclass
class Settings:
    """应用配置"""
    qqbot: QQBotConfig = field(default_factory=QQBotConfig)
    live: LiveConfig = field(default_factory=LiveConfig)
    message: MessageSettings = field(default_factory=MessageSettings)
    redis: RedisConfig = field(default_factory=RedisConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    templates: TemplatesConfig = field(default_factory=TemplatesConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    
    _config_path: Optional[Path] = field(default=None, repr=False)
    
    @classmethod
    def load(cls, config_path: Optional[str] = None) -> "Settings":
        """
        从YAML文件加载配置
        
        Args:
            config_path: 配置文件路径，默认为当前目录下的config.yaml
            
        Returns:
            Settings: 配置对象
        """
        if config_path is None:
            config_path = "config.yaml"
        
        path = Path(config_path)
        
        if not path.exists():
            cls._create_default_config(path)
            raise FileNotFoundError(
                f"配置文件 {path} 不存在，已创建默认配置文件，请修改后重新运行"
            )
        
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        
        settings = cls._parse_config(data)
        settings._config_path = path
        
        return settings
    
    @classmethod
    def _parse_config(cls, data: dict) -> "Settings":
        """解析配置数据"""
        qqbot_data = data.get("QQBot", {})
        live_data = data.get("Live", {})
        setting_data = data.get("Setting", {})
        redis_data = data.get("Redis", {})
        database_data = data.get("Database", {})
        auth_data = data.get("Auth", {})
        templates_data = data.get("Template", {})
        admin_data = data.get("Admin", {})

        return cls(
            qqbot=QQBotConfig(
                app_id=str(qqbot_data.get("app_id", "")),
                app_secret=str(qqbot_data.get("app_secret", "")),
            ),
            live=LiveConfig(
                room_id=live_data.get("live_id", ""),
            ),
            message=MessageSettings(
                anchor=setting_data.get("anchor", "N") == "Y",
                guard_achievement=setting_data.get("guard_achievement", "Y") == "Y",
                system_event=setting_data.get("system_event", "Y") == "Y",
                room_change=setting_data.get("room_change", "N") == "Y",
                dynamic_monitor=setting_data.get("dynamic_monitor", "N") == "Y",
                daily_report=setting_data.get("daily_report", "N") == "Y",
                weekly_report=setting_data.get("weekly_report", "N") == "Y",
                monthly_report=setting_data.get("monthly_report", "N") == "Y",
                web_panel=setting_data.get("web_panel", "N") == "Y",
                web_host=str(setting_data.get("web_host", "127.0.0.1")),
                web_port=int(setting_data.get("web_port", 8080)),
                web_token=str(setting_data.get("web_token", "")),
                ignore_cmds=setting_data.get("ignore_cmds", []),
            ),
            redis=RedisConfig(
                host=redis_data.get("host", "localhost"),
                port=redis_data.get("port", 6379),
                password=redis_data.get("password", ""),
                db=redis_data.get("db", 0),
            ),
            database=DatabaseConfig(
                type=database_data.get("type", "sqlite"),
                sqlite_path=database_data.get("sqlite_path", "data/blivepost.db"),
                host=database_data.get("host", "localhost"),
                port=database_data.get("port", 3306),
                user=database_data.get("user", "root"),
                password=database_data.get("password", ""),
                database=database_data.get("database", "blivepost"),
            ),
            auth=AuthConfig(
                sessdata=auth_data.get("sessdata", ""),
            ),
            templates=TemplatesConfig(
                **{k: str(templates_data.get(k, "")) for k in
                   ("live_start", "live_end", "anchor",
                    "guard_achievement", "cut_off", "warning", "daily_report")}
            ),
            admin=AdminConfig(
                openids=_as_str_list(admin_data.get("openids", [])),
                main_openids=_as_str_list(admin_data.get("main_openids", [])),
            ),
        )
    
    @staticmethod
    def _create_default_config(path: Path) -> None:
        """创建默认配置文件"""
        default_config = """# BLivePost 配置文件
# 版本: 1.0.0

Admin: # 管理员设置
  openids: [] # 管理员用户openId列表 (私聊命令、普通通知接收者)
  main_openids: [] # 主管理员openId列表 (Cookie失效等敏感通知接收者, 留空则发全部管理员)

QQBot: # QQ官方机器人设置 (基于botpy)
  app_id: "" # 机器人AppID
  app_secret: "" # 机器人AppSecret
  # 推送目标群通过群指令建立绑定（@机器人发送 /绑定 房间号），无需在此配置

# 动态监控通过群指令订阅（@机器人 /绑定动态 UP主UID），无需配置

Live: # 直播间设置
  live_id: "" # 直播间房间号

Setting: # 消息推送设置
  # 弹幕/礼物/舰长/SC/红包 仅记录日志并存入数据库，不推送消息
  anchor: "N" # 天选时刻推送
  guard_achievement: "Y" # 大航海成就推送
  system_event: "Y" # 系统事件推送 (切断/警告/禁言/房管)
  room_change: "N" # 房间信息变更推送
  dynamic_monitor: "N" # 动态监控总开关 (关闭时/绑定动态 不可用)
  daily_report: "N" # 每日结算推送 (每天00:05, 仅推有数据的房间, 消耗主动消息配额)
  weekly_report: "N" # 每周结算推送 (每周一00:05推送上周汇总)
  monthly_report: "N" # 每月结算推送 (每月1日00:05推送上月汇总)
  web_panel: "N" # Web状态面板 (只读, 默认仅本机访问)
  web_host: "127.0.0.1" # Web面板监听地址
  web_port: 8080 # Web面板端口
  web_token: "" # Web面板访问令牌 (留空不鉴权; 设置后需 ?token= 或 X-Web-Token 头)
  ignore_cmds: [] # 忽略的未知命令列表 (不打印日志)

Redis: # Redis配置 (可选)
  host: "localhost"
  port: 6379
  password: ""
  db: 0

Database: # 数据库配置
  type: "sqlite" # 数据库类型: sqlite, mysql, postgresql
  # SQLite配置 (type为sqlite时使用)
  sqlite_path: "data/blivepost.db" # SQLite数据库文件路径
  # MySQL/PostgreSQL配置 (type为mysql或postgresql时使用)
  host: "localhost"
  port: 3306 # MySQL默认3306, PostgreSQL默认5432
  user: "root"
  password: ""
  database: "blivepost"

Auth: # 认证配置
  sessdata: "" # 默认的B站登录凭证 (可被Redis覆盖)

Template: # 推送文案模板 (可选, 留空使用内置模板; str.format占位符, 可用字段见README)
  # 开播模板占位符: {name} {title} {room_id} {online} {time} {cover} {url}
  live_start: ""
  # 下播模板额外占位符: {like_count} {duration} {danmaku_count} {gold_count} {ship_count} {sc_count}
  live_end: ""
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_config, encoding="utf-8")
    
    def reload(self) -> "Settings":
        """重新加载配置"""
        if self._config_path is None:
            raise ValueError("配置文件路径未知，无法重新加载")
        return Settings.load(str(self._config_path))
