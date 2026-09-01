# -*- coding: utf-8 -*-
"""
B站账号Cookie池

维护多账号cookie（内存缓存+数据库持久化），供监控子进程取用：
- acquire：按最近使用时间轮转取用active账号，避免单账号高频使用
- upsert：扫码登录/维护刷新后写入（同uid更新）
- report_invalid：标记失效（仅在管理员私聊提醒，不影响群推送）
- needs_refresh：临近过期判断（SESSDATA剩余有效期不足或长期未刷新）
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional, TYPE_CHECKING

from ..database.models import CookieAccountRecord, local_now
from ..utils.logger import get_logger


if TYPE_CHECKING:
    from ..database.service import DatabaseService


logger = get_logger("cookie_pool")

# SESSDATA默认有效期约180天；剩余不足此天数视为临近过期
REFRESH_AHEAD_DAYS = 7
# 无过期时间信息时的兜底刷新周期（天）
REFRESH_INTERVAL_DAYS = 25


@dataclass
class CookieAccount:
    """账号cookie信息"""
    uid: int
    nickname: str = ""
    cookie_string: str = ""
    sessdata: str = ""
    bili_jct: str = ""
    ac_time_value: str = ""
    sessdata_expires_at: Optional[datetime] = None
    status: str = "active"
    last_error: Optional[str] = None
    updated_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None

    @classmethod
    def from_record(cls, rec: CookieAccountRecord) -> "CookieAccount":
        return cls(
            uid=rec.uid,
            nickname=rec.nickname,
            cookie_string=rec.cookie_string,
            sessdata=rec.sessdata,
            bili_jct=rec.bili_jct,
            ac_time_value=rec.ac_time_value,
            sessdata_expires_at=rec.sessdata_expires_at,
            status=rec.status,
            last_error=rec.last_error,
            updated_at=rec.updated_at,
            last_used_at=rec.last_used_at,
        )


class CookiePool:
    """B站账号Cookie池"""

    def __init__(self, db_service: "DatabaseService", warn_threshold: int = 2):
        """
        初始化Cookie池

        Args:
            db_service: 数据库存储服务
            warn_threshold: 可用账号耗尽预警阈值（剩余不多于该数量时提醒）
        """
        self._db = db_service
        self.warn_threshold = warn_threshold
        # {uid: CookieAccount}（含expired，便于查询）
        self._accounts = {}
        # 预警去重：记录上次提醒时的可用账号数（-1表示未提醒过）
        self._last_warned_level: int = -1

    async def load(self) -> int:
        """
        从数据库加载全部账号到内存

        Returns:
            int: 账号数量
        """
        records = await self._db.get_cookie_accounts()
        self._accounts = {rec.uid: CookieAccount.from_record(rec) for rec in records}
        active = sum(1 for a in self._accounts.values() if a.status == "active")
        logger.info(f"Cookie池已加载 {len(self._accounts)} 个账号（active: {active}）")
        return len(self._accounts)

    @property
    def accounts(self) -> List[CookieAccount]:
        """全部账号"""
        return list(self._accounts.values())

    def active_accounts(self) -> List[CookieAccount]:
        """全部active账号"""
        return [a for a in self._accounts.values() if a.status == "active"]

    def size(self) -> int:
        """账号总数"""
        return len(self._accounts)

    async def acquire(self) -> Optional[CookieAccount]:
        """
        轮转取用一个active账号（最近使用时间最早者优先）

        Returns:
            Optional[CookieAccount]: 账号信息，池空或无active账号返回None
        """
        active = self.active_accounts()
        if not active:
            return None

        account = min(
            active,
            key=lambda a: a.last_used_at or datetime.min,
        )
        account.last_used_at = local_now()
        await self._db.touch_cookie_account(account.uid)
        logger.info(f"Cookie池取用账号 {account.uid}（{account.nickname}）")
        return account

    async def upsert(
        self,
        uid: int,
        nickname: str,
        cookie_string: str,
        sessdata: str = "",
        bili_jct: str = "",
        ac_time_value: str = "",
        sessdata_expires_at: Optional[datetime] = None
    ) -> CookieAccount:
        """
        写入/更新账号cookie（登录成功或维护刷新后调用，重置为active）

        Args:
            uid: B站用户UID
            nickname: 昵称
            cookie_string: 完整cookie字符串
            sessdata: SESSDATA（未提供时从cookie_string解析）
            bili_jct: csrf（未提供时从cookie_string解析）
            ac_time_value: refresh token（未提供时从cookie_string解析）
            sessdata_expires_at: SESSDATA过期时间

        Returns:
            CookieAccount: 更新后的账号信息
        """
        def _from_cookie(name: str) -> str:
            if not cookie_string:
                return ""
            for part in cookie_string.split(";"):
                part = part.strip()
                if part.startswith(name + "="):
                    return part[len(name) + 1:]
            return ""

        sessdata = sessdata or _from_cookie("SESSDATA")
        bili_jct = bili_jct or _from_cookie("bili_jct")
        ac_time_value = ac_time_value or _from_cookie("ac_time_value")

        await self._db.save_cookie_account(
            uid=uid, nickname=nickname, cookie_string=cookie_string,
            sessdata=sessdata, bili_jct=bili_jct, ac_time_value=ac_time_value,
            sessdata_expires_at=sessdata_expires_at, status="active",
        )
        account = self._accounts.get(uid)
        if account is None:
            account = CookieAccount(uid=uid)
            self._accounts[uid] = account
        account.nickname = nickname
        account.cookie_string = cookie_string
        account.sessdata = sessdata
        account.bili_jct = bili_jct
        account.ac_time_value = ac_time_value
        account.sessdata_expires_at = sessdata_expires_at
        account.status = "active"
        account.last_error = None
        account.updated_at = local_now()
        logger.info(f"Cookie池已写入账号 {uid}（{nickname}）")
        return account

    async def acquire_specific(self, uid: int) -> Optional[CookieAccount]:
        """
        取用指定账号（房间亲和绑定用），并更新最近使用时间

        Args:
            uid: B站用户UID

        Returns:
            Optional[CookieAccount]: 账号不存在或非active返回None
        """
        account = self._accounts.get(uid)
        if account is None or account.status != "active":
            return None
        account.last_used_at = local_now()
        await self._db.touch_cookie_account(account.uid)
        return account

    def warn_if_needed(self) -> Optional[str]:
        """
        检查可用账号是否触及预警阈值（跨越水位时只提醒一次）

        Returns:
            Optional[str]: 预警文本；无需提醒返回None
        """
        remaining = len(self.active_accounts())
        if remaining > self.warn_threshold:
            self._last_warned_level = -1  # 恢复水位，重置提醒
            return None
        # 水位未变化或回升时不重复提醒
        if remaining == self._last_warned_level:
            return None
        previous = self._last_warned_level
        self._last_warned_level = remaining
        if previous != -1 and remaining >= previous:
            return None

        if remaining == 0:
            return "账号池可用账号已耗尽，相关房间监控将暂停。请私聊发送 /登录 添加账号"
        return f"账号池可用账号仅剩 {remaining} 个，请及时私聊发送 /登录 补充"

    async def report_invalid(self, uid: int, reason: str = "") -> bool:
        """
        标记账号失效

        Args:
            uid: B站用户UID
            reason: 失效原因

        Returns:
            bool: 账号是否存在
        """
        account = self._accounts.get(uid)
        if account is None:
            return False

        account.status = "expired"
        account.last_error = reason
        ok = await self._db.set_cookie_account_status(uid, "expired", reason)
        logger.warning(f"Cookie池账号 {uid} 标记失效: {reason}")
        return ok

    def needs_refresh(self, account: CookieAccount) -> bool:
        """
        判断账号cookie是否临近过期需要刷新

        规则：SESSDATA剩余有效期不足REFRESH_AHEAD_DAYS天，
        或无过期时间信息且距上次更新超过REFRESH_INTERVAL_DAYS天。

        Args:
            account: 账号信息

        Returns:
            bool: 是否需要刷新
        """
        if account.status != "active":
            return False

        now = local_now()
        if account.sessdata_expires_at is not None:
            return account.sessdata_expires_at - now < timedelta(days=REFRESH_AHEAD_DAYS)

        if account.updated_at is not None:
            return now - account.updated_at > timedelta(days=REFRESH_INTERVAL_DAYS)
        return True

    def refresh_due_accounts(self) -> List[CookieAccount]:
        """全部临近过期需要刷新的active账号"""
        return [a for a in self.active_accounts() if self.needs_refresh(a)]

    def get_account(self, uid: int) -> Optional[CookieAccount]:
        """按uid查询账号"""
        return self._accounts.get(uid)

    def remove(self, uid: int) -> Optional[CookieAccount]:
        """
        从池中移除账号（内存；调用方需配合db_service.delete_cookie_account持久化）

        Args:
            uid: B站用户UID

        Returns:
            Optional[CookieAccount]: 被移除的账号，不存在返回None
        """
        return self._accounts.pop(uid, None)

    async def reactivate(self, uid: int, reason: str = "管理员手动恢复") -> Optional[CookieAccount]:
        """
        将expired账号重新标记为active（不验证cookie有效性，
        建议调用方先用nav验证）

        Args:
            uid: B站用户UID
            reason: 恢复原因

        Returns:
            Optional[CookieAccount]: 恢复的账号，不存在返回None
        """
        account = self._accounts.get(uid)
        if account is None:
            return None
        account.status = "active"
        account.last_error = None
        await self._db.set_cookie_account_status(uid, "active", None)
        return account
