# -*- coding: utf-8 -*-
"""
Web状态面板（只读，aiohttp实现）

展示账号池状态、监控房间与进程状态、各房间当日汇总、最近下播记录。
默认监听127.0.0.1（仅本机访问），无鉴权——请勿直接暴露到公网。
通过Setting.web_panel开关，web_host/web_port配置监听。
"""

import json
import os
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from aiohttp import web

from ..utils.logger import get_logger


if TYPE_CHECKING:
    from .bindings import BindingStore
    from .cookie_pool import CookiePool
    from ..core.manager import MonitorManager
    from ..database.service import DatabaseService


logger = get_logger("webpanel")


# 收入趋势图（原生JS渲染，无外部依赖）：
# 按容器实际宽度绘制SVG（窗口resize重绘，移动端自适应）；
# 鼠标悬停/触摸显示该日四项数据浮层；数据由页面内嵌JSON提供
_TREND_JS = """
(function(){
  var el=document.getElementById('trend'), tip=document.getElementById('trend-tip');
  var data=[];
  try{ data=JSON.parse(document.getElementById('trend-data').textContent)||[]; }catch(e){}
  var S=[["danmaku","#4a90d9","弹幕"],["gift_gold","#e67e22","礼物(元)"],
         ["ship_gold","#2e8b57","上舰(元)"],["sc_total","#c0392b","SC(元)"]];
  if(!data.length){el.innerHTML='<p style="margin:12px;color:#888">暂无趋势数据</p>';return;}
  function fmt(v){v=+v||0;return Math.abs(v)>=100?String(Math.round(v)):(Math.round(v*10)/10).toString();}
  var PL=42, PR=10, step=0, n=0;
  function draw(){
    var W=el.clientWidth||600; if(W<260)W=260;
    var H=240, iw=W-PL-PR, ih=H-36; n=data.length;
    var peak=1;
    for(var s=0;s<S.length;s++){
      var arr=data.map(function(r){return +r[S[s][0]]||0;});
      S[s][3]=arr;
      for(var i=0;i<arr.length;i++) if(arr[i]>peak)peak=arr[i];
    }
    function X(i){return PL+(n>1?i*iw/(n-1):iw/2);}
    function Y(v){return 12+ih-(v/peak)*ih;}
    step=n>1?iw/(n-1):0;
    var g='';
    for(var f=0;f<=4;f++){
      var y=12+ih-ih*f/4;
      g+='<line x1="'+PL+'" y1="'+y+'" x2="'+(W-PR)+'" y2="'+y+'" stroke="#e8e8e8"/>'+
         '<text x="'+(PL-5)+'" y="'+(y+3)+'" font-size="10" fill="#999" text-anchor="end">'+fmt(peak*f/4)+'</text>';
    }
    var labN=Math.min(n,Math.max(2,Math.floor(W/95)));
    for(var j=0;j<labN;j++){
      var i2=labN>1?Math.round(j*(n-1)/(labN-1)):0;
      g+='<text x="'+X(i2)+'" y="'+(H-6)+'" font-size="10" fill="#999" text-anchor="middle">'+data[i2].day.slice(5)+'</text>';
    }
    var sv='<svg width="'+W+'" height="'+H+'" xmlns="http://www.w3.org/2000/svg">'+g;
    for(var s2=0;s2<S.length;s2++){
      var pts=S[s2][3].map(function(v,i3){return X(i3).toFixed(1)+','+Y(v).toFixed(1);}).join(' ');
      sv+='<polyline fill="none" stroke="'+S[s2][1]+'" stroke-width="1.8" points="'+pts+'"/>';
    }
    sv+='</svg>';
    el.innerHTML=sv;
    var svg=el.querySelector('svg');
    function pos(e){
      var cx=(e.touches?e.touches[0].clientX:e.clientX);
      var r=svg.getBoundingClientRect();
      var i=step?Math.round((cx-r.left-PL)/step):0;
      i=Math.max(0,Math.min(n-1,i));
      var rows='<b>'+data[i].day+'</b>';
      for(var s3=0;s3<S.length;s3++)
        rows+='<br><span style="color:'+S[s3][1]+'">●</span> '+S[s3][2]+': '+fmt(S[s3][3][i]);
      tip.innerHTML=rows; tip.style.display='block';
      var box=el.parentElement.getBoundingClientRect();
      var lx=cx-box.left+14;
      if(lx+150>box.width) lx=cx-box.left-164;
      if(lx<0) lx=0;
      tip.style.left=lx+'px'; tip.style.top='10px';
    }
    function hide(){tip.style.display='none';}
    svg.addEventListener('mousemove',pos);
    svg.addEventListener('mouseleave',hide);
    svg.addEventListener('touchstart',pos,{passive:true});
    svg.addEventListener('touchmove',pos,{passive:true});
    svg.addEventListener('touchend',hide);
  }
  var rt=null;
  window.addEventListener('resize',function(){clearTimeout(rt);rt=setTimeout(draw,200);});
  draw();
})();
"""

_PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>BLivePost 状态面板</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:24px;background:#f7f8fa;color:#333}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:24px;border-left:4px solid #4a90d9;padding-left:8px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}
th,td{{border:1px solid #e0e0e0;padding:6px 10px;text-align:left}}
th{{background:#f0f4f8}}
.ok{{color:#2e8b57}} .bad{{color:#c0392b}} small{{color:#888}}
.tbl{{overflow-x:auto;-webkit-overflow-scrolling:touch}}
.trend-box{{position:relative}}
#trend{{background:#fff;border:1px solid #e0e0e0}}
#trend-tip{{position:absolute;pointer-events:none;display:none;background:rgba(40,40,40,.92);
color:#fff;font-size:12px;line-height:1.6;padding:6px 10px;border-radius:4px;white-space:nowrap;z-index:5}}
@media (max-width:640px){{
body{{margin:10px}}
h1{{font-size:17px}} h2{{font-size:15px;margin-top:18px}}
table{{font-size:12px}} th,td{{padding:4px 6px}}
}}
</style></head><body>
<h1>BLivePost 状态面板 <small>刷新时间 {now} · 每30秒自动刷新</small></h1>
<p>机器人凭证：<b>{bot_state}</b>（token剩余 {token_expires_in} 秒）
　数据库体积：<b>{db_size_mb} MB</b> <small>（永久保留策略，数据不自动删除）</small></p>
<h2>机器人详情</h2>
<div class="tbl"><table><tr><th>头像</th><th>用户名</th><th>分享</th></tr>
{me_row}</table></div>
<h2>账号池（可用 {active}/{total}）</h2>
<div class="tbl"><table><tr><th>UID</th><th>昵称</th><th>状态</th><th>剩余有效期</th><th>使用次数</th><th>最近使用</th><th>关联房间</th></tr>
{accounts}</table></div>
<h2>收入趋势（近{hist_days}天） <small><a href="/api/history?days={hist_days}">JSON</a> · 悬停/触摸查看各日明细</small></h2>
<div class="trend-box"><div id="trend"></div><div id="trend-tip"></div></div>
<p><small>图例：蓝=弹幕 橙=礼物(元) 绿=上舰(元) 红=SC(元)
JSON: <a href="/api/history?days={hist_days}">/api/history</a></small></p>
<h2>监控房间（{room_count}）</h2>
<div class="tbl"><table><tr><th>房间</th><th>进程</th><th>账号</th><th>今日弹幕</th><th>礼物(元)</th><th>上舰(元)</th><th>SC(元)</th></tr>
{rooms}</table></div>
<h2>动态订阅（{dyn_count}）</h2>
<div class="tbl"><table><tr><th>UP主UID</th><th>昵称</th><th>轮询进程</th><th>订阅群数</th></tr>
{dynamics}</table></div>
<h2>最近下播记录</h2>
<div class="tbl"><table><tr><th>时间</th><th>房间</th><th>弹幕</th><th>礼物(元)</th><th>上舰(元)</th><th>SC(元)</th><th>时长</th></tr>
{live_records}</table></div>
<script id="trend-data" type="application/json">__TREND_JSON__</script>
<script>__TREND_JS__</script>
</body></html>"""


class WebPanel:
    """Web状态面板服务"""

    def __init__(
        self,
        cookie_pool: "CookiePool",
        monitor_manager: "MonitorManager",
        dynamic_manager,
        binding_store: "BindingStore",
        db_service: "DatabaseService",
        host: str = "127.0.0.1",
        port: int = 8080,
        token: str = "",
        bot_client=None,
    ):
        self.cookie_pool = cookie_pool
        self.monitor_manager = monitor_manager
        self.dynamic_manager = dynamic_manager
        self.binding_store = binding_store
        self.db_service = db_service
        self.host = host
        self.port = port
        self.token = token
        self.bot_client = bot_client
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        """启动HTTP服务"""
        app = web.Application()
        app.router.add_get("/", self._handle_home)
        app.router.add_get("/api/status", self._handle_api)
        app.router.add_get("/api/history", self._handle_history)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Web面板已启动: http://{self.host}:{self.port}/")

    async def stop(self) -> None:
        """停止HTTP服务"""
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def _check_token(self, request) -> bool:
        """访问令牌校验（未配置token时不鉴权）"""
        if not self.token:
            return True
        provided = (request.query.get("token")
                    or request.headers.get("X-Web-Token") or "")
        return provided == self.token

    async def _collect(self) -> dict:
        """收集面板数据"""
        now = datetime.now()

        # 机器人凭证状态（access_token，botpy请求级自动刷新）
        token_state = self.bot_client.token_state() if self.bot_client else {
            "client_started": False, "token_ready": False, "token_expires_in": 0}
        # 机器人详情（username/avatar/share_url，获取失败缓存为{}下轮重试）
        me = await self.bot_client.get_me() if self.bot_client else None
        me = me or {}

        # 数据库体积（永久保留策略下的增长监控）
        db_size_mb = 0.0
        db_path = getattr(self.db_service, "db_path", None) or getattr(
            getattr(self.db_service, "db_manager", None), "db_path", None)
        if db_path and os.path.exists(db_path):
            db_size_mb = round(os.path.getsize(db_path) / (1024 * 1024), 2)
        accounts = []
        for a in self.cookie_pool.accounts:
            rooms = self.monitor_manager.get_account_rooms(a.uid) if self.monitor_manager else []
            days_left = None
            if a.sessdata_expires_at is not None:
                days_left = max((a.sessdata_expires_at - now).days, 0)
            accounts.append({
                "uid": a.uid, "nickname": a.nickname, "status": a.status,
                "days_left": days_left,
                "use_count": getattr(a, "use_count", 0),
                "expires": a.sessdata_expires_at.strftime("%m-%d") if a.sessdata_expires_at else None,
                "last_used": a.last_used_at.strftime("%m-%d %H:%M") if a.last_used_at else None,
                "rooms": rooms,
            })

        rooms = []
        for rid in self.monitor_manager.room_ids() if self.monitor_manager else []:
            s = await self.db_service.get_daily_summary(rid, day=now)
            rooms.append({
                "room_id": rid,
                "alive": self.monitor_manager.has(rid),
                "uid": self.monitor_manager.get_cookie_uid(rid),
                "danmaku": s["danmaku_count"],
                "gold": s["gift_gold"], "ship": s["ship_total"], "sc": s["sc_total"],
            })

        dynamics = []
        for uid in self.binding_store.get_dynamic_up_uids():
            dynamics.append({
                "up_uid": uid,
                "name": self.binding_store.get_dynamic_up_name(uid),
                "alive": self.dynamic_manager.has(uid) if self.dynamic_manager else False,
                "groups": len(self.binding_store.get_dynamic_groups(uid)),
            })

        recent = await self.db_service.get_recent_live_status(limit=10)
        return {
            "now": now.strftime("%Y-%m-%d %H:%M:%S"),
            "bot": token_state,
            "me": me,
            "db_size_mb": db_size_mb,
            "accounts": accounts,
            "rooms": rooms,
            "dynamics": dynamics,
            "recent": recent,
        }

    async def _handle_history(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        try:
            days = max(min(int(request.query.get("days", 30)), 365), 1)
        except ValueError:
            days = 30
        history = await self.db_service.get_daily_history(days=days)
        return web.json_response({"days": days, "history": history})

    async def _handle_home(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        data = await self._collect()
        rooms = "".join(
            f"<tr><td>{r['room_id']}</td>"
            f"<td class=\"{'ok' if r['alive'] else 'bad'}\">{'运行中' if r['alive'] else '未运行'}</td>"
            f"<td>{r['uid'] or '-'}</td><td>{r['danmaku']}</td>"
            f"<td>{r['gold']}</td><td>{r['ship']}</td><td>{r['sc']}</td></tr>"
            for r in data["rooms"])
        dynamics = "".join(
            f"<tr><td>{d['up_uid']}</td><td>{d['name'] or '-'}</td>"
            f"<td class=\"{'ok' if d['alive'] else 'bad'}\">{'轮询中' if d['alive'] else '未运行'}</td>"
            f"<td>{d['groups']}</td></tr>"
            for d in data["dynamics"])
        recent = "".join(
            f"<tr><td>{r['end_time'].strftime('%m-%d %H:%M') if r.get('end_time') else '-'}</td>"
            f"<td>{r['room_id']}</td><td>{r['danmaku_count']}</td><td>{r['gold_count']}</td>"
            f"<td>{r['ship_count']}</td><td>{r['sc_count']}</td>"
            f"<td>{(r['duration'] or 0) // 3600}h{(r['duration'] or 0) % 3600 // 60}m</td></tr>"
            for r in data["recent"])
        accounts = "".join(
            f"<tr><td>{a['uid']}</td><td>{a['nickname']}</td>"
            f"<td class=\"{'ok' if a['status'] == 'active' else 'bad'}\">{a['status']}</td>"
            f"<td>{('-' if a['days_left'] is None else str(a['days_left']) + '天')}</td>"
            f"<td>{a['use_count']}</td>"
            f"<td>{a['last_used'] or '-'}</td>"
            f"<td>{'、'.join(map(str, a['rooms'])) or '-'}</td></tr>"
            for a in data["accounts"])
        history = await self.db_service.get_daily_history(days=30)
        # 内嵌JSON供前端JS绘制趋势图（</转义防提前闭合script标签）
        trend_json = json.dumps(history, ensure_ascii=False).replace("</", "<\\/")
        bot = data["bot"]
        me = data.get("me") or {}
        if me.get("username"):
            avatar_html = (f'<img src="{me["avatar"]}" width="48" height="48" '
                           f'style="border-radius:6px"/>' if me.get("avatar") else "-")
            share_html = (f'<a href="{me["share_url"]}" target="_blank">'
                          f'<button style="padding:4px 12px">分享</button></a>'
                          if me.get("share_url") else "-")
            me_row = (f"<tr><td>{avatar_html}</td><td>{me['username']}</td>"
                      f"<td>{share_html}</td></tr>")
        else:
            me_row = '<tr><td colspan=3>暂未获取到机器人详情</td></tr>'
        html = _PAGE.format(
            now=data["now"],
            me_row=me_row,
            bot_ready=bot["token_ready"],
            bot_state=("在线" if bot["client_started"] else "离线"),
            token_expires_in=bot["token_expires_in"],
            db_size_mb=data["db_size_mb"],
            active=sum(1 for a in data["accounts"] if a["status"] == "active"),
            total=len(data["accounts"]), accounts=accounts or "<tr><td colspan=7>空</td></tr>",
            hist_days=30, room_count=len(data["rooms"]), rooms=rooms or "<tr><td colspan=7>无</td></tr>",
            dyn_count=len(data["dynamics"]),
            dynamics=dynamics or "<tr><td colspan=4>无</td></tr>",
            live_records=recent or "<tr><td colspan=7>无</td></tr>",
        )
        html = (html.replace("__TREND_JSON__", trend_json)
                .replace("__TREND_JS__", _TREND_JS))
        return web.Response(text=html, content_type="text/html", charset="utf-8")

    async def _handle_api(self, request):
        if not self._check_token(request):
            return web.Response(status=401, text="unauthorized: token required")
        data = await self._collect()
        return web.json_response(data)
