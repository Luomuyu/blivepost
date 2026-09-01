# BLivePost 项目信息

> 最后更新：2026-09-01（第十三批：工程化——CI/Dockerfile/.env/wheel）

> Git：`04559d8` 基线 → `8625fe3` 面板/健康/重扫 → `e1cc18b` 文档 → 工程化提交（CI/Dockerfile/.env/wheel）

B站直播监控与推送系统：连接B站直播间弹幕服务器（blivedm），将开播/下播、天选、大航海成就、系统事件、房间变更等事件通过QQ官方机器人（botpy）推送到QQ群（各类事件均有独立开关）；弹幕、礼物、舰长、SC、红包仅记录日志并存入数据库（供群指令查询、汇总与CSV/JSON导出），不推送。内置B站账号Cookie池：扫码登录添加账号、多账号轮转监控、房间亲和绑定、临近过期自动刷新、失效自动轮换+每日自动重扫、池耗尽预警并分级私聊提醒管理员。支持群指令订阅B站UP主动态（wbi签名轮询、富媒体推送、开播联动，总开关默认关闭）与可选Web状态面板（token鉴权、SVG趋势图、账号健康）。

## 架构概览

```
主进程（__main__.py）—— 不监控直播间，只负责调度与IO枢纽
  ├─ services/qqbot.py      QQBotClient：botpy封装（群/私聊消息、富媒体、multipart本地图上传）
  ├─ services/commands.py   群指令（绑定/监控/动态订阅/状态/收入[周月]/SC榜/汇总/帮助）
  ├─ services/admin_commands.py 管理员私聊指令（/登录 /账号 /移除账号 /恢复账号 /导出 /我）
  ├─ services/bindings.py   BindingStore：房间绑定 + 动态订阅（内存缓存 + 数据库持久化）
  ├─ services/cookie_pool.py CookiePool：账号池（轮转/亲和/失效/恢复/刷新/耗尽预警/use_count）
  ├─ services/bili_login.py BiliLoginService：Playwright无头扫码登录 + cookie载入刷新 + nav验证
  ├─ services/push.py       PushService：推送模板（YAML可配置）、按房间绑定路由、富媒体
  ├─ services/report.py     DailyReportJob：日/周/月结算推送（仅推有数据房间，模板可配）
  ├─ services/dynamics.py   DynamicManager：UP主动态轮询（wbi签名、多类型提取、开播联动）
  │                         LiveStateWatcher：批量直播状态（单请求全覆盖）→ 状态文件共享
  ├─ services/webpanel.py   WebPanel：只读状态面板（token鉴权、SVG趋势图、账号健康）
  ├─ services/wbi.py        WBI签名单一实现（sign_url_params）
  ├─ config/env.py          .env加载 + 环境变量覆盖（密钥类配置）
  ├─ core/manager.py        MonitorManager + PushQueueConsumer + CookieMaintainer
  ├─ database/              SQLAlchemy异步引擎 + 14张表 + 聚合查询 + 明细导出
  └─ vendor/                blivedm运行时下载（四级回退链）

子进程
  ├─ 房间监控（每房间一个）：blivedm（SESSDATA登录）+ 事件处理 + 90秒状态校正
  └─ 动态轮询（每UP主一个）：wbi签名feed、多类型摘要/首图、读状态文件联动暂停
```

## 工程化（CI/Docker/.env）

- **CI**（.github/workflows/ci.yml）：三个job——
  - `lint`：flake8（配置见.flake8，src+tests零告警）
  - `type-check`：mypy（continue-on-error非阻塞，渐进收紧）
  - `test`：`pytest tests/ -q`（无需网络与凭证）
- **Docker**（Dockerfile）：python:3.12-slim + chromium系统依赖 + `playwright install chromium`；依赖层缓存优化；`/app/data`与`/app/logs`挂载点；`EXPOSE 8080`（面板）；配置经挂载`/app/config.yaml`提供
- **.env与环境变量**（config/env.py）：`.env`文件（KEY=VALUE，#注释，引号可选，不覆盖真实环境变量）+ 环境变量覆盖Settings，前缀`BLIVEPOST_`：
  `BLIVEPOST_QQBOT_APP_ID`/`_APP_SECRET`、`BLIVEPOST_ADMIN_OPENIDS`/`_MAIN_OPENIDS`（逗号分隔）、`BLIVEPOST_SESSDATA`、`BLIVEPOST_WEB_TOKEN`/`_WEB_PORT`、`BLIVEPOST_DB_TYPE/HOST/PORT/USER/PASSWORD/NAME/SQLITE_PATH`、`BLIVEPOST_REDIS_HOST/PORT/PASSWORD`
- **wheel**：`blivepost-1.1.0-py3-none-any.whl`已重新构建（含全部新功能）

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

## Cookie账号池

- **登录**：管理员私聊`/登录`，Playwright无头浏览器扫码（页面内fetch二维码+轮询），完整cookie（20+项）经nav验证后连同昵称UID入库；chromium启动时阻塞安装（已装秒级跳过）
- **房间亲和**：优先沿用上次账号；失效/池空自动LRU轮换；use_count取用计数持久化（`/账号`与面板可见）
- **耗尽预警**：可用账号≤2跨水位私聊预警
- **自动重扫**：每日对expired账号nav复核，登录态恢复自动回池并私聊告知
- **管理命令**（仅管理员私聊）：`/账号`（含使用次数）、`/移除账号 UID`、`/恢复账号 UID`、`/导出 房间号 [日期] [json]`
- `/我`（所有人）：回复发送者openid

## 群指令

