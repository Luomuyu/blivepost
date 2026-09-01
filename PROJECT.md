# BLivePost 项目信息

> 最后更新：2026-09-01（第十五批：官方API对齐、数据量监控）

> Git：`04559d8` 基线 → `8625fe3` → `e1cc18b` → `66ebdb4` 工程化 → `ff57e37` → `04f7c9a` 迁移器+mypy清零 → `673eef8` 官方API对齐+数据量监控 → `51fb0a1` 字段补齐

> Git：`04559d8` 基线 → `8625fe3` → `e1cc18b` → `66ebdb4` 工程化 → `ff57e37` → `04f7c9a` 迁移器+mypy清零

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
- **官方API对齐**（QQ开放平台v2文档核对）：群/单聊消息（`msg_type` 0文本/2markdown/7富媒体，`media.file_info`来自files上传，`msg_id`被动5分钟有效，`msg_seq`同ID下须递增）；富媒体上传（`file_type`/`url`/`srv_send_msg`，本地图片multipart `file_data`直传为平台兼容路径，文档标准路径为分片上传——需真机验证，失败时日志提示）
- **频控依据**：认证60/qpm、未认证30/qpm、单关系20/qpm、每群每天1000条——结算推送等高频功能设计均以此为约束

## 访问凭证（对齐官方access-token文档）

- **获取**：POST `getAppAccessToken`（appId+clientSecret），返回 `access_token`（有效期7200秒）；有效期内重复请求返回相同token
- **刷新**：botpy在**每次API请求前**执行 `check_session→check_token`，过期自动刷新并重建Authorization头——请求级惰性刷新，等效并优于文档建议的定时刷新
- **携带**：所有请求头 `Authorization: QQBot <access_token>`；自定义multipart上传同样经 `api._http.request` 自动携带
- **域名说明**：botpy使用 `bots.qq.com/app/getAppAccessToken`（官方SDK地址）；文档为 `api.bot.qq.com`——两者均为官方入口
- **诊断**：`QQBotClient.token_state()` 暴露凭证状态（client_started/token_ready/token_expires_in）；Web面板首页展示在线状态与token剩余秒数
- **安全**：app_secret建议经 `.env`/环境变量注入（`BLIVEPOST_QQBOT_APP_SECRET`）；token仅存进程内存

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

## 数据量监控（永久保留策略的配套）

- 设计决策：**所有入库数据永久保留，永不自动删除**（`Setting.data_retention_days=0`，代码不含自动删除逻辑）
- **Web面板**：首页展示数据库体积（MB），随 `/api/status` 一并输出JSON
- **超阈值提醒**：`Setting.db_size_warn_mb`（默认500MB，0=不提醒）；CookieMaintainer每轮维护时检查SQLite文件体积，超阈值经**单聊私聊提醒主管理员**，恢复到阈值下方前不重复提醒——只提醒不删除，符合永久保留决策
- 体积持续增长的治理路径：`/导出`归档 → 手动清理 → 迁移MySQL/PostgreSQL

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
- 路由：`/`（HTML：数据库体积/账号健康/房间汇总/SVG趋势图/动态/下播）、`/api/status`（含db_size_mb）、`/api/history?days=N`
- 账号池表含**剩余有效期天数**与**使用次数**
- 未配置token时请勿暴露公网

## 数据存储与查询

- SQLAlchemy 2.0异步引擎，SQLite/MySQL/PostgreSQL
- 14张表；单房间/多房间汇总与历史趋势均为聚合SQL；明细导出（CSV/JSON）
- **Schema迁移**：轻量版本表迁移器（`database/migrations.py`），表结构增量变更自动应用，新旧库均安全
- **数据保留**：永久保留策略（`Setting.data_retention_days=0`），代码不含任何自动删除逻辑
- 数据库体积监控：Web面板展示 + 超阈值私聊提醒（`db_size_warn_mb`，跨水位去重）

## 基础设施

- 配置管理（YAML自动生成 + .env/环境变量覆盖密钥）；SESSDATA仅进程内存保存
- WBI签名单一实现；Redis连接1秒快速降级（进程级记忆）
- 按天轮转日志（保留30天）
- 单元测试154个 + flake8/pyflakes/mypy三项零告警（mypy阻塞检查）
- **wheel构建验证**：43文件（含py.typed），关键模块与16项运行时依赖核对通过
- Schema迁移器（database/migrations.py，版本表+增量DDL）
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
- **2026-09 第十三批（审查）**：pyflakes全清、修复fingerprint缺random/Tuple导入（启动即崩）、finally变量未初始化、Redis 1秒快速降级、死代码清理4项、README重写结构/架构图
- **2026-09 第十四批（本次）**：
  - ~~SQLite无迁移机制~~ → 轻量Schema迁移器（`schema_version`版本表+增量ADD COLUMN，跨sqlite/mysql/pg，列存在自动跳过；`DatabaseManager.initialize`集成，旧库升级/新库幂等均有测试）
  - ~~mypy约146个历史告警（CI非阻塞）~~ → 配置放宽动态鸭子类型项+修复真实类型错误，**36文件0错误**；CI的type-check移除continue-on-error升为阻塞检查
  - ~~数据保留策略~~ → 设计决策确认：**所有入库数据永久保留，永不自动删除**（`Setting.data_retention_days`默认0=永久；用户可自行导出后手动清理，代码不提供自动删除逻辑）

