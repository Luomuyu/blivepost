# -*- coding: utf-8 -*-
"""
自定义blivedm消息模型
"""

import dataclasses


@dataclasses.dataclass
class WatchedChange:
    """观看变更"""
    num: int = 0
    """详细观看人数"""
    text_small: str = ''
    """万计观看人数"""
    text_large: str = ''
    """显示信息"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            num=data['num'],
            text_small=data['text_small'],
            text_large=data['text_large'],
        )


@dataclasses.dataclass
class LoginnoticeCmd:
    """未登录账号"""
    info: str = ''
    """账号未登录提示信息"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            info=data['cmd'],
        )


@dataclasses.dataclass
class RoomChange:
    """房间信息更改"""
    title: str = ''
    """标题"""
    area_id: int = ''
    """子分区ID"""
    parent_area_id: int = ''
    """主分区ID"""
    area_name: str = ''
    """子分区"""
    parent_area_name: str = ''
    """主分区"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            title=data['title'],
            area_name=data['area_name'],
            parent_area_name=data['parent_area_name'],
        )


@dataclasses.dataclass
class LiveStart:
    """开播"""
    cmd: str = ''
    """命令"""
    roomid: int = 0
    """房间ID"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            cmd=data['cmd'],
            roomid=data['roomid'],
        )


@dataclasses.dataclass
class LivePreparing:
    """下播"""
    cmd: str = ''
    """命令"""
    roomid: int = 0
    """房间ID"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            cmd=data['cmd'],
            roomid=data['roomid'],
        )


@dataclasses.dataclass
class AnchorStart:
    """天选"""
    award_name: str = ''
    """礼物"""
    danmu: str = ''
    """弹幕"""
    require_text: str = ''
    """要求"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            award_name=data['award_name'],
            danmu=data['danmu'],
            require_text=data['require_text'],
        )


@dataclasses.dataclass
class RedPocketStart:
    """红包"""
    sender_name: str = ''
    """发送人"""
    total_price: int = 0
    """总价"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            sender_name=data['sender_name'],
            total_price=data['total_price'],
        )


@dataclasses.dataclass
class GuardAchievementRoom:
    """舰队成就"""
    anchor_guard_achieve_level: str = ''
    """舰队数量"""
    first_line_content: str = ''
    """信息1"""
    second_line_content: str = ''
    """信息2"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            anchor_guard_achieve_level=data['anchor_guard_achieve_level'],
            first_line_content=data['first_line_content'],
            second_line_content=data['second_line_content'],
        )


@dataclasses.dataclass
class CutOff:
    """被切"""
    msg: str = ''
    """被切原因"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            msg=data['msg'],
        )


@dataclasses.dataclass
class Warned:
    """警告"""
    msg: str = ''
    """警告原因"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            msg=data['msg'],
        )


@dataclasses.dataclass
class LikeUpdate:
    """点赞数更新"""
    click_count: int = 0
    """点赞数"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            click_count=data['click_count'],
        )


@dataclasses.dataclass
class PKStart:
    """PK开始"""
    uname: str = ''
    """用户名"""
    uid: int = 0
    """用户ID"""
    room_id: int = 0
    """房间ID"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            uname=data['uname'],
            uid=data['uid'],
            room_id=data['room_id'],
        )


@dataclasses.dataclass
class RoomBlock:
    """有人被禁言"""
    uname: str = ''
    """用户名"""
    uid: int = 0
    """用户ID"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            uname=data['uname'],
            uid=data['uid'],
        )


@dataclasses.dataclass
class AdminEntrance:
    """设置房管"""
    uid: int = 0
    """用户ID"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            uid=data['uid'],
        )


@dataclasses.dataclass
class AdminRevoke:
    """撤销房管"""
    uid: int = 0
    """用户ID"""

    @classmethod
    def from_command(cls, data: dict):
        return cls(
            uid=data['uid'],
        )
