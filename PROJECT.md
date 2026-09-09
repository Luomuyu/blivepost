# BLivePost 项目信息

> 最后更新：2026-09-03（第二十六~二十九批：qq-botpy-v2替换、union链路修复、chromium运行目录、绑定风控修复、文档重建）

B站直播监控与推送系统：连接B站直播间弹幕服务器（blivedm），将开播/下播、天选、大航海成就、系统事件、房间变更等事件通过QQ官方机器人（自建qq-botpy-v2，接口兼容官方botpy）推送到QQ群（各类事件均有独立开关）；弹幕、礼物、舰长、SC、红包仅记录日志并存入数据库（供群指令查询、汇总与CSV/JSON导出），不推送。内置B站账号Cookie池：扫码登录添加账号、多账号轮转监控、房间亲和绑定、临近过期自动刷新、失效自动轮换+每日自动重扫、池耗尽预警并分级私聊提醒管理员。支持群指令订阅B站UP主动态（wbi签名轮询、富媒体推送、开播联动，总开关默认关闭）与可选Web状态面板（token鉴权、SVG趋势图、账号健康）。

## 架构概览

```
主进程（__main__.py）—— 不监控直播间，只负责调度与IO枢纽
  ├─ services/qqbot.py      QQBotClient：qq-botpy-v2封装（群/私聊消息、富媒体、multipart本地图上传）
  ├─ services/commands.py   群指令（绑定/监控/动态订阅/状态/收入[周月]/SC榜/汇总/帮助）
  ├─ services/admin_commands.py 管理员私聊指令（/登录 /账号 /移除账号 /恢复账号 /导出 /体积 /我）
  ├─ services/bindings.py   BindingStore：房间绑定 + 动态订阅（内存缓存 + 数据库持久化）
  ├─ services/contacts.py   ContactStore（好友/群联系状态与接收开关）+ ContactEventHandler（平台事件）
  ├─ services/cookie_pool.py CookiePool：账号池（轮转/亲和/失效/恢复/刷新/耗尽预警/use_count）
  ├─ services/bili_login.py BiliLoginService：Playwright无头扫码登录 + cookie载入刷新 + nav验证
  ├─ services/push.py       PushService：推送模板（YAML可配置）、按房间绑定路由、富媒体
  ├─ services/report.py     DailyReportJob：日/周/月结算推送（仅推有数据房间，模板可配）
  ├─ services/dynamics.py   DynamicManager：UP主动态轮询（wbi签名、多类型提取、开播联动）
  │                         LiveStateWatcher：批量直播状态（单请求全覆盖）→ 状态文件共享
  ├─ services/webpanel.py   WebPanel：只读状态面板（token鉴权、SVG趋势图、账号健康）
  ├─ services/bilibili.py   B站免签名API（直播间信息/用户名片，规避wbi/acc/info风控）
  ├─ services/wbi.py        WBI签名单一实现（dynamics/fingerprint使用）
  ├─ utils/contact.py       消息发送者身份提取（union_openid > member_openid > user_openid > id）
  ├─ utils/net.py           TCP端口预检（Redis快速跳过）
  ├─ config/env.py          .env加载 + 环境变量覆盖（密钥类配置）
  ├─ core/manager.py        MonitorManager + PushQueueConsumer + CookieMaintainer
  ├─ database/              SQLAlchemy异步引擎 + 14张表 + 聚合查询 + 明细导出 + Schema迁移器
  └─ vendor/                blivedm运行时下载（四级回退链）

子进程
  ├─ 房间监控（每房间一个）：blivedm（SESSDATA登录）+ 事件处理 + 90秒状态校正
  └─ 动态轮询（每UP主一个）：wbi签名feed、多类型摘要/首图、读状态文件联动暂停
```

## 工程化（CI/Docker/.env）

