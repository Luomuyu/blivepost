# -*- coding: utf-8 -*-
"""
直播相关数据模型
"""

from dataclasses import dataclass
from typing import Optional
from datetime import datetime


@dataclass
class LiveRoomInfo:
    """直播间信息"""
    uid: int
    name: str
    title: str
    cover: str
    room_id: int
    online: str
    
    def __post_init__(self):
        """确保数据类型正确"""
        self.uid = int(self.uid) if self.uid else 0
        self.room_id = int(self.room_id) if self.room_id else 0


@dataclass
class LiveStats:
    """直播统计数据"""
    danmaku_count: int = 0
    gold_count: float = 0.0  # 元数
    ship_count: float = 0.0  # 元数
    sc_count: int = 0  # 元
    like_count: int = 0
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    
    def reset(self) -> None:
        """重置所有统计数据"""
        self.danmaku_count = 0
        self.gold_count = 0.0
        self.ship_count = 0.0
        self.sc_count = 0
        self.like_count = 0
        self.start_time = None
        self.end_time = None
    
    @property
    def duration_seconds(self) -> int:
        """直播时长（秒）"""
        if self.start_time is None:
            return 0
        end = self.end_time or datetime.now()
        return int((end - self.start_time).total_seconds())
    
    @property
    def duration_str(self) -> str:
        """直播时长字符串"""
        seconds = self.duration_seconds
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours}小时{minutes}分钟{secs}秒"
    
    def add_gift(self, price: int, coin_type: str) -> None:
        """
        添加礼物收入
        
        Args:
            price: 礼物总价（瓜子数）
            coin_type: 瓜子类型 (gold/silver)
        """
        if coin_type == "gold":
            self.gold_count += price / 1000  # 转换为元
    
    def add_ship(self, price: int) -> None:
        """添加上舰收入"""
        self.ship_count += price / 1000
    
    def add_sc(self, price: int) -> None:
        """添加SC收入"""
        self.sc_count += price
    
    def add_red_pocket(self, total_price: int) -> None:
        """添加红包收入（电池转换为瓜子估算）"""
        self.gold_count += total_price * 0.25


@dataclass
class TempData:
    """临时数据存储（用于去重）"""
    popular: int = 0
    watched: int = 0
    like: int = 0
    
    def should_update_popular(self, value: int) -> bool:
        """检查是否应该更新人气值"""
        if value == 1 or value == self.popular:
            return False
        self.popular = value
        return True
    
    def should_update_watched(self, value: int) -> bool:
        """检查是否应该更新观看人数"""
        if value == self.watched:
            return False
        self.watched = value
        return True
    
    def should_update_like(self, value: int) -> bool:
        """检查是否应该更新点赞数"""
        if value == self.like:
            return False
        self.like = value
        return True


@dataclass
class UserInfo:
    """用户信息"""
    uid: int
    uname: str
    face: str = ""
    level: int = 0
    medal_name: str = ""
    medal_level: int = 0
    
    def __post_init__(self):
        """确保数据类型正确"""
        self.uid = int(self.uid) if self.uid else 0
        self.level = int(self.level) if self.level else 0
        self.medal_level = int(self.medal_level) if self.medal_level else 0


@dataclass
class DanmakuInfo:
    """弹幕信息"""
    uid: int
    uname: str
    msg: str
    medal_name: str = ""
    medal_level: int = 0
    timestamp: int = 0
    
    def __post_init__(self):
        """确保数据类型正确"""
        self.uid = int(self.uid) if self.uid else 0
        self.medal_level = int(self.medal_level) if self.medal_level else 0
        self.timestamp = int(self.timestamp) if self.timestamp else 0


@dataclass
class GiftInfo:
    """礼物信息"""
    uid: int
    uname: str
    gift_name: str
    gift_id: int
    num: int
    price: int
    coin_type: str = "gold"
    
    def __post_init__(self):
        """确保数据类型正确"""
        self.uid = int(self.uid) if self.uid else 0
        self.gift_id = int(self.gift_id) if self.gift_id else 0
        self.num = int(self.num) if self.num else 0
        self.price = int(self.price) if self.price else 0
    
    @property
    def total_price(self) -> int:
        """总价"""
        return self.price * self.num


@dataclass
class SuperChatInfo:
    """醒目留言信息"""
    uid: int
    uname: str
    price: int
    message: str
    background_color: str = ""
    timestamp: int = 0
    
    def __post_init__(self):
        """确保数据类型正确"""
        self.uid = int(self.uid) if self.uid else 0
        self.price = int(self.price) if self.price else 0
        self.timestamp = int(self.timestamp) if self.timestamp else 0


@dataclass
class GuardInfo:
    """上舰信息"""
    uid: int
    uname: str
    guard_level: int
    gift_name: str
    price: int
    num: int = 1
    
    def __post_init__(self):
        """确保数据类型正确"""
        self.uid = int(self.uid) if self.uid else 0
        self.guard_level = int(self.guard_level) if self.guard_level else 0
        self.price = int(self.price) if self.price else 0
        self.num = int(self.num) if self.num else 1


@dataclass
class DynamicInfo:
    """动态信息"""
    dynamic_id: str
    author_name: str
    author_uid: int
    content: str
    publish_time: int
    platform: str = "bili"  # bili / weibo
    url: str = ""
    images: list = None
    
    def __post_init__(self):
        """确保数据类型正确"""
        if self.images is None:
            self.images = []
        self.author_uid = int(self.author_uid) if self.author_uid else 0
    
    @property
    def full_url(self) -> str:
        """获取完整URL"""
        if self.url:
            return self.url
        if self.platform == "bili":
            return f"https://www.bilibili.com/opus/{self.dynamic_id}"
        elif self.platform == "weibo":
            return f"https://weibo.com/{self.author_uid}/{self.dynamic_id}"
        return ""