- 控制指令（仅`Admin.openids`/`main_openids`名单内）：`/绑定` `/监控` `/取消监控` `/解绑` `/绑定动态` `/取消动态`
- 查询指令（不受限）：`/状态`、`/我的绑定`、`/收入 房间号 [日期/周/月]`、`/SC榜 房间号 [日期]`、`/汇总 [周/月]`、`/帮助`

## Web状态面板

- `Setting.web_panel`（默认N）；`web_host`（127.0.0.1）/`web_port`（8080）/`web_token`（鉴权，留空不鉴权）
- 路由：`/`（HTML：账号健康/房间汇总/SVG趋势图/动态/下播）、`/api/status`、`/api/history?days=N`
- 未配置token时请勿暴露公网

## 数据存储与查询

- SQLAlchemy 2.0异步引擎，SQLite/MySQL/PostgreSQL
- 14张表；单房间/多房间汇总与历史趋势均为聚合SQL；明细导出（CSV/JSON）

## 基础设施

- 配置管理（YAML自动生成 + .env/环境变量覆盖密钥）；SESSDATA仅进程内存保存
- WBI签名单一实现；Redis连接1秒快速降级（进程级记忆）
- 按天轮转日志（保留30天）
- 单元测试143个 + flake8零告警（src+tests）+ mypy渐进收紧
- 版本1.1.0；wheel已重新构建

## 修复批次记录

- **2026-08 第一批**：botpy化、handler去重、SESSDATA装配
- **2026-08 第二批**：异步化、WBI统一、测试从零、子进程化预备
- **2026-09 第三批**：指纹异步化、富媒体扩展、查询指令、推送模板
- **2026-09 第四批**：Cookie账号池、扫码登录、失效轮换
- **2026-09 第五批**：推送范围收敛（5类仅入库）、账号管理命令、通知分级
- **2026-09 第六批**：vendor四级回退链、单条SQL聚合、推送三开关、群权限、chromium自动安装
- **2026-09 第七批**：chromium阻塞安装、权限收敛Admin名单、CSV导出、池预警+亲和
- **2026-09 第八批**：动态监控重做、Web面板、/账号健康、周月结算、/汇总、JSON导出、/我
- **2026-09 第九批**：动态摘要+图片富媒体、开播联动暂停、dynamic_monitor开关
- **2026-09 第十批**：feed wbi签名、多类型提取、批量状态共享、开播提醒
- **2026-09 第十一批**：首次提交、面板token、趋势、健康、自动重扫
- **2026-09 第十二批**：CI、Dockerfile、.env/环境变量覆盖、wheel重新构建、flake8/mypy清理、WebPanel装配修复、Redis快速降级

## 代码存在的问题（当前仍存在）

1. **真机联调未做**：botpy连接、扫码登录、动态轮询、Web面板、结算推送，均未在真实凭证环境验证。
2. **vendor镜像时效性**：第三方镜像可用性随时间变化。
3. **chromium首次安装耗时**：约180MB，启动时阻塞（Docker镜像已预装）。
4. **Web面板鉴权为可选**：未配置web_token时不鉴权（默认仅本机，风险可控）；远程访问应配置token或反代鉴权。
5. **动态feed接口**：摘要与图片提取依赖feed返回结构，结构变更时需适配（已带wbi签名与防御式提取）。
6. **SQLite无迁移机制**：表结构变更需删库重建或手动ALTER；正式发布前建议引入Alembic。
7. **mypy存在约146个历史告警**（CI非阻塞），需渐进收紧。

## 未实现 / 半成品的功能

1. **微博动态监控**：仅B站UP主动态已实现，微博订阅未做。
2. **富媒体场景限制**：media上传在部分群/用户需单独权限申请，配额行为未验证。
3. **Web面板增强**：只读；历史趋势图表已实现，编辑操作与登录体系未做。

## 建议添加的新功能（按优先级）

1. **按群推送粒度**：各类推送对所有监控中的群内容一致；可在绑定关系上增加push_types字段细分。
2. **mypy告警清零**：按模块渐进修复约146个告警并移除CI的continue-on-error。
3. **数据保留策略**：弹幕等高频表自动清理（按天保留N天），防止SQLite无限膨胀。

## 运行要点

```bash
pip install -e .           # 安装（自动安装除blivedm外全部依赖）
pip install -e ".[dev]"    # 开发环境
pytest tests/              # 单元测试（无需联网）
flake8 src/ tests/         # lint（零告警）
blivepost -c config.yaml   # 运行；启动时阻塞安装chromium（已装秒级跳过）

# Docker
docker build -t blivepost .
docker run -v $(pwd)/config.yaml:/app/config.yaml -v $(pwd)/data:/app/data -p 8080:8080 blivepost

# 密钥经环境变量（推荐）或工作目录.env文件
export BLIVEPOST_QQBOT_APP_SECRET="..."   # 敏感信息不落config.yaml
export BLIVEPOST_ADMIN_OPENIDS="openid1,openid2"
```

配置最小集：`QQBot.app_id`/`app_secret`（或对应环境变量）；`Admin.openids`。可选项：`Admin.main_openids`、`Setting.*`推送开关（均默认关闭）、`Setting.web_token`（面板鉴权）。

注意事项：
- 动态监控默认关闭，开启需设置`Setting.dynamic_monitor: "Y"`并重启
- 主动消息有平台频控；`daily/weekly/monthly_report`均消耗配额
- Cookie提醒只走私聊（敏感通知优先`Admin.main_openids`）
- UP主开播期间动态轮询自动暂停，下播自动恢复
- 多实例部署：各实例独立工作目录即可完全隔离