- **CI**（.github/workflows/ci.yml）：lint（flake8）/ type-check（mypy非阻塞）/ test（pytest）
- **Docker**（Dockerfile）：python:3.12-slim + chromium系统依赖；依赖层缓存优化；chromium烘焙至 `/app/ms-playwright`（容器内运行目录）；`/app/data`与`/app/logs`挂载点；`EXPOSE 8080`；配置经挂载`/app/config.yaml`提供
- **chromium安装位置**：本地运行装到运行目录 `ms-playwright/`（`PLAYWRIGHT_BROWSERS_PATH`由bili_login模块设默认值，显式环境变量优先）；`.dockerignore`/`.gitignore`已排除该目录
- **.env与环境变量**（config/env.py）：`.env`文件 + `BLIVEPOST_`前缀环境变量覆盖（`_QQBOT_APP_ID/_APP_SECRET`、`_ADMIN_OPENIDS/_MAIN_OPENIDS`、`_SESSDATA`、`_WEB_TOKEN/_WEB_PORT`、`_DB_*`、`_REDIS_*`）
- **wheel**：`blivepost-1.1.0-py3-none-any.whl`（含py.typed）
- **官方API对齐**（QQ开放平台v2文档核对）：群/单聊消息（`msg_type` 0文本/2markdown/7富媒体，`msg_id`被动5分钟有效，`msg_seq`同ID下须递增）；富媒体分片上传（upload_prepare→PUT→part_finish）；指令面板`/v2/panels`系
- **频控依据**：认证60/qpm、未认证30/qpm、单关系20/qpm、每群每天1000条

## 访问凭证（对齐官方access-token文档）

- 获取：POST `getAppAccessToken`，`access_token`有效期7200秒；botpy在每次API请求前惰性检查并自动刷新
- 携带：请求头 `Authorization: QQBot <token>`；自定义multipart/分片上传同样自动携带
- 诊断：`QQBotClient.token_state()`；Web面板展示在线状态与token剩余秒数
- 安全：app_secret建议经 `.env`/环境变量注入；token仅存进程内存

## 消息推送范围与开关

弹幕、礼物、舰长（上舰）、SC、红包仅**日志+数据库**，不推送。

| 事件 | 配置项 | 默认 |
|------|--------|------|
| 开播/下播 | 始终开启，不设开关 | - |
| 天选时刻 | `Setting.anchor` | N |
| 大航海成就 | `Setting.guard_achievement` | Y |
| 系统事件（切断/警告/禁言/设房管/撤房管） | `Setting.system_event` | Y |
| 房间信息变更 | `Setting.room_change` | N |
| 动态监控（订阅+轮询+推送+开播提醒） | `Setting.dynamic_monitor` | N |
| 每日/每周/每月结算 | `Setting.daily_report`/`weekly_report`/`monthly_report` | N |

## 联系人管理（好友与群）

- **两张数据表**：`friend`（openid唯一、昵称、del_flag、msg_receive、union_openid）、`bot_group`（group_openid唯一、群名、del_flag、msg_receive）
- **8个平台事件接线**：friend_add（入库+author携带union则写入）/friend_del、group_add_robot/group_del_robot（退群自动清理该群全部绑定与监控）、c2c与group的msg_receive/reject开关
- **发送前检查**：PushService发送前经ContactStore过滤未开启接收的群，未知群默认放行
- **union_openid链路**（第二十六批修复后）：qq-botpy-v2在消息author与好友事件author子对象上原生解析union_openid；`extract_sender`按union优先提取发送者（群/私聊权限判定与提示一致）；好友add事件从`event.author.union_openid`写入；私聊消息携带时自动回填好友表（仅更新已有记录）
- **机器人详情**：Web面板展示用户名、头像、分享按钮

## 终端命令与指令面板

- **终端stop命令**：运行时输入 `stop`/`quit`/`exit` 安全关闭（与Ctrl+C等效）
- **日志**：项目logger propagate=False防重复输出；botpy日志经dictConfig纳管（logs/botpy.log按天轮转）
- **指令面板脚本**（scripts/）：panel_create.py（一次创建c2c+group两面板）+ panel_manage.py（query/modify/delete），panel_id存panel_ids.json；端点对齐`/v2/panels`系
- **数据治理**：`/体积`——库文件体积+表行数明细；配合永久保留策略

## Cookie账号池

- **登录**：管理员私聊`/登录`扫码（chromium装于运行目录ms-playwright/，启动时阻塞安装、已装秒级跳过），完整cookie经nav验证入库
- **轮转**：房间亲和优先；失效/池空自动LRU轮换；use_count持久化
- **预警**：可用账号≤2跨水位私聊预警；失效凭证每日自动nav重扫，恢复自动回池
- **管理命令**：`/账号`、`/移除账号 UID`、`/恢复账号 UID`、`/导出 房间号 [日期] [json]`、`/体积`；`/我`（所有人）

