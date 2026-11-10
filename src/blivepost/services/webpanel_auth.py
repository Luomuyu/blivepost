# -*- coding: utf-8 -*-
"""
Web状态面板鉴权与会话混入（AuthMixin）

会话管理（游客只读/用户可控/管理员可管）、登录防爆破、登录/登出动作
与登录页面。与WebPanel其余职责拆分组合使用（见webpanel.py）。

会话持久化（签名sid，无服务端状态）：sid格式为 ``payload.signature``，
payload为urlsafe-base64(JSON{u:用户名, a:管理员0/1, e:过期unix时间戳,
i:随机nonce})，signature=HMAC-SHA256(payload, 密钥)。会话不存内存/磁盘，
进程重启后登录态依然有效（与7天cookie承诺一致）。

密钥来源（derive_session_key）：配置了web_token时确定性派生
（sha256("sess:"+web_token)，重启不变）；未配置时进程启动生成随机密钥
（密钥不落盘——重启后旧签名失效、全员回到游客，是安全与便利的折衷；
如需重启保留登录态请配置web_token）。

代价与限制（无状态会话固有）：
- 单会话吊销为进程内存级（jti黑名单，v1.2.0起）：登出即吊销、防cookie
  残留重放；但进程重启后黑名单清空（被窃取的sid重启后恢复可用）；
- 全量登出手段：修改web_token（或未配置token时重启进程）会使全部旧会话
  验签失败→全员回到游客——顺带成为"紧急全量登出"的后门开关。
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Optional

from aiohttp import web

from .webpanel_common import _SESSION_COOKIE, _SESSION_TTL, logger, verify_password


if TYPE_CHECKING:
    from ..database.service import DatabaseService


def _b64url_encode(data: bytes) -> str:
    """urlsafe-base64编码（去填充，可作cookie/URL安全片段）"""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    """urlsafe-base64解码（容忍无填充输入）"""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def derive_session_key(web_token: str) -> bytes:
    """会话签名密钥：web_token非空时确定性派生（重启后旧会话仍有效）；
    未配置时进程启动随机生成（重启后旧签名全部失效）"""
    if web_token:
        return hashlib.sha256(f"sess:{web_token}".encode("utf-8")).digest()
    return secrets.token_bytes(32)


def _session_sign(body: str, key: bytes) -> str:
    """对payload主体做HMAC-SHA256签名（b64url无填充）"""
    return _b64url_encode(hmac.new(key, body.encode("ascii"), hashlib.sha256).digest())


def create_session_sid(user: str, is_admin: bool, exp: float, key: bytes) -> str:
    """构造签名会话sid：``payload.signature``（payload含用户名/管理员位/过期时间/nonce）"""
    payload = {"u": user, "a": 1 if is_admin else 0,
               "e": int(exp), "i": secrets.token_urlsafe(8)}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8"))
    return f"{body}.{_session_sign(body, key)}"


def verify_session_sid(sid: str, key: bytes) -> Optional[dict]:
    """验签+验期并解析会话sid；格式错/签名不符/字段非法/已过期返回None

    返回与旧内存会话同构的 {"user", "admin", "exp"}。
    """
    parts = sid.split(".")
    if len(parts) != 2:
        return None
    body, sig = parts
    try:
        if not secrets.compare_digest(sig, _session_sign(body, key)):
            return None
        payload = json.loads(_b64url_decode(body))
        if not (isinstance(payload, dict)
                and isinstance(payload.get("u"), str)
                and isinstance(payload.get("a"), int)
                and isinstance(payload.get("e"), (int, float))):
            return None
    except (ValueError, TypeError):
        return None
    if payload["e"] < datetime.now().timestamp():
        return None
    return {"user": payload["u"], "admin": bool(payload["a"]),
            "exp": payload["e"], "jti": payload.get("i", "")}


class AuthMixin:
    """Web面板鉴权与会话混入"""

    # 登录防爆破：窗口内失败达到上限后锁定该IP+用户名组合
    LOGIN_MAX_FAILURES = 5
    LOGIN_WINDOW_SECONDS = 300.0
    LOGIN_LOCK_SECONDS = 300.0
    # 失败计数字典容量上限（防随机用户名脚本无界增长内存）
    LOGIN_MAX_TRACKED = 1000
    # 吊销列表容量上限（jti → 过期monotonic；过期自动清理）
    REVOKED_MAX = 5000

    # ---- 以下实例属性由组合后的宿主WebPanel.__init__提供（仅类型标注） ----
    token: str
    admin_user: str
    admin_password: str
    db_service: "DatabaseService"
    _session_key: bytes
    _login_failures: dict

    if TYPE_CHECKING:
        # 渲染能力（PagesMixin提供）
        _render: Callable[..., Any]
        _html_response: Callable[..., Any]

    # ========== 会话与鉴权（游客只读/用户可控/管理员可管） ==========

    def _create_session(self, user: str, is_admin: bool) -> str:
        """新建会话（7天有效），返回签名会话ID（无状态持久化，见模块docstring）"""
        if not hasattr(self, "_revoked_jtis"):
            self._revoked_jtis: dict = {}
        exp = (datetime.now() + _SESSION_TTL).timestamp()
        return create_session_sid(user, is_admin, exp, self._session_key)

    def revoke_session(self, sid: str) -> bool:
        """吊销指定会话（加入jti黑名单，签名再有效也拒绝；进程生命周期内）"""
        if not hasattr(self, "_revoked_jtis"):
            self._revoked_jtis = {}
        sess = verify_session_sid(sid, self._session_key)
        if sess is None:
            return False
        jti = sess.get("jti", "")
        if not jti:
            return False
        self._prune_revoked()
        self._revoked_jtis[jti] = sess["exp"]  # 到会话自然过期时自动清理
        logger.info(f"已吊销会话（用户 {sess['user']}）")
        return True

    def _prune_revoked(self) -> None:
        """清理已自然过期的吊销条目"""
        import time
        if not hasattr(self, "_revoked_jtis"):
            return
        now = time.time()
        for k in [k for k, exp in self._revoked_jtis.items() if exp < now]:
            self._revoked_jtis.pop(k, None)
        # 超容量时截断最旧的
        while len(self._revoked_jtis) > self.REVOKED_MAX:
            self._revoked_jtis.pop(next(iter(self._revoked_jtis)))

    def _session_from_sid(self, sid: str) -> Optional[dict]:
        """验签解析会话sid（含吊销检查；None=无效/伪造/过期/已吊销）"""
        sess = verify_session_sid(sid, self._session_key)
        if sess is None:
            return None
        jti = sess.get("jti", "")
        if jti and hasattr(self, "_revoked_jtis") and jti in self._revoked_jtis:
            return None  # 已吊销
        return sess

    def _session(self, request) -> Optional[dict]:
        """当前会话（None=游客）：cookie携签名sid，验签+验期+吊销检查"""
        sid = request.cookies.get(_SESSION_COOKIE, "")
        return self._session_from_sid(sid)

    def _current_user(self, request) -> Optional[str]:
        sess = self._session(request)
        return sess["user"] if sess else None

    def _is_admin(self, request) -> bool:
        sess = self._session(request)
        return bool(sess and sess["admin"])

    def _web_token_ok(self, request) -> bool:
        """管理员快捷令牌校验（未配置token时不可用；常量时间比较防时序侧信道）"""
        if not self.token:
            return False
        provided = (request.query.get("token")
                    or request.headers.get("X-Web-Token")
                    or "")
        # 比较bytes：str入参含非ASCII时compare_digest会抛TypeError
        return secrets.compare_digest(
            provided.encode("utf-8"), self.token.encode("utf-8"))

    def _set_session_cookie(self, resp: web.Response, sid: str) -> None:
        resp.set_cookie(_SESSION_COOKIE, sid, max_age=int(_SESSION_TTL.total_seconds()),
                        httponly=True, samesite="Lax", path="/")

    async def _auth_token_to_admin(self, request):
        """?token=管理员快捷令牌→创建管理员会话并重定向到干净URL（无令牌返回None）"""
        provided = request.query.get("token") or ""
        if not (self.token and provided and
                secrets.compare_digest(provided.encode("utf-8"),
                                        self.token.encode("utf-8"))):
            return None
        sid = self._create_session(self.admin_user, is_admin=True)
        target = request.path or "/"
        resp = web.HTTPFound(target)
        self._set_session_cookie(resp, sid)
        logger.info(f"Web面板令牌登录（管理员）：{request.remote}")
        return resp

    def _requester(self, request) -> tuple:
        """请求者身份：(用户名, 是否管理员)；游客返回("", False)"""
        sess = self._session(request)
        if sess:
            return sess["user"], bool(sess["admin"])
        if self._web_token_ok(request):
            return self.admin_user, True
        return "", False

    # ---- 登录防爆破（IP+用户名双维度失败锁定） ----

    def _login_locked(self, key: str) -> bool:
        """key（IP或用户名）是否处于登录锁定状态"""
        rec = self._login_failures.get(key)
        if rec is None:
            return False
        count, window_start, lock_until = rec
        now = time.monotonic()
        if lock_until and now < lock_until:
            return True
        if now - window_start > self.LOGIN_WINDOW_SECONDS:
            self._login_failures.pop(key, None)
        return False

    def _record_login_failure(self, key: str) -> None:
        """记录一次登录失败；窗口内达到上限则锁定。

        计数字典有上限（防脚本化随机用户名无界增长）：超限时先清过期项，
        仍超限则按插入序淘汰最旧条目（等效放宽其限速，不放大风险面）。
        """
        now = time.monotonic()
        rec = self._login_failures.get(key)
        if rec is None or now - rec[1] > self.LOGIN_WINDOW_SECONDS:
            rec = (0, now, 0.0)
        count = rec[0] + 1
        lock_until = (now + self.LOGIN_LOCK_SECONDS
                      if count >= self.LOGIN_MAX_FAILURES else rec[2])
        self._login_failures[key] = (count, rec[1], lock_until)
        if len(self._login_failures) > self.LOGIN_MAX_TRACKED:
            expired = now - self.LOGIN_WINDOW_SECONDS
            for k in [k for k, v in self._login_failures.items()
                      if v[1] < expired and not (v[2] and v[2] > now)]:
                self._login_failures.pop(k, None)
            while len(self._login_failures) > self.LOGIN_MAX_TRACKED:
                self._login_failures.pop(next(iter(self._login_failures)))

    def _clear_login_failures(self, *keys: str) -> None:
        for key in keys:
            self._login_failures.pop(key, None)

    async def _require_login(self, request) -> Optional[web.Response]:
        """动作接口鉴权：游客拒绝（登录用户与管理员放行）"""
        if self._session(request) is None and not self._web_token_ok(request):
            return web.Response(status=401, text="unauthorized: 请先登录（游客只读）")
        return None

    async def _require_admin(self, request) -> Optional[web.Response]:
        """后台接口鉴权：仅管理员会话（web_token快捷令牌视同管理员）"""
        if not self._is_admin(request) and not self._web_token_ok(request):
            return web.Response(status=403, text="forbidden: 需要管理员权限")
        return None

    # ========== 登录/登出页面 ==========

    async def _handle_login_page(self, request):
        """登录页（游客可访问；已登录重定向首页）"""
        token_resp = await self._auth_token_to_admin(request)
        if token_resp is not None:
            return token_resp
        if self._session(request) is not None:
            raise web.HTTPFound("/")
        ctx = {
            "page": "login", "nav": [], "current_user": None, "is_admin": False,
            "error": request.query.get("err", ""),
            "admin_enabled": bool(self.admin_password),
        }
        html = self._render("base.html", page_template="pages/login.html", **ctx)
        return self._html_response(request, html)

    async def _action_web_login(self, request):
        """登录：先匹配config管理员，再匹配web_user表；成功建会话重定向首页。

        带防爆破：IP与用户名双维度失败计数，窗口内达到上限锁定。
        """
        form = await request.post()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        ip_key = f"ip:{request.remote or 'unknown'}"
        user_key = f"user:{username}"
        if self._login_locked(ip_key) or self._login_locked(user_key):
            logger.warning(f"Web面板登录被拒绝（锁定中）：{username}（{request.remote}）")
            raise web.HTTPFound("/login?err=3")
        user, is_admin = None, False
        if self.admin_password and username == self.admin_user \
                and secrets.compare_digest(password.encode("utf-8"),
                                           self.admin_password.encode("utf-8")):
            user, is_admin = self.admin_user, True
        else:
            record = await self.db_service.get_web_user(username) \
                if self.db_service is not None else None
            if record is not None and verify_password(password, record["password_hash"]):
                user = username
        if user is None:
            self._record_login_failure(ip_key)
            self._record_login_failure(user_key)
            logger.warning(f"Web面板登录失败：{username}（{request.remote}）")
            raise web.HTTPFound("/login?err=1")
        self._clear_login_failures(ip_key, user_key)
        sid = self._create_session(user, is_admin)
        resp = web.HTTPFound("/admin" if is_admin else "/")
        self._set_session_cookie(resp, sid)
        logger.info(f"Web面板登录：{user}（{'管理员' if is_admin else '用户'}，{request.remote}）")
        return resp

    async def _action_web_logout(self, request):
        """登出：吊销当前会话jti + 清除cookie回到游客

        v1.2.0起支持单会话吊销（jti黑名单，进程生命周期内有效）；
        全量登出仍可通过改web_token实现。注：吊销列表在内存，
        进程重启后清空（被窃取的sid重启后恢复可用——需改token根治）。
        """
        sid = request.cookies.get(_SESSION_COOKIE, "")
        if sid:
            self.revoke_session(sid)  # 吊销jti，防cookie残留重放
        resp = web.HTTPFound("/")
        resp.del_cookie(_SESSION_COOKIE, path="/")
        raise resp
