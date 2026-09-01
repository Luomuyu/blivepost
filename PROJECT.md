# BLivePost 项目信息

> 最后更新：2026-09-01（第十批：动态feed签名、状态共享与开播提醒）

B站直播监控与推送系统：连接B站直播间弹幕服务器（blivedm），将开播/下播、天选、大航海成就、系统事件、房间变更等事件通过QQ官方机器人（botpy）推送到QQ群（各类事件均有独立开关）；弹幕、礼物、舰长、SC、红包仅记录日志并存入数据库（供群指令查询、汇总与CSV/JSON导出），不推送。内置B站账号Cookie池：扫码登录添加账号、多账号轮转监控、房间亲和绑定、临近过期自动刷新、失效自动轮换、池耗尽预警并分级私聊提醒管理员。支持群指令订阅B站UP主动态（独立子进程wbi签名轮询，含内容摘要与图片富媒体推送、UP主开播联动暂停与开播提醒，总开关默认关闭）与可选Web状态面板。

## 架构概览

```
主进程（__main__.py）—— 不监控直播间，只负责调度与IO枢纽
  ├─ services/qqbot.py      QQBotClient：botpy封装（群/私聊消息、富媒体、multipart本地图上传）
  ├─ services/commands.py   群指令（绑定/监控/动态订阅/状态/收入[周月]/SC榜/汇总/帮助）
  ├─ services/admin_commands.py 管理员私聊指令（/登录 /账号 /移除账号 /恢复账号 /导出 /我）
  ├─ services/bindings.py   BindingStore：房间绑定 + 动态订阅（内存缓存 + 数据库持久化）
  ├─ services/cookie_pool.py CookiePool：账号池（轮转/亲和取用/失效/恢复/刷新判断/耗尽预警）
  ├─ services/bili_login.py BiliLoginService：Playwright无头扫码登录 + cookie载入刷新 + nav验证
  ├─ services/push.py       PushService：推送模板（YAML可配置）、按房间绑定路由、富媒体
  ├─ services/report.py     DailyReportJob：日/周/月结算推送（仅推有数据房间，模板可配）
  ├─ services/dynamics.py   DynamicManager：UP主动态轮询（每UID一个子进程，wbi签名请求、
  │                         多类型内容提取、直播联动暂停）
  │                         LiveStateWatcher：批量直播状态查询（单请求覆盖全部UID）→ 原子写
  │                         共享状态文件供子进程读取；开播变化检测经队列推送订阅群
  ├─ services/webpanel.py   WebPanel：只读状态面板（aiohttp，账号/房间/动态/下播，默认关闭）
  ├─ services/wbi.py        WBI签名单一实现（sign_url_params供feed签名）
  ├─ core/manager.py        MonitorManager：房间子进程管理 + 队列消费（推送路由+落库+失效轮转
  │                         +动态富媒体推送+开播提醒）
  │                         CookieMaintainer：Cookie维护循环
  ├─ database/              SQLAlchemy异步引擎 + 14张表 + 统计聚合（单条SQL）+ 明细导出
  └─ vendor/                blivedm运行时下载（四级回退链）

子进程
  ├─ 房间监控（每房间一个）：blivedm客户端（SESSDATA登录）+ 事件处理 + 90秒状态校正
  └─ 动态轮询（每UP主一个）：60-90秒随机轮询（wbi签名feed，多类型摘要/首图提取，
     读共享状态文件联动暂停），新动态经队列入主进程富媒体推送
```

## 动态监控