### 2026-09 第十五批（本次）
- ~~消息/富媒体实现未经官方文档核对~~ → 按v2文档逐项核对并完善：`msg_seq`递增机制（修复登录流程同seq冲突）、群聊本地图片multipart直传（`send_group_image`，与单聊对称）、富媒体上传路径说明与日志提示
- ~~数据量监控缺失~~ → Web面板展示数据库体积；`db_size_warn_mb`超阈值私聊提醒主管理员（跨水位去重、只提醒不删除）

### 2026-09 第十三批（审查）+ 第十四批 + 第十五批
- 第十三批（审查）：pyflakes全清、fingerprint启动崩溃修复、Redis快速降级、死代码清理、README重写（详见REVIEW.md第一轮）
- 第十四批：Schema迁移器（版本表+增量加列，旧库升级/新库跳过）、mypy清零（36文件0错误）、CI阻塞化、数据永久保留策略（`data_retention_days=0`）
- 第十五批：按官方v2文档对齐消息/富媒体实现（msg_seq递增修复登录流程冲突、群聊本地图片直传、富媒体上传路径标注）、数据量监控（面板体积+超阈值私聊提醒）
- **2026-09-02 第二轮审查**：修复数据量提醒链路断裂（db_path取值错误致功能失效）与maintainer分级丢失（回滚所致），新增3个端到端测试；149个测试全过，pyflakes/flake8/mypy三项零告警
- **2026-09-02 第十六批（第三轮审查+wheel）**：修复CookieMaintainer双重体积检查（绕过开关配置）；notify_admins字符串入参防御；动态昵称回填去重；删除死代码（push.qqbot_config、fingerprint.build_signed_url）；**补齐py.typed并重新构建wheel**（43文件，内容完整性已验证）

### 2026-09-02 第十七批（本次，凭证诊断）
- 按官方access-token文档核对凭证链路：获取接口/7200秒有效期/请求级自动刷新/QQBot头携带——实现符合文档
- 新增 `QQBotClient.token_state()` 凭证诊断；Web面板展示机器人在线状态与token剩余秒数
- 154个测试

## 代码存在的问题（当前仍存在）

1. **真机联调未做**：botpy连接、扫码登录、动态轮询、Web面板、结算推送，均未在真实凭证环境验证。
2. **vendor镜像时效性**：第三方镜像可用性随时间变化。
3. **chromium首次安装耗时**：约180MB，启动时阻塞（Docker镜像已预装）。
4. **Web面板鉴权为可选**：未配置web_token时不鉴权（默认仅本机，风险可控）；远程访问应配置token或反代鉴权。
5. **动态feed接口**：摘要与图片提取依赖feed返回结构，结构变更时需适配（已带wbi签名与防御式提取）。

## 未实现 / 半成品的功能

1. **微博动态监控**：仅B站UP主动态已实现，微博订阅未做。
2. **富媒体场景限制**：media上传在部分群/用户需单独权限申请，配额行为未验证。
3. **Web面板增强**：只读；历史趋势图表已实现，编辑操作与登录体系未做。

## 建议添加的新功能（按优先级）

1. **按群推送粒度**：各类推送对所有监控中的群内容一致；可在绑定关系上增加push_types字段细分。
2. **数据量监控**：永久保留策略下SQLite文件会持续增长，可在Web面板展示数据库体积并在超阈值时提醒管理员（提醒而非删除，符合永久保留决策）。

## 运行要点

```bash
pip install -e .           # 安装（自动安装除blivedm外全部依赖）
pip install -e ".[dev]"    # 开发环境
pytest tests/              # 单元测试（146个，无需联网）
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
- 访问凭证7200秒有效，botpy在每次API请求前自动刷新；Web面板可见剩余秒数
- 数据库体积超`db_size_warn_mb`（默认500MB）时私聊提醒主管理员；治理靠导出归档，代码不自动删除
- 审查报告见REVIEW.md（两轮，第二轮2026-09-02）
