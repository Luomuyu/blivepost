# -*- coding: utf-8 -*-
"""
数据库模型定义
"""

import enum
from typing import Optional

from sqlalchemy import (
    String, Text, Integer, BigInteger, Float,
    Enum, Index, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..utils.timeutil import now_ms


def id_pk_type():
    """
    自增主键（id）列类型

    - SQLite 渲染为 INTEGER：自增依赖 INTEGER PRIMARY KEY 的 rowid 别名，
      渲染为 BIGINT 将导致插入时 NOT NULL 约束失败
    - MySQL / PostgreSQL 渲染为 BIGINT（新库建表用；见下方历史说明）

    历史说明：原 primary_key_column()/set_db_type() 在类定义（import 时）按
    _db_type 求值，而 set_db_type() 在 DatabaseManager 初始化时才调用，对已构建
    的模型无效——"动态主键类型"是永不生效的死代码，已移除，改为静态
    with_variant 声明。对既有库的影响：SQLite 建表 DDL 与原先完全一致
    （INTEGER，无 schema 漂移）；MySQL 老库的既有表不会被 create_all 重建
    （表已存在则跳过，不加列/不改型），仅新建库的主键为 BIGINT，属可接受的
    新库演进——不为此注册会在大表上失败/长时间锁表的 ALTER 改型迁移。
    """
    return BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """主数据库基类（配置选择的库：绑定/账号池/好友/订阅/主播/直播间映射等）"""
    pass


class EventBase(DeclarativeBase):
    """
    直播间事件子数据库基类（sqlite专用，不受config数据库类型控制）

    弹幕/礼物/舰长/SC/天选/红包/大航海成就/系统事件/房间变更/开播下播
    等直播间接收的事件数据，按直播间拆分为独立sqlite子库（见
    database/room_store.py）。

    索引说明：各事件表 room_id 不建单列索引——复合索引 (room_id, timestamp)
    的最左前缀已覆盖按房间查询。timestamp 单列索引仅加在高频分析表
    （danmaku/gift/super_chat/guard/red_pocket，create_all自动建）——
    复合索引最左列是room_id，timestamp-only范围查询（分析/趋势/词云）
    用不上最左前缀。create_all 不会删除旧库中已存在的冗余索引（无害）。
    """
    pass


class EventType(enum.Enum):
    """事件类型枚举"""
    CUT_OFF = "cut_off"        # 被切断
    WARNING = "warning"        # 被警告
    BAN = "ban"               # 被禁言
    ADMIN_SET = "admin_set"    # 设为房管
    ADMIN_REVOKE = "admin_revoke"  # 撤销房管


class DanmakuRecord(EventBase):
    """弹幕记录表"""
    __tablename__ = "danmaku"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    uid: Mapped[int] = mapped_column(BigInteger, index=True, comment="用户ID")
    uname: Mapped[str] = mapped_column(String(100), comment="用户名")
    msg: Mapped[str] = mapped_column(Text, comment="弹幕内容")
    dm_type: Mapped[int] = mapped_column(Integer, default=0, comment="弹幕类型(0普通/1表情)")
    emoticon_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment="表情URL")
    medal_name: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, comment="勋章名")
    medal_level: Mapped[int] = mapped_column(Integer, default=0, comment="勋章等级")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_danmaku_room_time", "room_id", "timestamp"),
        # timestamp单列索引：分析/趋势/词云等timestamp-only范围查询
        # （复合索引最左列是room_id，用不上最左前缀）
        Index("idx_danmaku_timestamp", "timestamp"),
    )


class GiftRecord(EventBase):
    """礼物记录表"""
    __tablename__ = "gift"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    uid: Mapped[int] = mapped_column(BigInteger, index=True, comment="用户ID")
    uname: Mapped[str] = mapped_column(String(100), comment="用户名")
    gift_id: Mapped[int] = mapped_column(Integer, comment="礼物ID")
    gift_name: Mapped[str] = mapped_column(String(100), comment="礼物名称")
    num: Mapped[int] = mapped_column(Integer, comment="礼物数量")
    price: Mapped[int] = mapped_column(Integer, comment="单价(瓜子)")
    total_price: Mapped[int] = mapped_column(Integer, comment="总价(瓜子)")
    coin_type: Mapped[str] = mapped_column(String(10), comment="瓜子类型(gold/silver)")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_gift_room_time", "room_id", "timestamp"),
        Index("idx_gift_timestamp", "timestamp"),
    )


class SuperChatRecord(EventBase):
    """醒目留言(SC)记录表"""
    __tablename__ = "super_chat"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    uid: Mapped[int] = mapped_column(BigInteger, index=True, comment="用户ID")
    uname: Mapped[str] = mapped_column(String(100), comment="用户名")
    price: Mapped[int] = mapped_column(Integer, comment="价格(元)")
    message: Mapped[str] = mapped_column(Text, comment="留言内容")
    background_color: Mapped[Optional[str]] = mapped_column(String(20), nullable=True, comment="背景色")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_sc_room_time", "room_id", "timestamp"),
        Index("idx_sc_timestamp", "timestamp"),
    )