## 权限组模型（逐级包含：main ⊇ openids ⊇ default）

- **default**：群查询指令（/状态 /绑定列表 /收入 /SC榜 /汇总 /帮助）+ 私聊 /我
- **管理员组**（`Admin.openids`）：+群控制指令（/绑定 /监控 /取消监控 /解绑 /绑定动态 /取消动态；名单空时不限制）；私聊管理命令（/账号 /移除 /恢复 /导出 /体积）
- **主管理员组**（`Admin.main_openids`）：+敏感通知接收；/登录对 openids∪main 开放
- 名单配置值：与消息sender提取一致（union_openid或member_openid/user_openid）

## Web状态面板

- `Setting.web_panel`（默认N）；web_host/web_port/web_token（留空不鉴权，请勿暴露公网）
- 路由：`/`（HTML面板）与 `/api/status`、`/api/history?days=N`；含数据库体积、账号剩余有效期与使用次数、SVG趋势图

## 数据存储

- SQLAlchemy 2.0异步引擎，SQLite/MySQL/PostgreSQL；14张表；聚合SQL；明细导出CSV/JSON
- **Schema迁移器**（database/migrations.py）：版本表+增量DDL，旧库自动升级（v2 use_count、v3 friend.union_openid）；**任何表结构变更必须同步注册MIGRATIONS条目**
- **永久保留策略**：`data_retention_days=0`，代码不含自动删除；体积超`db_size_warn_mb`（默认500MB）私聊提醒主管理员，治理靠`/导出`归档

## 修复批次记录（摘要）

