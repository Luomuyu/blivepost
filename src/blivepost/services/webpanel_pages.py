# -*- coding: utf-8 -*-
"""
Web状态面板页面与上下文混入（PagesMixin）

HTML渲染辅助、面板数据收集、页面处理器生成、各页面上下文与局部/
JSON接口（趋势/词云/历史/状态）。与WebPanel其余职责拆分组合使用
（见webpanel.py）。
"""

import asyncio
import json
import time

from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Tuple

from aiohttp import web

from .webpanel_common import (
    SWITCH_DEFS,
    _add_days,
    _aggregate_monthly,
    _env,
    _parse_day,
    _parse_month,
    logger,
)
from ..utils.timeutil import ms_to_str, now_local, now_ms


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .cookie_pool import CookiePool
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


class PagesMixin:
    """Web面板页面与上下文混入"""

    # ---- 以下实例属性由组合后的宿主WebPanel.__init__提供（仅类型标注） ----
    cookie_pool: "CookiePool"
    monitor_manager: "MonitorManager"
    dynamic_manager: Any
    binding_store: "BindingStore"
    db_service: "DatabaseService"
    bot_client: Any
    message_settings: Any
    # 页面注册表：key -> (路径, 标题)（由宿主WebPanel定义）
    PAGES: dict

    # 主播昵称进程内TTL缓存：room_id -> (monotonic到期秒, 昵称)。
    # 昵称读库但变更频率极低（绑定时写入+每日0点批量刷新），10分钟TTL
    # 足够新鲜；混入无__init__，故为类级共享（进程内单面板实例，等同进程缓存）。
    _STREAMER_NAME_TTL = 600.0
    _streamer_names: Dict[int, Tuple[float, str]] = {}

    if TYPE_CHECKING:
        # 鉴权能力（AuthMixin提供）
        _auth_token_to_admin: Callable[..., Any]
        _current_user: Callable[..., Any]
        _is_admin: Callable[..., Any]
        _require_login: Callable[..., Any]
        _session: Callable[..., Any]
        _web_token_ok: Callable[..., Any]

    # ========== HTML渲染辅助 ==========

    def _html_response(self, request, html: str) -> web.Response:
        """HTML响应"""
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    def _render(self, template: str, **ctx) -> str:
        return _env.get_template(template).render(**ctx)

    # ========== 数据收集 ==========

    async def _collect(self) -> dict:
        """收集面板数据"""
        now = now_ms()

        # 机器人凭证状态（access_token，botpy请求级自动刷新）
        token_state = self.bot_client.token_state() if self.bot_client else {
            "client_started": False, "token_ready": False, "token_expires_in": 0}
        # 机器人详情（username/avatar/share_url，获取失败缓存为{}下轮重试）
        me = await self.bot_client.get_me() if self.bot_client else None
        me = me or {}
        app_id = getattr(self.bot_client, "app_id", "") if self.bot_client else ""

        # 数据库体积（主库+全部房间子库+动态子库；永久保留策略下的增长监控）
        db_size_mb = 0.0
        try:
            if self.db_service is not None:
                db_size_mb = await self.db_service.get_storage_size_mb()
        except Exception as e:
            logger.warning(f"统计存储体积失败: {e}")

        accounts = []
        for a in (self.cookie_pool.accounts if self.cookie_pool else []):
            rooms = self.monitor_manager.get_account_rooms(a.uid) if self.monitor_manager else []
            days_left = None
            if a.sessdata_expires_at is not None:
                days_left = max((a.sessdata_expires_at - now) // 86_400_000, 0)
            accounts.append({
                "uid": a.uid, "nickname": a.nickname, "status": a.status,
                "days_left": days_left,
                "use_count": getattr(a, "use_count", 0),
                "expires": ms_to_str(a.sessdata_expires_at, "%m-%d") if a.sessdata_expires_at else None,
                "last_used": ms_to_str(a.last_used_at, "%m-%d %H:%M") if a.last_used_at else None,
                "rooms": rooms,
            })

        # 房间行并行收集：今日汇总一次批量扇出（get_daily_summaries内部
        # 并行查各房间子库），主播名按房并行查（带TTL缓存），不再逐房串行2N次
        room_ids = list(self.monitor_manager.room_ids()) \
            if self.monitor_manager else []
        summaries: Dict[int, dict] = {}
        if room_ids and self.db_service is not None:
            try:
                summaries = await self.db_service.get_daily_summaries(
                    room_ids)
            except Exception as e:
                logger.warning(f"批量查询每日汇总失败: {e}")

        async def _room_row(rid: int) -> dict:
            # 单房间行：主播名查询失败仅降级为空名，不拖垮整页其余房间行
            zero = {"danmaku_count": 0, "gift_gold": 0,
                    "ship_total": 0, "sc_total": 0}
            try:
                streamer = await self._streamer_name(rid)
            except Exception as e:
                logger.warning(f"查询主播昵称失败(房间{rid}): {e}")
                streamer = ""
            s = summaries.get(rid) or zero
            return {
                "room_id": rid,
                "alive": self.monitor_manager.has(rid) if self.monitor_manager else False,
                "uid": self.monitor_manager.get_cookie_uid(rid) if self.monitor_manager else None,
                "streamer": streamer,
                "danmaku": s["danmaku_count"],
                "gold": s["gift_gold"], "ship": s["ship_total"], "sc": s["sc_total"],
            }

        rooms = list(await asyncio.gather(*(_room_row(rid) for rid in room_ids)))

        dynamics = []
        for uid in self.binding_store.get_dynamic_up_uids() \
                if self.binding_store else []:
            dynamics.append({
                "up_uid": uid,
                "name": self.binding_store.get_dynamic_up_name(uid),
                "alive": self.dynamic_manager.has(uid) if self.dynamic_manager else False,
                "groups": len(self.binding_store.get_dynamic_groups(uid)),
            })

        recent = await self.db_service.get_recent_live_status(limit=10) \
            if self.db_service else []
        return {
            "now": ms_to_str(now, "%Y-%m-%d %H:%M:%S"),
            "bot": token_state,
            "me": me,
            "app_id": app_id,
            "db_size_mb": db_size_mb,
            "accounts": accounts,
            "rooms": rooms,
            "dynamics": dynamics,
            "recent": recent,
        }

    async def _streamer_name(self, room_id: int) -> str:
        """直播间对应主播昵称（读库：绑定时写入、每日0点批量刷新；10分钟TTL缓存）"""
        cached = self._streamer_names.get(room_id)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
        uid = 0
        if self.binding_store is not None:
            for group in self.binding_store.get_monitored_groups(room_id):
                b = self.binding_store.get_binding(group, room_id)
                if b is not None and b.uid:
                    uid = b.uid
                    break
        if not uid or self.db_service is None:
            return ""
        try:
            name = await self.db_service.get_streamer_name(uid)
        except Exception as e:
            logger.warning(f"查询主播昵称失败(房间{room_id}): {e}")
            return ""  # 查询失败不缓存，下轮重试
        self._streamer_names[room_id] = (
            time.monotonic() + self._STREAMER_NAME_TTL, name)
        return name

    # ========== 页面与局部渲染 ==========

    def _make_page_handler(self, key: str):
        """生成页面GET处理器（HTMX请求返回内容片段，普通请求返回完整布局；游客可浏览）"""
        nav = [(path, key_, title) for key_, (path, title) in self.PAGES.items()]

        async def _handler(request):
            # ?token=令牌登录已收窄到/login页（防token经URL在全站传播，
            # 见webpanel_auth._web_token_ok）；此处仅做会话/令牌头判定
            is_guest = (self._session(request) is None
                        and not self._web_token_ok(request))
            if is_guest and key == "accounts":
                # 账号池页=运营情报（UID/昵称/到期/扫码入池），整页要求登录
                raise web.HTTPFound("/login")
            ctx = await self._page_context(key)
            if is_guest:
                # 游客可见聚合数据，身份标识隐藏（与/api/status脱敏口径一致）
                ctx["app_id"] = ""
            ctx.update(page=key, nav=nav, current_user=self._current_user(request),
                       is_admin=self._is_admin(request))
            if request.headers.get("HX-Request"):
                html = self._render(f"pages/{key}.html", **ctx)
            else:
                html = self._render("base.html", page_template=f"pages/{key}.html", **ctx)
            return self._html_response(request, html)
        _handler.__name__ = f"page_{key}"
        return _handler

    async def _page_context(self, key: str) -> dict:
        """各页面公共上下文（概览=全量；子页面按需子集）"""
        data = await self._collect()
        ctx = {
            "bot": data["bot"], "me": data["me"], "app_id": data["app_id"],
            "db_size_mb": data["db_size_mb"], "accounts": data["accounts"],
            "rooms": data["rooms"], "dynamics": data["dynamics"],
            "recent": data["recent"],
            "pool_active": sum(1 for a in data["accounts"] if a["status"] == "active"),
            "pool_total": len(data["accounts"]),
        }
        if key in ("live", "dynamics", "groups"):
            # active_only：已移出的群不再出现在选项与列表（重新入群自动恢复）
            ctx["group_options"] = [(g.group_openid, g.group_name)
                                    for g in await self.db_service.load_groups(active_only=True)]
        if key == "groups":
            ctx["groups"] = await self.db_service.load_groups(active_only=True)
        if key == "live":
            # 上半段：直播间注册表（推送群/监控状态），下半段：群绑定关系（含群详情）
            groups_by_openid = {}
            for g in await self.db_service.load_groups(active_only=True):
                groups_by_openid[g.group_openid] = g
            live_rooms = []
            for rec in await self.db_service.list_rooms():
                push_groups = self.binding_store.get_monitored_groups(rec.room_id) \
                    if self.binding_store else []
                push_names = [groups_by_openid[g].group_name if g in groups_by_openid else g
                              for g in push_groups]
                live_rooms.append({
                    "room_id": rec.room_id,
                    "streamer": await self.db_service.get_streamer_name(rec.uid)
                                if rec.uid else "",
                    "push_groups": push_names,
                    # openid列表：行内群编辑器复选框回显勾选用（push_groups是名称仅展示）
                    "push_group_openids": list(push_groups),
                    "monitored": bool(push_groups),
                    "alive": bool(push_groups) and self.monitor_manager is not None
                             and self.monitor_manager.has(rec.room_id),
                })
            ctx["rooms"] = live_rooms

            global_sw = {ps["name"]: ps for ps in await self._push_settings()}
            bindings = []
            for rec in await self.db_service.list_bindings():
                if not rec.monitored:
                    continue  # 已停止的推送关系不入库也不展示
                own = await self.db_service.get_group_push_setting(
                    rec.room_id, rec.group_openid)
                push_items = []
                custom_any = False
                for name, label in SWITCH_DEFS:
                    if name == "dynamic_monitor":
                        continue  # 动态监控为全局项，不随房间
                    g = global_sw.get(name, {})
                    glob_on = g.get("on", False)
                    # custom=本群值偏离全局（行随绑定自动播种，偏离才视为独立设置）
                    custom = own is not None and own[name] != glob_on
                    custom_any = custom_any or custom
                    push_items.append({
                        "name": name, "label": label,
                        "on": own[name] if own else glob_on,
                        "custom": custom,
                        "allow_modify": g.get("allow_modify", False),
                    })
                bindings.append({
                    "group_openid": rec.group_openid,
                    **self._group_info(groups_by_openid.get(rec.group_openid)),
                    "room_id": rec.room_id,
                    "push_settings": push_items,
                    "has_custom": custom_any,
                })
            ctx["bindings"] = bindings
        if key == "dynamics":
            # 下半段：动态订阅的群绑定关系（群×UP，含群详情）
            groups_by_openid = {}
            for g in await self.db_service.load_groups(active_only=True):
                groups_by_openid[g.group_openid] = g
            dyn_bindings = []
            if self.binding_store is not None:
                for uid in self.binding_store.get_dynamic_up_uids():
                    up_name = self.binding_store.get_dynamic_up_name(uid)
                    for openid in self.binding_store.get_dynamic_groups(uid):
                        dyn_bindings.append({
                            "group_openid": openid,
                            **self._group_info(groups_by_openid.get(openid)),
                            "up_uid": uid,
                            "up_name": up_name,
                        })
            ctx["dyn_bindings"] = dyn_bindings
            ctx["push_settings"] = await self._push_settings()
        if key == "trend":
            ctx.update(await self._trend_context("d30", ""))
        if key == "wordcloud":
            ctx.update(await self._wordcloud_context("today"))
        return ctx

    async def _push_settings(self) -> list:
        """全局推送开关徽标（直播/动态两页的群详情共用；DB运行时值优先）"""
        ms = self.message_settings
        def _default(attr):
            return bool(getattr(ms, attr, False)) if ms is not None else False
        switches = {}
        if self.db_service is not None:
            try:
                switches = await self.db_service.get_push_switches()
            except Exception:
                switches = {}
        return [
            {"name": name, "label": label,
             "on": switches.get(name, {}).get("enabled", _default(name)),
             "allow_modify": switches.get(name, {}).get("allow_modify", False)}
            for name, label in SWITCH_DEFS
        ]

    @staticmethod
    def _group_info(g) -> dict:
        """bot_group行→群详情字段（tags JSON文本转顿号分隔；缺组/空值保持None）"""
        tags = None
        if g is not None and g.tags:
            try:
                tag_list = json.loads(g.tags)
                tags = "、".join(str(t) for t in tag_list) or None
            except ValueError:
                tags = g.tags
        return {
            "group_name": g.group_name if g else None,
            "group_desc": g.group_desc if g else None,
            "category": g.category if g else None,
            "tags": tags,
            "member_count": g.member_count if g else None,
        }

    async def _handle_trend(self, request):
        room_raw = (request.query.get("room") or "").strip()
        room_id = int(room_raw) if room_raw.isdigit() else None
        ctx = await self._trend_context(
            request.query.get("range", "d30"), request.query.get("month", ""),
            room_id=room_id)
        html = self._render("partials/trend.html", **ctx)
        return self._html_response(request, html)

    async def _handle_wordcloud(self, request):
        """词云局部（HTMX交换；游客只读可看）"""
        room_raw = (request.query.get("room") or "").strip()
        room_id = int(room_raw) if room_raw.isdigit() else None
        ctx = await self._wordcloud_context(
            request.query.get("range", "today"),
            day_str=request.query.get("day", ""),
            month_str=request.query.get("month", ""),
            year_str=request.query.get("year", ""),
            room_id=room_id)
        html = self._render("partials/wordcloud.html", **ctx)
        return self._html_response(request, html)

    async def _wordcloud_context(self, range_key: str, day_str: str = "",
                                 month_str: str = "", year_str: str = "",
                                 room_id: Optional[int] = None) -> dict:
        """
        词云上下文：按天（今天/昨天/指定天）、近3/7/14天、按月（本月/上月/指定月）、
        按年（今年/去年/指定年），可切全部/单独直播间

        Returns:
            dict: {label, words, total, room_id, active, day/month/year值, room_options}
        """
        today = now_local().date()
        start = end = today
        active = range_key or "today"
        label = "今天"
        day_value = today.strftime("%Y-%m-%d")
        month_value = today.strftime("%Y-%m")
        year_value = str(today.year)

        def _month_bounds(y: int, m: int):
            start_m = date(y, m, 1)
            next_m = date(y + (m == 12), (m % 12) + 1, 1)
            return start_m, _add_days(next_m, -1)

        if range_key == "today":
            label = "今天"
        elif range_key == "yesterday":
            start = end = _add_days(today, -1)
            label = "昨天"
        elif range_key == "day" and _parse_day(day_str):
            start = end = _parse_day(day_str)
            day_value = start.strftime("%Y-%m-%d")
            active = "day"
            label = f"{start.year}年{start.month}月{start.day}日"
        elif range_key in ("d3", "d7", "d14"):
            n = int(range_key[1:])
            start = _add_days(today, -(n - 1))
            label = f"近{n}天"
        elif range_key == "month":
            start, end = _month_bounds(today.year, today.month)
            end = min(end, today)
            label = f"{today.year}年{today.month}月"
        elif range_key == "lastmonth":
            first_this = today.replace(day=1)
            last_prev = _add_days(first_this, -1)
            start = last_prev.replace(day=1)
            end = last_prev
            label = f"{last_prev.year}年{last_prev.month}月"
        elif range_key.startswith("month:") and _parse_month(month_str):
            y, m = _parse_month(month_str)
            start, end = _month_bounds(y, m)
            end = min(end, today)
            if start > today:
                start = end = today
            active = f"month:{month_str}"
            month_value = month_str
            label = f"{y}年{m}月"
        elif range_key == "year":
            start = date(today.year, 1, 1)
            label = f"{today.year}年"
        elif range_key == "lastyear":
            start = date(today.year - 1, 1, 1)
            end = date(today.year - 1, 12, 31)
            label = f"{today.year - 1}年"
        elif range_key.startswith("year:") and year_str.isdigit() \
                and 2000 <= int(year_str) <= today.year:
            y = int(year_str)
            start = date(y, 1, 1)
            end = min(date(y, 12, 31), today)
            active = f"year:{year_str}"
            year_value = year_str
            label = f"{y}年"
        else:
            active = "today"

        room_ids = [room_id] if room_id else None
        texts = await self.db_service.get_danmaku_texts(
            room_ids=room_ids, start_day=start, end_day=end)
        from .wordcloud import build_wordcloud
        words = await asyncio.get_running_loop().run_in_executor(
            None, build_wordcloud, texts)
        label += f" · 房间{room_id}" if room_id else ""

        room_options = set()
        if self.binding_store is not None:
            try:
                room_options.update(self.binding_store.get_monitored_room_ids())
            except Exception:
                pass
        try:
            room_options.update(await self.db_service.list_room_ids())
        except Exception:
            pass
        return {
            "label": label, "words": words, "total": len(texts),
            "danmaku_count": len(texts),
            "room_id": room_id, "active": active,
            "day_value": day_value, "month_value": month_value,
            "year_value": year_value,
            "this_month_key": month_value, "this_year": str(today.year),
            "room_options": sorted(room_options),
        }

    async def _trend_context(self, range_key: str, month_str: str,
                             room_id: Optional[int] = None) -> dict:
        """
        趋势数据上下文：近N天 / 本月 / 上月 / 指定自然月 / 近12个月（按月汇总）

        Args:
            room_id: 指定直播间（None=全部直播间总和）
        """
        today = now_local().date()
        rows: list = []
        mode = "daily"
        active = "d30"
        label = "近30天"
        month_value = today.strftime("%Y-%m")
        room_ids = [room_id] if room_id else None

        if range_key in ("d7", "d14", "d30"):
            n = int(range_key[1:])
            active = range_key
            label = f"近{n}天"
            rows = await self.db_service.get_daily_history_range(
                _add_days(today, -(n - 1)), today, room_ids=room_ids)
        elif range_key == "this_month":
            active = range_key
            label = f"{today.year}年{today.month}月"
            rows = await self.db_service.get_daily_history_range(
                today.replace(day=1), today, room_ids=room_ids)
        elif range_key == "last_month":
            first_this = today.replace(day=1)
            last_prev = _add_days(first_this, -1)
            start = last_prev.replace(day=1)
            active = range_key
            label = f"{last_prev.year}年{last_prev.month}月"
            rows = await self.db_service.get_daily_history_range(
                start, last_prev, room_ids=room_ids)
        elif range_key == "12m":
            active = range_key
            mode = "monthly"
            label = "近12个月（按月汇总）"
            first_this = today.replace(day=1)
            start = _add_days(first_this, -1).replace(day=1)
            for _ in range(10):
                start = _add_days(start, -1).replace(day=1)
            daily = await self.db_service.get_daily_history_range(
                start, today, room_ids=room_ids)
            rows = _aggregate_monthly(daily)
        elif (range_key == "custom_month" or range_key.startswith("month:")) \
                and _parse_month(month_str):
            y, m = _parse_month(month_str)
            start = date(y, m, 1)
            if (y, m) == (today.year, today.month):
                end = today
            elif start > today:
                return await self._trend_context("d30", "")  # 未来月份回落近30天
            else:
                next_month = date(y + (m == 12), (m % 12) + 1, 1)
                end = _add_days(next_month, -1)
            active = f"month:{month_str}"
            month_value = month_str
            label = f"{y}年{m}月"
            rows = await self.db_service.get_daily_history_range(
                start, end, room_ids=room_ids)
        else:
            rows = await self.db_service.get_daily_history_range(
                _add_days(today, -29), today, room_ids=room_ids)

        if room_id:
            label += f" · 房间{room_id}"

        room_options = set()
        if self.binding_store is not None:
            try:
                room_options.update(self.binding_store.get_monitored_room_ids())
            except Exception:
                pass
        try:
            room_options.update(await self.db_service.list_room_ids())
        except Exception:
            pass

        return {
            "data": rows, "mode": mode, "active": active, "label": label,
            "month_value": month_value,
            "this_month_key": today.strftime("%Y-%m"),
            "room_id": room_id or "",
            "room_options": sorted(room_options),
        }

    # ========== JSON接口 ==========

    async def _handle_history(self, request):
        try:
            days = max(min(int(request.query.get("days", 30)), 365), 1)
        except ValueError:
            days = 30
        history = await self.db_service.get_daily_history(days=days) \
            if self.db_service else []
        return web.json_response({"days": days, "history": history})

    async def _handle_api(self, request):
        # /api/status含app_id/账号UID与到期/群规模等运营情报，不再对游客开放
        denied = await self._require_login(request)
        if denied is not None:
            return denied
        data = await self._collect()
        return web.json_response(data)