class GuardRecord(EventBase):
    """舰长记录表"""
    __tablename__ = "guard"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    uid: Mapped[int] = mapped_column(BigInteger, index=True, comment="用户ID")
    uname: Mapped[str] = mapped_column(String(100), comment="用户名")
    guard_level: Mapped[int] = mapped_column(Integer, comment="舰长等级(1总督/2提督/3舰长)")
    gift_name: Mapped[str] = mapped_column(String(50), comment="礼物名称")
    price: Mapped[int] = mapped_column(Integer, comment="价格(瓜子)")
    num: Mapped[int] = mapped_column(Integer, default=1, comment="数量")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_guard_room_time", "room_id", "timestamp"),
        Index("idx_guard_timestamp", "timestamp"),
    )


class AnchorRecord(EventBase):
    """天选记录表"""
    __tablename__ = "anchor"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    award_name: Mapped[str] = mapped_column(String(200), comment="奖品名称")
    danmu: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, comment="弹幕要求")
    require_text: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, comment="要求文本")
    require_type: Mapped[int] = mapped_column(Integer, default=0, comment="要求类型")
    require_value: Mapped[str] = mapped_column(String(100), default="", comment="要求值")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_anchor_room_time", "room_id", "timestamp"),
    )


class RedPocketRecord(EventBase):
    """红包记录表"""
    __tablename__ = "red_pocket"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    sender_uid: Mapped[int] = mapped_column(BigInteger, comment="发送者ID")
    sender_name: Mapped[str] = mapped_column(String(100), comment="发送者名")
    total_price: Mapped[int] = mapped_column(Integer, comment="总价值(电池)")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_redpocket_room_time", "room_id", "timestamp"),
        Index("idx_redpocket_timestamp", "timestamp"),
    )


class GuardAchievementRecord(EventBase):
    """大航海成就记录表"""
    __tablename__ = "guard_achievement"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    achieve_level: Mapped[int] = mapped_column(Integer, comment="成就等级")
    content: Mapped[str] = mapped_column(String(500), comment="成就内容")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_achievement_room_time", "room_id", "timestamp"),
    )


class SystemEventRecord(EventBase):
    """系统事件记录表(被切断、被警告、被禁言、设为房管、撤销房管)"""
    __tablename__ = "system_event"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    event_type: Mapped[EventType] = mapped_column(Enum(EventType), comment="事件类型")
    uid: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="相关用户ID")
    uname: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="相关用户名")
    msg: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="消息内容")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_sys_event_room_time", "room_id", "timestamp"),
    )


class RoomChangeRecord(EventBase):
    """房间信息变更记录表"""
    __tablename__ = "room_change"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    title: Mapped[str] = mapped_column(String(200), comment="直播间标题")
    parent_area_id: Mapped[int] = mapped_column(Integer, default=0, comment="父分区ID")
    parent_area_name: Mapped[str] = mapped_column(String(50), comment="父分区名")
    area_id: Mapped[int] = mapped_column(Integer, default=0, comment="分区ID")
    area_name: Mapped[str] = mapped_column(String(50), comment="分区名")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_room_change_room_time", "room_id", "timestamp"),
    )


class GroupBindingRecord(Base):
    """QQ群绑定记录表(群openid与直播间的绑定关系)"""
    __tablename__ = "group_binding"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    # group_openid 无单列索引：唯一约束 (group_openid, room_id) 的最左前缀已覆盖
    group_openid: Mapped[str] = mapped_column(String(64), comment="群openid")
    room_id: Mapped[int] = mapped_column(BigInteger, index=True, comment="直播间ID")
    uid: Mapped[int] = mapped_column(BigInteger, default=0, comment="主播UID")
    monitored: Mapped[int] = mapped_column(Integer, default=0, comment="是否处于监控状态(0否/1是)")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="绑定时间(13位毫秒时间戳)")

    __table_args__ = (
        UniqueConstraint("group_openid", "room_id", name="uq_binding_group_room"),
    )