| 批次 | 主要内容 |
|------|----------|
| 2026-08 第一批 | botpy化、handler去重、SESSDATA装配修复 |
| 2026-08 第二批 | 异步化、WBI统一、测试从零 |
| 2026-09 第三批 | 指纹异步化、富媒体扩展、查询指令、推送模板 |
| 2026-09 第四批 | Cookie账号池、扫码登录、失效轮换 |
| 2026-09 第五批 | 推送范围收敛（5类仅入库）、账号管理命令、通知分级 |
| 2026-09 第六批 | vendor回退链、单条SQL聚合、推送开关、群权限、chromium自动安装 |
| 2026-09 第七批 | chromium阻塞安装、权限收敛、CSV导出、池预警+亲和 |
| 2026-09 第八批 | 动态监控重做、Web面板、周月结算、/汇总、JSON导出、/我 |
| 2026-09 第九批 | 动态摘要+图片富媒体、开播联动、dynamic_monitor开关 |
| 2026-09 第十批 | feed wbi签名、批量状态共享、开播提醒 |
| 2026-09 第十一批 | 首次提交、面板token、趋势、自动重扫 |
| 2026-09 第十二批 | CI、Dockerfile、.env覆盖、wheel构建、mypy清零 |
| 2026-09 第十三批（审查） | pyflakes全清、fingerprint启动崩溃修复、Redis快速降级 |
| 2026-09 第十四批 | Schema迁移器、数据永久保留策略 |
| 2026-09 第十五批 | 官方v2文档对齐（msg_seq/群图直传）、数据量监控 |
| 2026-09 第十六批（审查） | 修数据量提醒链路断裂、maintainer分级丢失 |
| 2026-09 第十七批 | friend/bot_group两表、8事件接线、退群解绑、发送过滤、机器人详情；README配置同步 |
| 2026-09 第十八批 | logger防双输出、终端stop、/体积数据治理、指令面板脚本 |
| 2026-09 第十九批 | Redis连接超时收紧、botpy日志纳管、/登录权限并集修正、版本横幅 |
| 2026-09 第二十批 | 指令面板端点对齐/v2/panels（真机11001反馈） |
| 2026-09 第二十一批 | Redis TCP预检（utils/net.py）、botpy文件日志收口、wheel重构建 |
| 2026-09 第二十二批 | 富媒体分片上传实现（真机40093007）、登录流程seq递增（40054005）、二维码URL回退 |
| 2026-09 第二十三批 | sender提取链（union优先）、friend表union_openid列、分片上传诊断日志 |
| 2026-09 第二十四批 | 迁移v3补注册（旧库union_openid列缺失）；教训：表结构变更必须注册MIGRATIONS |
| 2026-09 第二十五批 | Redis预检根因修复（Windows双栈）、union_openid monkeypatch（已被第二十六批取代） |
| 2026-09 第二十六批 | **qq-botpy-v2替换官方botpy**（已发布PyPI，依赖`>=1.0.0`）；monkeypatch移除（v2原生解析union）；**union回填写线**（私聊消息→friend表，防幻影创建）；friend_add改读author子对象 |
| 2026-09 第二十七批 | **chromium安装至运行目录ms-playwright/**（本地默认+Docker /app/ms-playwright）；.dockerignore/.gitignore排除 |
| 2026-09 第二十八批 | **绑定风控修复**：get_room_info改免签名接口（Room/get_info+用户名片card），弃用wbi/acc/info（真机-352）；房间9646546实测通过 |
| 2026-09 第二十九批（审查） | 第六轮审查：修复2个日期边界测试缺陷（导出文件名写死20260902）→159用例首次全绿；三文档整理重建；wheel重构建 |
| 2026-09 第三十批（真机反馈：stop退出噪音） | botpy的start循环经ensure_future派生ws/心跳子任务且不留句柄，取消start任务不传播——子任务存活到循环拆除被强杀（CancelledError回溯/"会话未正常回队"/Unclosed session噪音）；stop改为先取消start任务、按协程所属模块（botpy.*）清扫余留子任务、最后关http会话；真机stop链路本身已验证正常 |
| 2026-09 第三十一批（Web面板改进） | **修复机器人详情获取**（get_me按dict解析——botpy的http.request返回dict而非User对象，原按对象属性取值恒为空串）；**30秒自动刷新**（meta refresh）；**趋势图重做**：页面内嵌JSON+原生JS按容器宽度绘制SVG（resize重绘、悬停/触摸显示各日明细浮层），替换服务端定宽SVG；**移动端适配**（viewport、≤640px紧凑样式、表格横向滚动容器）；顺手修复`/api/status`缺失token校验；浏览器实测桌面+390px移动端通过 |
| 2026-09 第三十二批（真机反馈：stop噪音仍在+WebUI现代化） | **stop噪音根因补全**：qq-botpy-v2 1.0.1起ws_connect的finally兜底对任何退出（含取消）回队告警、bot_connect捕获BaseException经on_error记录——主动取消必然产生噪音日志；`_PushClient`覆写bot_connect：_stopping置位时静默取消不回队，非关闭期保持回队重连语义，预置_requeued禁用finally兜底。**WebUI现代化重构**：Jinja2模板+HTMX局部刷新（摘要每30秒、趋势区间切换）+Tailwind暗色主题（CDN）；机器人详情卡片化（头像+用户名/AppID/分享竖排）；监控房间表新增主播名列（绑定uid→名片接口，带缓存）；收入趋势区间选择器（近7/14/30天、本月、上月、任意自然月、近12个月按月汇总）；新增get_daily_history_range自然日区间查询。依赖+jinja2；浏览器实测桌面+移动端通过 |
| 2026-09 第三十三批（主播/直播间信息表） | **新增streamer与live_room两张表**（uid/room_id唯一，create_all对旧库自动建表，无需迁移条目）；**绑定时全量落库**：get_status_info_by_uids一次免登录查询同时写两表（uname/face/title/short_id/live_status/分区/封面/关键帧/在线），快照失败退回get_room_info已知字段，均不阻断绑定；**每日0点批量刷新**：StreamerRefresher（manager）按绑定uid分批（50/次）查询并更新快照，启动时表空回填；**WebUI主播名改读数据库**（移除实时名片API与缓存）；**回退bot_connect覆写**（SDK侧将由qq-botpy-v2源码修复，项目侧保留stop()子任务清扫）；依赖钉qq-botpy-v2>=1.0.1 |
| 2026-09 第三十四批（真机反馈：40034105推送失败） | 真机开播推送返回`40034105 主动消息失败,无权限`——**与消息接收开关无关**：接收开关只控制收@消息，推送属主动消息需平台单独授权（未放量约4条/群/月）。发送路径识别40034105/主动消息拒绝→输出每群每6小时一次的可操作提示（申请主动消息放量），替代逐条ERROR；接收开关事件（群/单聊开启关闭）增加INFO日志便于对照；README补主动消息权限说明 |
| 2026-09 第四十五批（群信息快照） | bot_group表新增群简介(group_desc)/群分类(category)/群标签列表(tags,JSON文本)/群成员人数(member_count)四列（迁移v4，旧库启动自动补列）；经群基本信息接口（GET /v2/groups/{openid}/info，白名单30QPM）每日0点定时刷新（GroupInfoRefresher，逐群限流2秒）；启动时存在空值的群立即回填；**任意查询数据项为空时填NULL**（不写空串，tags序列化JSON）；查询失败的群保持NULL待下轮回填 |
| 2026-09 第四十九批（容器化升级） | Dockerfile基础镜像升至**python:3.14-slim**；新增**docker-compose.yml**：data/logs/config挂载默认在本文件同级目录（./data ./logs ./config.yaml:ro），面板端口默认仅映射宿主机127.0.0.1（容器内监听0.0.0.0由环境变量注入），LAN暴露与web_token配置以注释示例；pyproject补3.14分类器。已核实全部编译型依赖均有cp314 manylinux wheel（aiohttp/asyncpg/Brotli/greenlet等）；本地Docker Hub不可达未能实机构建，需在网络可用环境执行docker compose up --build验证 |
| 2026-09 第四十八批（导航激活样式+移动端适配） | **导航激活标签改用专属nav-active类**（主题变量surface-3底+text-strong字+加粗）——修复日间模式下主题覆盖层把bg-slate-800映射为浅底而text-white仍为白的白字浅底不可见问题；导航改两行布局（标题行+可横向滑动导航行，whitespace-nowrap+隐藏滚动条），移动端不再换行挤压；**移动端适配**：≤640px收紧页面留白/表格字号内边距，各表格容器横向滚动，390px实测无页面级溢出 |
| 2026-09 第四十七批（日间/夜间主题） | WebUI支持日间/夜间模式**跟随浏览器prefers-color-scheme自动切换**：base.html新增主题变量层（背景/卡片/边框/文字/状态色/浮层），将模板使用的固定slate色阶工具类映射到CSS变量（!important覆盖CDN Tailwind注入顺序）；趋势图配色随主题自适应（matchMedia监听重绘）；无新增依赖，浏览器实测暗色基线与日间变量注入渲染 |
| 2026-09 第四十六批（WebUI多页面扩展） | **单页拆分为7个子页面**：概览/直播监控/动态监控/账号池/下播记录/收入趋势/群列表（base布局+HTMX导航，各页手动刷新按钮，概览/直播/动态保留30秒自动刷新）；**直播监控页操作**：绑定/监控/取消监控/解绑（群下拉+房间号，normalize走get_room_info，取消监控后无其他监控群自动停进程，解绑同规则）；**动态监控页操作**：绑定动态/取消动态（ensure/remove联动，末群取消自动停轮询）；**账号池页**：Web端扫码登录（on_qrcode回调收PNG，二维码展示+状态2秒轮询+取消按钮，成功自动入池）与逐账号刷新Cookie按钮；**群列表页**：群openid/名称/简介/分类/标签/成员数/接收开关（数据来自bot_group四列，刷新群信息按钮触发白名单接口）；收入趋势/下播记录独立成页。动作端点全部token鉴权+HX-Refresh刷新展示 |
| 2026-09 第四十四批（全推送Markdown→文字两段降级） | 降级链去掉富媒体中间层，统一为**Markdown优先→文字兜底**两段：Markdown原生图片语法已嵌封面/图片（富媒体消息形态冗余），Markdown失败降级纯文本时图片以URL行保留；降级链测试覆盖嵌图/兜底/全失败 |
| 2026-09 第四十三批（时长修复+趋势房间选择+按钮链接回退https） | **下播时长0h0m根因**：_on_live_start先设start_time再调stats.reset()，而reset将start_time置None→duration_seconds守卫返回0（所有场次皆0时长，轮询开播场景最先暴露）；顺序改为reset在前；开播推送时用REST接口live_time回填实际开播时间（取更早值）——轮询/校正晚检出的场次时长同样准确；**按钮跳转链接回退https://live.bilibili.com/房间号**（真机bilibili://协议无法识别）；**WebUI收入趋势新增直播间选择**：下拉框（全部直播间/单独直播间，HTMX切换保留区间状态），数据按房间子库过滤 |
| 2026-09 第四十二批（全推送Markdown优先+按钮链接改回协议） | **所有推送统一Markdown优先降级链**：bot层重构为统一入口`push_message(content, groups, keyboard, media)`（Markdown原生嵌图/按钮→富媒体→文本），push/push_media/broadcast成为兼容入口，push_live_start/push_live_start降级链并入统一链；PushService各发送路径（send_message/push_media_message/from_parts）优先走push_message；动态带图推送、开播提醒等全部受益；**按钮跳转链接改回`bilibili://live/房间号`**（真机确认可跳转）；自定义bot鸭子类型兼容保留 |
| 2026-09 第四十一批（启动迁移日志噪音） | 新装库（子库架构后建库）主库本就没有事件表，启动迁移对每张表报WARNING'no such table'共10条——迁移前经has_table探测，无表静默返回0（旧库照常迁移） |
| 2026-09 第四十批（Markdown嵌封面图） | 按官方markdown文档（原生markdown群聊已对所有机器人开放、免申请）：开播Markdown正文用原生图片语法`![封面 #640px #360px](URL)`嵌入B站封面（URL需公网可访问，平台下载转存）——Markdown形态同时具备封面图+按钮，成功后不再降级富媒体；新增live_markdown_content组装函数（bot层降级链与自定义bot链路共用） |
| 2026-09 第三十九批（真机反馈：开播按钮不显示+链接改https） | **按钮不显示根因**：官方文档中keyboard仅与msg_type=2（Markdown）示例搭配，文本/富媒体消息不渲染按钮——开播推送原走富媒体/文本，按钮自然不出现。重构开播链路：优先**Markdown+按钮**（新send_group_markdown/push_live_start，msg_type=2+keyboard，markdown字段填写后content留空），失败自动降级富媒体（保封面，不带按钮）→文本（含封面URL）；降级链在bot层内部（真实发送结果驱动，调度返回值仅代表已调度）；子进程队列新增push_live_start独立事件类型，主进程消费者执行降级链；**跳转链接改回https://live.bilibili.com/房间号**。注意：群Markdown需平台授权（原生Markdown/模板权限），无权限时自动降级即无按钮——需在开放平台申请 |
| 2026-09 第三十八批（真机反馈：弹幕连接僵死） | 真机出现`_send_heartbeat() failed: ClientConnectionResetError('Cannot write to closing transport')`连续刷屏——blivedm对心跳异常只记WARNING且定时器永远重排，传输层已死但进程存活、join()不返回，监控静默失效（watcher不会重建）。vendor补丁新增心跳失败看门狗：连续3次（约90秒）心跳失败即stop()客户端→join()返回→子进程结束→现有watcher自动重建监控；补丁幂等；Web面板/队列桥零改动 |
| 2026-09 第三十七批（存储架构：每直播间子库） | **直播间事件数据按房间拆分为独立sqlite子库**：弹幕/礼物/舰长/SC/天选/红包/大航海成就/系统事件/房间变更/开播下播10张事件表迁至`data/rooms/<主库文件名>/room_<id>.db`（EventBase元数据，sqlite专用，**不受config数据库类型控制**）；主库仅新增room_db映射表（房间→子库文件名）；DatabaseService路由层：11个写入与单房读取按映射路由，跨房聚合（趋势/汇总/下播记录/表行数）扇出全部子库合并，对外方法签名不变（队列桥/报表/指令零改动）；**旧库事件数据启动自动迁移**（查500→写子库→删主库，数据不丢，幂等）；**动态已见表**：每UP独立子库`data/dynamics/up_<uid>.db`（dynamic_seen表，动态ID持久判重——发布后删除的旧动态/进程重启均不再触发推送，替代旧last_seen内存比较；动态是UP维度故按同样隔离原则落到每UP子库而非房间子库）；Web面板数据库体积改为主库+全部子库合计；新增专项测试（路由/扇出/迁移/动态判重）；174测试全过、静态零告警 |
| 2026-09 第三十六批（开播推送按钮） | 开播推送携带「进入直播间」按钮keyboard：线框样式(style=0)、所有人可点击(permission.type=2)、跳转`bilibili://live/房间号`（唤起B站客户端）；bot层send_group_message/media支持keyboard参数（**带按钮发送失败自动去按钮重发**——按钮需平台单独授权，保证推送必达）；keyboard经子进程队列桥透传（开播推送从房间子进程发出）；富媒体失败回退文本同样携带按钮；自定义bot鸭子类型兼容（签名探测回退旧调用）。后按官方v2群消息接口文档修正按钮字段：style=1蓝色线框（0为灰）、visited_label点击后文字（无visited_color字段）、移除非文档字段clicked_data/at_bot_show、补unsupport_tips版本过低提示 | 新增`Setting.debug`（Y/N，默认N）：开启后收到群聊/私聊消息时输出发送者身份明细（user_openid/member_openid/union_openid/union_user_account/username/member_role/id，仅输出携带的字段；群消息附group_openid）；QQBotClient经构造参数透传至_PushClient；支持.env/环境变量覆盖；README配置样例同步 |

各批次过程细节见REVIEW.md（六轮审查记录）与git历史。

## 真机验证状态

- **已验证**：access_token获取、websocket鉴权与心跳、终端stop、/登录权限判定（main_openids单配）、扫码流程启动、日志输出、Redis预检
- **待复验**：/绑定 端到端（风控修复后）、分片上传富媒体、/v2/panels 指令面板渲染

## 已知问题与风险

1. **vendor镜像时效性**：blivedm运行时下载依赖第三方镜像可用性。
2. **chromium首次安装**：约180MB（现装于运行目录ms-playwright/；Docker镜像已预装）。
3. **Web面板鉴权可选**：未配置web_token时不鉴权（默认仅本机）；远程访问应配置token或反代。
4. **动态feed接口**：依赖feed返回结构，结构变更需适配（已带wbi签名与防御式提取）。
5. **富媒体权限**：media上传在部分群/用户需单独权限申请，配额行为未验证。

## 未实现 / 半成品

1. **微博动态监控**：仅B站UP主动态已实现。
2. **Web面板增强**：只读；编辑操作与登录体系未做。

## 建议添加的新功能（按优先级）

1. **按群推送粒度**：绑定关系增加push_types字段，各群自选订阅事件类型。
2. **失效凭证自动重扫增强**： expired账号密码重登等场景恢复后自动回池的告警优化（每日nav重扫已有）。

## 运行要点

```bash
pip install -e .           # 安装（自动安装全部依赖，含PyPI的qq-botpy-v2）
pip install -e ".[dev]"    # 开发环境
pytest tests/              # 单元测试（159个，无需联网）
flake8 src/ tests/         # lint（零告警）
blivepost -c config.yaml   # 运行；chromium装到运行目录ms-playwright/（已装秒级跳过）

# Docker
docker build -t blivepost .
docker run -v $(pwd)/config.yaml:/app/config.yaml -v $(pwd)/data:/app/data -p 8080:8080 blivepost

# 密钥经环境变量（推荐）或工作目录.env文件
export BLIVEPOST_QQBOT_APP_SECRET="..."   # 敏感信息不落config.yaml
export BLIVEPOST_ADMIN_OPENIDS="openid1,openid2"
```

配置最小集：`QQBot.app_id`/`app_secret`（或对应环境变量）；`Admin.openids`。可选项：`Admin.main_openids`、`Setting.*`推送开关（均默认关闭）、`Setting.web_token`。

注意事项：
- 动态监控默认关闭，开启需`Setting.dynamic_monitor: "Y"`并重启
- 主动消息有平台频控；`daily/weekly/monthly_report`均消耗配额
- Cookie提醒只走私聊（敏感通知优先`Admin.main_openids`）
- UP主开播期间动态轮询自动暂停，下播自动恢复
- Redis不可达时0.5秒TCP预检快速跳过，不拖慢启动
- 多实例部署：各实例独立工作目录即可完全隔离（chromium也随目录隔离）
- 数据库体积超阈值私聊提醒主管理员；代码不自动删除数据
- 审查报告见REVIEW.md（六轮，最新2026-09-03）
