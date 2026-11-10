# BLivePost 交接文档

> 生成日期：2026-11-11（v1.2.0全项目审查后更新） ｜ 对应版本：v1.2.0（第十一~十六批+审查修复已并入；正文历史批次描述未逐条重写，以 git log 与 README 更新日志为准）
> 用途：供后续开发者/维护者快速接手。项目细节以 `PROJECT.md`（批次完整记录）、`README.md`（用户视角）、`REVIEW.md`（审查记录，第七轮=2026-11-10四阶段整改）为准，本文档是入口与摘要。

---

## 一、项目概述

**BLivePost** 是 B站直播监控与 QQ 群推送系统：监控 B 站直播间（弹幕/礼物/舰长/SC/天选等事件入库），经自建 [qq-botpy-v2](https://github.com/tencent-connect/botpy)（PyPI 包，import 名仍为 `botpy`）向 QQ 群主动推送开播/下播/事件/结算消息，并带有一个功能完整的 Web 管理面板（登录体系 + 管理后台 + 数据大屏 + 词云）。

- **语言/运行时**：Python ≥3.11（开发机 3.14），异步 aiohttp + SQLAlchemy 2.x async
- **监控**：blivedm（vendor 内置 + 补丁，见 `_vendor/`）
- **WebUI**：aiohttp + Jinja2 + HTMX（CDN）+ Tailwind（CDN，Play 模式）
- **分词**：jieba ≥0.42（纯 Python；缺失时词云自动退回二元组切分）
- **存储**：SQLite（默认）/ MySQL / PostgreSQL，弹幕事件按直播间拆独立 SQLite 子库
- **部署形态**：本地长驻进程 / Docker（`Dockerfile` + `docker-compose.yml`，python:3.14-slim 基础镜像，chromium 预装于 `/app/ms-playwright`）

## 二、代码结构地图

```
src/blivepost/
├── __main__.py            # 启动装配：日志→配置→数据库→播种→各服务→恢复现场（finally全组件预清+按序stop）
├── config/settings.py     # Settings/MessageSettings（web_admin_user/password 在此）+样例config注释
├── config/env.py          # 环境变量覆盖映射（BLIVEPOST_*，含 WEB_HOST/WEB_PORT）
├── core/
│   ├── handler.py         # 直播事件处理器（子进程内运行；推送不再做开关门控，恒转发）
│   ├── manager.py         # RoomProcess/MonitorManager（崩溃退避重建）+ re-export 子模块公共名
│   ├── room_process.py    # 子进程侧：QueueDbBridge/QueuePushService/_room_process_main
│   ├── consumer.py        # PushQueueConsumer（switch标签+群独立设置过滤收群；动态/开播提醒推送）
│   └── maintenance.py     # CookieMaintainer/StreamerRefresher/GroupInfoRefresher
├── database/
│   ├── models.py          # Base(主库) + EventBase(子库) 两套元数据；全部表定义
│   ├── manager.py         # DatabaseManager（引擎/建表/迁移入口；SQLite WAL+busy_timeout；密码URL转义）
│   ├── migrations.py      # DDL迁移(v2~v6) + MIGRATION_HANDLERS函数型迁移(v5)
│   ├── service.py         # DatabaseService 组合类 + 子库路由（房间→子库名进程内缓存）
│   ├── service_events.py  # EventWriterMixin：全部事件写入
│   ├── service_stats.py   # StatsMixin：汇总/历史/词云查询
│   ├── service_admin.py   # AdminMixin：账号池/开关/Web用户/导出/旧库迁移
│   └── room_store.py      # RoomDbManager/Registry（并发首建锁+close_all）
├── services/
│   ├── webpanel.py        # WebPanel 组合类（路由注册；五Mixin拆分见 webpanel_*.py）
│   ├── webpanel_common.py # 共享：模板环境/SWITCH_DEFS/口令哈希/日期工具
│   ├── webpanel_auth.py   # AuthMixin：会话/token/登录限速锁定/扫码会话隔离
│   ├── webpanel_actions.py# ActionsMixin：全部 _action_* 写路径
│   ├── webpanel_pages.py  # PagesMixin：页面上下文/_collect/词云/趋势/API
│   ├── webpanel_admin.py  # AdminMixin：后台页/导出（默认脱敏+VACUUM INTO）
│   ├── webpanel_screen.py # ScreenMixin：数据大屏
│   ├── bindings.py        # BindingStore 群绑定+动态订阅（bind/unbind时播种/清理群推送设置）
│   ├── push.py            # PushService：Markdown→富媒体→文本降级链；_switch/_groups标记上下文
│   ├── report.py          # DailyReportJob 日/周/月结算（失败当日重试；发送前查运行时开关+按群过滤）
│   ├── commands.py        # QQ群指令（/监控 /取消监控 /查询 等；绑定/解绑指令已移除且正则不捕获）
│   ├── admin_commands.py  # 私聊管理员指令
│   ├── qqbot.py           # QQBotClient（websocket、主动消息、有界发送队列+worker背压）
│   ├── cookie_pool.py     # B站账号池（轮转/亲和/失效上报）
│   ├── bili_login.py      # B站扫码登录（playwright chromium 装运行目录 ms-playwright/）
│   ├── bilibili.py        # B站API（免签名 get_info+card，规避-352风控）
│   ├── dynamics.py        # 动态轮询（每UP独立子进程+每UP已见动态子库）
│   ├── contacts.py        # 联系人（friend/bot_group；退群清理保留他群监控）
│   ├── wordcloud.py       # jieba分词统计（停用词过滤；jieba缺失退回二元组）
│   └── templates/         # Jinja2模板（base布局+8页面+partials；screen.html独立大屏布局）
└── utils/logger.py        # 日志（apply_debug：DEBUG日志由Setting.debug控制；静默jieba噪音）
```

## 三、数据存储结构

- **主库**（`data/blivepost.db`，类型可配 MySQL/PG）：绑定关系 `group_binding`、`bot_group`（群简介/分类/标签/成员数）、`friend`、`cookie_account`、`dynamic_binding`、`room_db`（房间→子库映射）、`web_user`（前台用户，pbkdf2口令哈希）、`push_switch`（全局推送开关，enabled+allow_modify）、`group_at_all`（群艾特全体当日使用记录，第八批）。
- **房间子库** `data/rooms/room_<id>.db`：10张事件表 + **`group_push_setting`**（第六十批新增：每绑定群对本房间7项推送开关；行缺省回落全局开关，绑定/开启监控时自动播种继承全局，解绑清理）。
- **动态子库** `data/dynamics/up_<uid>.db`：`dynamic_seen`（已见动态判重）。
- 迁移：启动自动跑（旧库事件表搬子库、列补齐、monitored列类型更正等）；**新表靠 create_all 自动建，无需迁移条目**；改列类型等DDL覆盖不了的变更必须注册 MIGRATION_HANDLERS（第二十四批教训）。

## 四、鉴权与权限模型（第五十九批起，重要）

| 角色 | 来源 | 权限 |
|---|---|---|
| 游客 | 默认 | 只读浏览所有页面与只读接口；**一切动作接口 401** |
| 前台用户 | 后台添加，存 `web_user` 表 | 可操作（添加房间/推送群/动态订阅/扫码登录等）；可改「前台可改(allow_modify)」的全局与群独立开关 |
| 管理员 | `Setting.web_admin_user/web_admin_password`（**口令非空才启用**） | 全部权限 + `/admin` 后台（用户增改删/全局推送开关/数据导出） |
| web_token 快捷令牌 | `Setting.web_token` | `?token=` 即建管理员会话（老令牌兼容） |

- 会话：HMAC签名 sid + cookie `blivepost_sid`（7天）。web_token 非空时密钥由其派生，**登录态跨进程重启保留**；未配置 web_token 时进程随机密钥（重启失效）；修改 web_token=全量登出。单会话吊销=jti内存黑名单（v1.2.0起，登出即吊销；重启清空，改web_token全量登出）（第八批签名会话，第十五批补吊销）
- 群独立推送设置的修改权限沿用对应全局开关的 allow_modify；管理员不受限。
- `/screen` 数据大屏需登录；`/admin` 需管理员。

## 五、本开发周期完成内容（第五十一～六十六批，详见 PROJECT.md 批次表）

**WebUI 交互重构（51~58批）**
- 直播/动态监控页均拆「上半段管理表 + 下半段群绑定状态」两段；群绑定行点击展开群详情（群信息+推送设置徽标）
- 推送群选择改行内展开编辑器（多选下拉/清空/取消/保存），编辑期间暂停自动刷新（ui-busy），展开状态跨刷新保留
- 删除模式（勾选+确认删除+取消）；动态页 enterDynAddMode 缺失等按钮失效修复

**登录体系与管理后台（59批）**
- 游客只读/用户可控/管理员可管三层鉴权；`/login` 账号密码登录；手动刷新按钮改图标；页头身份区（游客→登录/用户名/大屏/后台/退出）
- 后台：前台用户管理、全局推送设置（状态+是否允许前台修改）、数据导出（主库+子库 zip）
- **推送开关运行时化**：`push_switch` 表（启动按 config 播种缺失行）；子进程推送消息携带开关 tag，主进程消费者按 DB 判定发送/丢弃；动态监控开关关→停全部轮询、开→自动恢复

**群独立推送设置（60批）**
- 房间子库 `group_push_setting` 表；创建继承全局、本群设置优先；结算推送同规则按群过滤；解绑清理重绑重继承

**词云（61/62/65/66批）**
- `/wordcloud` 页：按天（今天/昨天/指定天）、近3/7/14天、按月（本月/上月/指定月）、按年（今年/去年/指定年）× 全部/单独直播间
- jieba 分词 + 停用词过滤，Top100；圆形布局（黄金角螺旋+碰撞检测，高频大词居中）；导出 PNG（SVG→Canvas 2x，含主题底色与范围标签）
- 两次修复：首次渲染锚定错位（Tailwind CDN/HTMX settle 时序竞争 → 容器自带 relative + 双rAF/load/settle 三重校正）；容器高度改按词云实际铺开范围自适应（卡片随之伸缩）

**数据大屏（63批）**
- `/screen` 独立全屏布局：实时时钟、六张KPI卡、近7天趋势（柱=弹幕/线=礼物）、房间今日排行、最近下播；30秒 HTMX 轮询自动刷新；登录后页头「大屏」入口（新窗口）

**日志（64批）**
- `Setting.debug` 控制 DEBUG 日志输出（默认 INFO）；jieba 词典构建噪音默认静默

**第七批全项目审查整改（2026-11-10，详见 REVIEW.md 第七轮）**
- **P0修复**：动态推送/UP开播提醒 `await` 同步方法100%失败、`/绑定` 正则残留反向执行、退群误停他群监控、Docker BLIVEPOST_WEB_HOST 缺键、启动早期 finally NameError
- **可靠性**：房间→子库映射热路径缓存、verify_nav 出循环、日结算当日重试、崩溃重建指数退避、优雅关闭补全、qqbot 有界发送队列、SQLite WAL
- **安全**：登录限速锁定、扫码会话隔离、导出默认脱敏（VACUUM INTO+清cookie_account）、web_token 常量时间比较+不落cookie、/api/status 需登录
- **拆分**：service.py(1940→100)、webpanel.py(1482→186)、manager.py(1336→446) 拆为12个领域模块，公共导入路径不变
- **测试**：200→**251**；新增 qqbot 发送链路18例、__main__ 冒烟11例、审查回归37例；pyflakes/mypy 双零

**第八批（2026-11-10，遗留清零+新功能）**
- **开播提醒自动艾特全体**：每群每自然日1次（平台配额），当天首次开播提醒（房间开播推送与UP开播提醒均计）自动在正文嵌入 `<qqbot-at-everyone />`（QQ官方文本交互嵌入格式，Markdown与文本降级均生效）；配额登记主库 `group_at_all` 表（调度即占用，发送失败不退还——保守防重复艾特）；消费者判定配额，bot层逐群嵌入；自定义bot经签名探测兼容（不支持 at_all_groups 参数则原样发送）
- **遗留清零**：push上下文改contextvars（并发安全）、webpanel N+1并行化+主播名TTL缓存、动态轮询WbiSigner复用（nav请求~1次/小时）+漏推中间动态修复（全部未见动态按时间序推送）+连续异常告警（10轮≈15分钟私聊管理员）、子进程独立日志文件（room_<id>/dyn_<uid>后缀，防多进程轮转冲突）、会话签名cookie持久化（HMAC签名sid，web_token派生密钥，重启不掉登录）、群推送设置QQ指令入口（/推送设置 概览/明细/单项开关/恢复，权限同控制指令）、大屏主题跟随系统日间模式（CSS变量+prefers-color-scheme）
- **开播/下播消息形态**（第八批末追加）：下播消息移除"直播地址："行；开播消息仅 Markdown 形态移除该行（跳转入口仅保留「进入直播间」按钮），富媒体与文本降级保留；按钮 keyboard 的 data 由 `bilibili://live/{id}` 改为 `https://live.bilibili.com/{id}`（QQ 不支持协议链接拉起B站客户端）
- 测试：251→**318**（+67）；pyflakes/mypy 双零；wheel 76文件

**第十批（2026-11-10，第九轮复审整改+遗留清零，详见 REVIEW.md 第九/十轮）**
- **回退**：「平台标签剥离」按用户结论回退——@全体配额为平台每群每日1次硬限制，正文注入标签无法绕过
- **第九轮修复**：`/绑定动态` 指令失效（CommandHandler缺dynamic_manager接线）、UP开播提醒收群过滤与配额口径一致、DynamicManager log_dir、qqbot stop收尾窗口、登录计数上限、Wbi外层TTL
- **遗留清零**：大屏N+1并行化；退群清理补全（动态订阅/轮询/at_all记录）；重新订阅不再补推（`ensure(baseline_first=)`）；空列表≠风控告警；dynamic_seen保留500条；指令异常回显收敛；DB死代码（primary_key_column/set_db_type）与12处冗余索引移除；旧库迁移幂等化（崩溃重跑安全）；migrations按库过滤；CI矩阵3.11/3.12/3.14+vendor预下载+wheel冒烟

## 六、运行 / 测试 / 发布流程

```bash
# 开发（仓库根）
pip install -e ".[dev]"        # 依赖含 jieba
pytest tests/                  # 当前 318 个用例，全绿，无需联网
pyflakes src tests             # 零告警
mypy src                       # 零错误
blivepost -c config.yaml       # 运行；chromium 装运行目录 ms-playwright/

# 发布给真机（WheelTest）
python -m build --wheel --outdir dist
WheelTest/.venv/Scripts/python -m pip install --force-reinstall --no-deps dist/blivepost-1.1.0-py3-none-any.whl
# ⚠️ --no-deps 跳过依赖：新环境需手动 pip install jieba（及 wheel 新增的其他依赖）

# Docker
docker compose up --build      # config.yaml/data/logs 挂载，端口8080（BLIVEPOST_WEB_HOST=0.0.0.0 已生效）
```

- 真机环境在 `WheelTest/`（venv + config.yaml + data/）；改动后「重建 wheel → 装 WheelTest → 重启进程 → 真机验证」是固定交付节奏。
- 浏览器验证惯例：仓库根曾用临时 `_panel_demo.py`（FakeXXX 桩 + 真实 DatabaseService，gitignore 的 `_*.py`）起 127.0.0.1:18080 起演示面板实测交互；用完删除。

## 七、配置要点（config.yaml Setting 段新增/关注项）

| 键 | 说明 |
|---|---|
| `web_panel` / `web_host` / `web_port` | 面板开关与监听（默认仅本机） |
| `web_token` | 管理员快捷令牌（`?token=` 即管理员登录） |
| `web_admin_user` / `web_admin_password` | 后台管理员；**口令非空才启用登录与后台**（WheelTest 的 config 目前口令为空，需自行填写后重启） |
| `debug` | Y 时 DEBUG 日志输出（排障明细），并放开 jieba 内部日志 |
| 8个推送开关（anchor/guard_achievement/...） | 仅作**首次播种的全局默认值**；运行时以后台/DB 为准 |

## 八、已知问题与注意事项

1. **全局推送开关语义**：群独立设置行随绑定自动播种后，全局开关主要作为「新绑定群的初始值/缺省回退」——修改全局不会影响已播种的群（后台页面已注明）；如需全群批量调整需逐群改或直接改库。
2. **会话为签名无状态**：jti黑名单支持登出即吊销（进程内存级，重启清空；改 web_token 可全量失效）；未配置 web_token 时重启仍掉登录态。
3. **HTMX 页面脚本时序**：涉及首屏布局的脚本务必延迟（rAF/load/settle）执行，Tailwind CDN 异步注入样式，教训见 65/66 批。
4. **Tailwind/HTMX 走 CDN**：离线或 CDN 不可达环境页面会裸奔（布局丢失但功能可用）；内网部署需自行本地化这两个脚本。
5. **数据导出**仅 SQLite 存储支持（zip 打包 data 目录）。
6. 真机主动消息配额、群 Markdown 权限等平台侧限制见 README「已知问题」。
7. 仓库根有两个未入库的用户产物：`scripts/panel_ids - 101989338.json`、词云导出 PNG——非项目文件，勿误提交。

## 九、未完成 / 建议方向（承接 PROJECT.md 建议节，第十批后更新）

**下一步开发方向与计划以 `ROADMAP.md` 为准**（v1.1.1 真机验证+运维小件 → v1.2.0 群内体验+数据价值+API公网开关+破坏性清理+13位时间戳迁移 → v1.3.0 待定项）。已决点（2026-11-10）：微博监控挂起；时间改13位毫秒时间戳（存储与时区解耦，展示按 `Setting.timezone` 转换，第十六批实施）；REST API 加公网开关（默认仅私网）。当前简记：

- 后台数据导出粒度细化（按房间/按时间）→ 第十二批
- MySQL/PG 下的数据导出方案 → 第十二批（明细 CSV/JSON 导出不依赖 zip）
- naive本地时间统一时区化 → **第十六批：13位毫秒时间戳方案（已定）**
- 发布即置顶的动态延迟推送（已知行为，见 dynamics.py 注释）
- qqbot 直接访问 botpy 私有属性 → 第十五批适配层
- `LiveMonitor` 旧库API移除 → 第十五批 v1.2.0 窗口

## 十、真机验证状态

- 已验证：登录体系（管理员/前台用户/游客越权拒绝）、后台开关即时生效、群独立设置翻转/恢复、词云各范围与导出、数据大屏渲染、动态页交互、开播推送降级链、弹幕连接看门狗、Redis 预检、终端 stop。
- **第十一批进展（2026-11-10）**：自动化项全过（面板鉴权/限速锁定err=3/签名会话/导出脱敏cookie_account=0/面板modify v3/子进程独立日志/退群清理他群保留/监控恢复）；真机反馈四问题已修复——@全体无权限(40034143)降级重发、群编辑器回显勾选+保存401提示、收入两位小数、/api/status下播记录datetime致500；会话跨重启保留已验证（web_token=123456派生密钥）。
- **@全体最终规则（用户拍板）**：发送前经 `GET /v2/groups/{id}/bot_state` 查机器人角色（**仅白名单机器人可用**，11253时None放行靠降级兜底）——群主/管理员才艾特，普通成员跳过且**不占当日配额**（peek+mark两段式）；艾特发送失败自动去标签降级重发并**退还当日配额**（at_all_refund钩子→clear_group_at_all）。角色缓存10分钟。
- **第十一批后续反馈（同日）**：/绑定动态真机通过；token登录admin/screen页未建会话已修（302+Set-Cookie，页头正确显示管理员）；已移出群从群列表/编辑器/回填过滤（重新入群自动恢复）；真机验证均通过。
- **第十一批反馈四（同日）**：开播按钮不渲染根因=官方bot路径push_media/push未透传keyboard（已修，下次开播应见「进入直播间」按钮）；@全体当日配额残留已清（旧失败先于退还机制产生，退还仅对新失败生效）；告警openids空时回退主管理员（原仅落日志）；开播推送增加调度日志。另发现：动态轮询持续风控失败（非JSON响应）已触发新告警链路，待专项排查。
- **动态轮询修复（2026-11-10下午，真机实证）**：根因三层——①WBI签名触发412（平台轮换mixin表，旧签名反被判风控；无签名+完整cookie正常）②仅传SESSDATA缺buvid③订阅UID填错（房间号3060820≠UP UID 36296621）。修复：完整cookie+签名失败回退无签名+失败退避(≥3轮改5-10分钟)+子进程父存活自检(强杀泄漏曾累积11进程)+绑定动态房间号归一化。真机订阅已修正为36296621。visited_label已移除（无自动恢复）。
- **动态轮询彻底修复（2026-11-10傍晚）**：requests与curl-cffi（模拟TLS指纹）均被请求级风控412，仅真实浏览器导航正常——最终方案：动态子进程内置持久无头Chromium（cookie注入，复用登录模块的ms-playwright内核），每轮页面导航到API URL取数；三级链：浏览器→requests签名→requests无签名。**真机验证：UP 36296621 基线12条动态成功入库，零失败**。**已按用户决策改为全局单浏览器（2026-11-10）**：主进程 DynFetchService（127.0.0.1 随机端口 + 进程级 token + X-Fetch-Token 校验 + URL 白名单 api.bilibili.com/x/polymer/），单浏览器惰性启动/损坏自动重建，cookie 随账号池同步；UP 轮询子进程经 _RemoteFetchProxy 调用，失败回退各自 requests 链。**不论订阅多少 UP，浏览器内存恒定 ~300MB**。真机验证：服务链路正常。参考数据（否决的备选）：每UP独立实例280-350MB/UP、用后即毁峰值不降且每轮4.1s。