class CookieAccountRecord(Base):
    """B站账号cookie池表"""
    __tablename__ = "cookie_account"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(BigInteger, unique=True, comment="B站用户UID")
    nickname: Mapped[str] = mapped_column(String(100), default="", comment="账号昵称")
    cookie_string: Mapped[str] = mapped_column(Text, comment="完整cookie字符串(k=v;...)")
    sessdata: Mapped[str] = mapped_column(Text, default="", comment="SESSDATA")
    bili_jct: Mapped[str] = mapped_column(String(100), default="", comment="bili_jct (csrf)")
    ac_time_value: Mapped[str] = mapped_column(
        String(100), default="", comment="ac_time_value (refresh token)")
    sessdata_expires_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="SESSDATA过期时间(13位毫秒时间戳)")
    status: Mapped[str] = mapped_column(String(20), default="active", comment="状态(active/expired)")
    use_count: Mapped[int] = mapped_column(Integer, default=0, comment="被取用次数")
    last_error: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, comment="最近一次失效原因")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="入库时间(13位毫秒时间戳)")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")
    last_used_at: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True, comment="最近一次被取用时间(13位毫秒时间戳)")


class FriendRecord(Base):
    """好友记录表(机器人与用户的联系状态)"""
    __tablename__ = "friend"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    openid: Mapped[str] = mapped_column(String(64), unique=True, comment="用户openid")
    union_openid: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, comment="UnionID体系标识(跨场景一致)")
    nickname: Mapped[str] = mapped_column(String(100), default="", comment="用户昵称")
    del_flag: Mapped[int] = mapped_column(Integer, default=0, comment="是否被删除(0否/1被删)")
    msg_receive: Mapped[int] = mapped_column(Integer, default=1, comment="单聊消息接收(1开/0关)")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="添加时间(13位毫秒时间戳)")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")


class GroupRecord(Base):
    """群记录表(机器人与群的联系状态)"""
    __tablename__ = "bot_group"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    group_openid: Mapped[str] = mapped_column(String(64), unique=True, comment="群openid")
    group_name: Mapped[str] = mapped_column(String(200), default="", comment="群名称")
    group_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="群简介")
    category: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, comment="群分类")
    tags: Mapped[Optional[str]] = mapped_column(Text, nullable=True, comment="群标签列表(JSON数组文本)")
    member_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, comment="群成员人数")
    del_flag: Mapped[int] = mapped_column(Integer, default=0, comment="是否被移除(0否/1被移出)")
    msg_receive: Mapped[int] = mapped_column(Integer, default=1, comment="群消息接收(1开/0关)")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="加入时间(13位毫秒时间戳)")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")


class RoomDbRecord(Base):
    """直播间子数据库映射表（主库仅记录直播间对应的子库文件名）"""
    __tablename__ = "room_db"

    room_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, comment="直播间长号")
    db_name: Mapped[str] = mapped_column(String(100), unique=True, comment="子库文件名(rooms/目录下)")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="创建时间(13位毫秒时间戳)")


class DynamicSeenRecord(EventBase):
    """已见动态记录表（动态列表持久化：发布后删除的旧动态不再触发推送）"""
    __tablename__ = "dynamic_seen"

    dynamic_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="动态ID")
    up_uid: Mapped[int] = mapped_column(BigInteger, index=True, comment="UP主UID")
    summary: Mapped[str] = mapped_column(String(200), default="", comment="动态内容摘要")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="记录时间(13位毫秒时间戳)")


class DynamicBindingRecord(Base):
    """动态订阅记录表(群openid与B站UP主的绑定关系)"""
    __tablename__ = "dynamic_binding"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    # group_openid 无单列索引：唯一约束 (group_openid, up_uid) 的最左前缀已覆盖
    group_openid: Mapped[str] = mapped_column(String(64), comment="群openid")
    up_uid: Mapped[int] = mapped_column(BigInteger, index=True, comment="UP主UID")
    up_name: Mapped[str] = mapped_column(String(100), default="", comment="UP主昵称")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="绑定时间(13位毫秒时间戳)")

    __table_args__ = (
        UniqueConstraint("group_openid", "up_uid", name="uq_dynamic_group_up"),
    )


class StreamerRecord(Base):
    """主播信息表(绑定主播的资料快照：绑定时写入、每日0点get_status_info_by_uids批量刷新)"""
    __tablename__ = "streamer"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(BigInteger, unique=True, comment="主播UID")
    uname: Mapped[str] = mapped_column(String(100), default="", comment="主播昵称")
    face: Mapped[str] = mapped_column(String(255), default="", comment="主播头像url")
    room_id: Mapped[int] = mapped_column(BigInteger, default=0, comment="主直播间长号")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="添加时间(13位毫秒时间戳)")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")


class LiveRoomRecord(Base):
    """直播间信息表(绑定直播间的资料快照，与主播表同源刷新)"""
    __tablename__ = "live_room"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, unique=True, comment="直播间长号")
    uid: Mapped[int] = mapped_column(BigInteger, index=True, default=0, comment="主播UID")
    title: Mapped[str] = mapped_column(String(200), default="", comment="直播间标题")
    short_id: Mapped[int] = mapped_column(BigInteger, default=0, comment="短号(0为无)")
    live_status: Mapped[int] = mapped_column(Integer, default=0, comment="开播状态(0未开/1直播/2轮播)")
    area_name: Mapped[str] = mapped_column(String(50), default="", comment="分区名")
    parent_area_name: Mapped[str] = mapped_column(String(50), default="", comment="父分区名")
    cover: Mapped[str] = mapped_column(String(255), default="", comment="直播间封面url")
    keyframe: Mapped[str] = mapped_column(String(255), default="", comment="关键帧url")
    online: Mapped[int] = mapped_column(BigInteger, default=0, comment="在线人数")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="添加时间(13位毫秒时间戳)")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")