- **总开关**：`Setting.dynamic_monitor`（**默认N关闭**）。关闭时`/绑定动态`回复"未启用"，启动恢复跳过；开启后订阅指令可用
- **订阅**：群内@机器人 `/绑定动态 UP主UID`，`/取消动态 UP主UID` 取消（均需管理员名单权限）
- **轮询**：每UP主一个独立子进程，60-90秒随机间隔查询动态feed接口（**wbi签名请求**，签名不可用时自动降级为无签名URL；跳过置顶，dynamic_id递增判新）
- **推送内容**：作者、发布时间、内容摘要、opus链接；**带图动态走富媒体推送**（单群失败自动回退文本）
- **内容提取覆盖多类型**：图文（draw首图）、投稿视频（archive描述+封面）、专栏（opus标题+配图）、转发（附言+被转发内容摘要）；提取全程防御式，结构异常不崩溃
- **开播联动**：LiveStateWatcher（主进程）每10分钟**单次批量请求**查询全部订阅UP主的直播状态（`get_status_info_by_uids`支持批量），原子写入共享状态文件；轮询子进程读文件联动暂停（在播时待机5分钟），消除各进程独立请求的线性增长
- **开播提醒**：watcher检测到"未播→在播"变化（跨轮去重）经队列入主进程，推送到订阅该UP主的群（含房间号与标题）——订阅动态即获得开播提醒
- 每UID全局仅一个轮询进程；死亡watcher即时重建；最后一个订阅群取消后自动停止；重启自动恢复（仅开关开启时）；UP主昵称首次推送时自动回填

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
| 每日结算（00:05，仅推有数据房间，模板可配） | `Setting.daily_report` | N |
| 每周结算（周一00:05推上周汇总） | `Setting.weekly_report` | N |
| 每月结算（每月1日00:05推上月汇总） | `Setting.monthly_report` | N |

## Cookie账号池

- **登录**：管理员私聊`/登录`，无头浏览器扫码（页面内fetch二维码+轮询），完整cookie（20+项）经nav验证后连同昵称UID入库；chromium内核启动时阻塞安装（已装秒级跳过）
- **房间亲和**：房间优先沿用上次账号；失效/池空自动LRU轮换
- **耗尽预警**：可用账号降至阈值（默认≤2）跨水位私聊预警
- **管理命令**（仅管理员私聊）：`/账号`（池状态+关联房间+最近使用）、`/移除账号 UID`、`/恢复账号 UID`（nav验证）、`/导出 房间号 [日期] [json]`（明细CSV/JSON）
- `/我`（私聊，所有人可用）：回复发送者openid

## 群指令

- 控制指令（仅`Admin.openids`/`main_openids`名单内用户）：`/绑定` `/监控` `/取消监控` `/解绑` `/绑定动态` `/取消动态`
- 查询指令（不受限）：`/状态`、`/我的绑定`、`/收入 房间号 [日期/周/月]`、`/SC榜 房间号 [日期]`、`/汇总 [周/月]`、`/帮助`

## Web状态面板

- `Setting.web_panel`开关（默认N）；`web_host`默认127.0.0.1、`web_port`默认8080
- 只读展示：账号池、监控房间（进程状态+当日汇总）、动态订阅（轮询状态+订阅群数）、最近10场下播记录
- 路由：`/`（HTML）、`/api/status`（JSON）；**无鉴权，勿暴露公网**

## 数据存储与查询

- SQLAlchemy 2.0异步引擎，SQLite/MySQL/PostgreSQL
- 14张表；单房间/多房间汇总单条SQL；时间段汇总（周/月）；明细导出（CSV/JSON）

## 基础设施

- 配置管理（YAML自动生成默认配置）；SESSDATA仅进程内存保存
- WBI签名单一实现（`sign_url_params`供外部URL签名）；指纹服务异步入口
- 按天轮转日志（保留30天）
- 单元测试123个（pytest + pytest-asyncio）
- 版本1.1.0

## 已修复的问题（历史批次）

### 2026-08 第一批 ~ 2026-09 第五批
- botpy化/handler去重/异步化/WBI统一/测试/子进程化/富媒体/账号池/通知分级等

### 2026-09 第六批
- vendor四级回退链、统计单条SQL、推送三开关、群权限、日期查询与每日结算、chromium自动安装

### 2026-09 第七批
- chromium阻塞安装、群权限收敛至Admin名单、CSV导出、池预警+亲和绑定、结算模板化+周月查询