class LiveStatusRecord(EventBase):
    """开播/下播记录表"""
    __tablename__ = "live_status"

    id: Mapped[int] = mapped_column(id_pk_type(), primary_key=True, autoincrement=True)
    room_id: Mapped[int] = mapped_column(BigInteger, comment="直播间ID")
    status: Mapped[str] = mapped_column(String(20), comment="状态(live_start/live_end)")
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, comment="直播间标题")
    cover: Mapped[Optional[str]] = mapped_column(String(500), nullable=True, comment="封面URL")
    # 下播统计数据
    danmaku_count: Mapped[int] = mapped_column(Integer, default=0, comment="弹幕数")
    gold_count: Mapped[float] = mapped_column(Float, default=0, comment="礼物收入(元)")
    ship_count: Mapped[float] = mapped_column(Float, default=0, comment="上舰收入(元)")
    sc_count: Mapped[int] = mapped_column(Integer, default=0, comment="SC收入(元)")
    duration: Mapped[int] = mapped_column(Integer, default=0, comment="直播时长(秒)")
    start_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="开播时间(13位毫秒时间戳)")
    end_time: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, comment="下播时间(13位毫秒时间戳)")
    timestamp: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="记录时间(13位毫秒时间戳)")
    
    __table_args__ = (
        Index("idx_live_status_room_time", "room_id", "timestamp"),
    )


class WebUserRecord(Base):
    """Web面板前台用户表（游客只读，登录后可控；管理员来自config不入库）"""
    __tablename__ = "web_user"

    username: Mapped[str] = mapped_column(String(64), primary_key=True, comment="用户名")
    password_hash: Mapped[str] = mapped_column(Text, comment="pbkdf2口令哈希")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="创建时间(13位毫秒时间戳)")


class PushSwitchRecord(Base):
    """全局推送开关表（运行时可改：后台设置状态与是否允许前台修改）"""
    __tablename__ = "push_switch"

    name: Mapped[str] = mapped_column(String(32), primary_key=True, comment="开关名")
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="0关/1开")
    allow_modify: Mapped[int] = mapped_column(Integer, nullable=False, default=0, comment="前台可改0/1")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")


class GroupAtAllRecord(Base):
    """群艾特全体配置与使用记录。

    mode: at_all模式（第十三批用户决策）——
      daily=每日首次开播提醒艾特（默认）
      rooms=仅指定房间开播时艾特（room_ids列）
      off=本群关闭艾特全体
    used_date: 每群每自然日仅1次配额的判定依据。
    """
    __tablename__ = "group_at_all"

    group_openid: Mapped[str] = mapped_column(String(64), primary_key=True, comment="群openid")
    mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="daily",
        comment="at_all模式: daily/rooms/off")
    room_ids: Mapped[str] = mapped_column(
        String(500), nullable=False, default="",
        comment="rooms模式下艾特的房间ID列表(逗号分隔)")
    used_date: Mapped[str] = mapped_column(String(10), nullable=False, comment="最近使用日期YYYY-MM-DD")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")


class GroupPushSettingRecord(EventBase):
    """群推送设置表（房间子库：每个绑定群对本房间各推送项的独立开关；
    行缺省时回落全局开关，创建时默认继承全局）"""
    __tablename__ = "group_push_setting"

    group_openid: Mapped[str] = mapped_column(String(64), primary_key=True, comment="群openid")
    anchor: Mapped[int] = mapped_column(Integer, default=0, comment="天选时刻推送")
    guard_achievement: Mapped[int] = mapped_column(Integer, default=0, comment="大航海成就推送")
    system_event: Mapped[int] = mapped_column(Integer, default=0, comment="系统事件推送")
    room_change: Mapped[int] = mapped_column(Integer, default=0, comment="房间变更推送")
    daily_report: Mapped[int] = mapped_column(Integer, default=0, comment="每日结算推送")
    weekly_report: Mapped[int] = mapped_column(Integer, default=0, comment="每周结算推送")
    monthly_report: Mapped[int] = mapped_column(Integer, default=0, comment="每月结算推送")
    created_at: Mapped[int] = mapped_column(BigInteger, default=now_ms, comment="创建时间(13位毫秒时间戳)")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, default=now_ms, onupdate=now_ms, comment="更新时间(13位毫秒时间戳)")