### 2026-09 第八批
- 动态监控重做、Web面板、/账号关联房间、周月自动结算、/汇总、JSON导出、/我命令

### 2026-09 第九批
- 动态推送摘要+图片富媒体、UP主开播联动暂停、dynamic_monitor总开关（默认关闭）

### 2026-09 第十批
- 动态feed wbi签名+降级、多类型内容提取、批量直播状态共享、开播提醒经订阅群

### 2026-09 第十一批（本次）
- ~~直播状态文件位于系统临时目录（多实例同机互相覆盖）~~ → 状态文件迁移至**数据库目录**（`dynamic_live_state.json`与sqlite同目录，实例私有），路径经`state_path`参数贯通DynamicManager/子进程/watcher，多实例同机互不干扰
- ~~动态feed未带wbi签名~~ → `_build_dynamic_url`经`WbiSigner.sign_url_params`签名（host_mid+platform参数），签名不可用自动降级无签名URL
- ~~内容提取依赖单一结构~~ → `_extract_content`防御式多类型提取：图文/投稿视频/专栏/转发，结构异常安全返回空
- ~~直播状态各轮询进程独立查询线性增长~~ → `LiveStateWatcher`主进程单次批量请求全部UID，原子写共享状态文件；子进程读文件联动暂停
- ~~开播无提醒渠道~~ → watcher检测"未播→在播"变化（跨轮去重）经队列推送订阅群（房间号+标题）

## 代码存在的问题（当前仍存在）

1. **仓库尚未提交**：所有改动仍在工作区，无任何commit；`dist/`下的wheel需重新构建。
2. **真机联调未做**：botpy连接、扫码登录、动态轮询（wbi签名的feed请求未在真实环境验证）、Web面板、结算推送，均未在真实凭证环境验证。
3. **vendor镜像时效性**：第三方镜像可用性随时间变化。
4. **chromium首次安装耗时**：约180MB，启动时阻塞。
5. **Web面板无鉴权**：默认仅绑定127.0.0.1，请勿暴露公网；如需远程访问应自行加反代鉴权。

## 未实现 / 半成品的功能

1. **微博动态监控**：仅B站UP主动态已实现，微博订阅未做。
2. **富媒体场景限制**：media上传在部分群/用户需单独权限申请，配额行为未验证。
3. **Web面板增强**：当前只读且无鉴权，编辑操作与登录鉴权未做。

## 建议添加的新功能（按优先级）

1. **按群推送粒度**：各类推送对所有监控中的群内容一致；可在绑定关系上增加push_types字段细分。
2. **Web面板增强**：简单token鉴权、历史趋势图表、远程访问支持。
3. **工程化**：CI（lint+mypy+pytest）、Dockerfile、密钥环境变量覆盖（.env）、重新构建wheel并完成首次git提交。

## 运行要点

```bash
pip install -e .           # 安装（自动安装除blivedm外全部依赖）
pip install -e ".[dev]"    # 开发环境
pytest tests/              # 单元测试（无需联网）
blivepost -c config.yaml   # 运行；启动时阻塞安装chromium（已装秒级跳过）
```

配置最小集：`QQBot.app_id`/`app_secret`；`Admin.openids`。可选项：`Admin.main_openids`、`Setting.*`推送开关（含`dynamic_monitor`、`web_panel`，均默认关闭）。

注意事项：
- 动态监控默认关闭，开启需设置`Setting.dynamic_monitor: "Y"`并重启
- 动态订阅群自动获得开播提醒（订阅即生效，无需额外指令）
- Web面板无鉴权，默认仅本机访问；`daily_report`/`weekly_report`/`monthly_report`均消耗主动消息配额
- Cookie相关提醒只走私聊（敏感通知优先发`Admin.main_openids`）
- 账号池为空时房间监控以游客身份连接；动态轮询游客可查但可能受风控限制
- UP主开播期间其动态轮询自动暂停（共享状态文件联动，文件位于数据库目录、实例私有），下播自动恢复
- 多实例同机部署时各实例使用独立工作目录/数据库路径即可完全隔离
