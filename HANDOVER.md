# BLivePost 交接文档

> 生成日期：2026-11-09 ｜ 对应版本：v1.1.0（master @ 9dd4a9b）
> 用途：供后续开发者/维护者快速接手。项目细节以 `PROJECT.md`（六十六批完整记录）、`README.md`（用户视角）、`REVIEW.md`（审查记录）为准，本文档是入口与摘要。

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
├── __main__.py            # 启动装配：日志→配置→数据库→播种→各服务→恢复现场
├── config/settings.py     # Settings/MessageSettings（web_admin_user/password 在此）+样例config注释
├── config/env.py          # 环境变量覆盖映射（BLIVEPOST_*）
├── core/
│   ├── handler.py         # 直播事件处理器（子进程内运行；推送不再做开关门控，恒转发）
│   └── manager.py         # MonitorManager/房间子进程/QueuePushService/PushQueueConsumer
│                          #   消费者：按队列item携带的switch标签+群独立设置过滤收群（主进程侧）
├── database/
│   ├── models.py          # Base(主库) + EventBase(子库) 两套元数据；全部表定义
│   ├── manager.py         # DatabaseManager（引擎/建表/迁移入口）
│   ├── migrations.py      # DDL迁移(v2~v6) + MIGRATION_HANDLERS函数型迁移(v5)
│   ├── service.py         # DatabaseService：全部读写门面（含路由到房间子库）
│   └── room_store.py      # RoomDbManager/Registry（每直播间子库，create_all自动补新表）
├── services/
│   ├── webpanel.py        # Web面板（页面/动作/登录会话/管理后台/大屏/词云/导出）
│   ├── bindings.py        # BindingStore 群绑定+动态订阅（bind/unbind时播种/清理群推送设置）
│   ├── push.py            # PushService：Markdown→富媒体→文本降级链；_switch/_groups标记上下文
│   ├── report.py          # DailyReportJob 日/周/月结算（发送前查运行时开关+按群过滤）
│   ├── commands.py        # QQ群指令（/监控 /取消监控 /查询 等；绑定/解绑指令已移除）
│   ├── admin_commands.py  # 私聊管理员指令
│   ├── qqbot.py           # QQBotClient（websocket、主动消息、debug身份明细日志）
│   ├── cookie_pool.py     # B站账号池（轮转/亲和/失效上报）
│   ├── bili_login.py      # B站扫码登录（playwright chromium 装运行目录 ms-playwright/）
│   ├── bilibili.py        # B站API（免签名 get_info+card，规避-352风控）
│   ├── dynamics.py        # 动态轮询（每UP独立子进程+每UP已见动态子库）
│   ├── contacts.py        # 联系人（friend/bot_group；union_openid链路）
│   ├── wordcloud.py       # jieba分词统计（停用词过滤；jieba缺失退回二元组）
│   └── templates/         # Jinja2模板（base布局+8页面+partials；screen.html独立大屏布局）
└── utils/logger.py        # 日志（apply_debug：DEBUG日志由Setting.debug控制；静默jieba噪音）
```

## 三、数据存储结构

- **主库**（`data/blivepost.db`，类型可配 MySQL/PG）：绑定关系 `group_binding`、`bot_group`（群简介/分类/标签/成员数）、`friend`、`cookie_account`、`dynamic_binding`、`room_db`（房间→子库映射）、`web_user`（前台用户，pbkdf2口令哈希）、`push_switch`（全局推送开关，enabled+allow_modify）。
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

- 会话：内存 dict + cookie `blivepost_sid`（7天，进程重启失效）。
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

## 六、运行 / 测试 / 发布流程

```bash
# 开发（仓库根）
pip install -e ".[dev]"        # 依赖含 jieba
pytest tests/                  # 当前 200 个用例，全绿，无需联网
pyflakes src tests             # 零告警
blivepost -c config.yaml       # 运行；chromium 装运行目录 ms-playwright/

# 发布给真机（WheelTest）
python -m build --wheel --outdir dist
WheelTest/.venv/Scripts/python -m pip install --force-reinstall --no-deps dist/blivepost-1.1.0-py3-none-any.whl
# ⚠️ --no-deps 跳过依赖：新环境需手动 pip install jieba（及 wheel 新增的其他依赖）

# Docker
docker compose up --build      # config.yaml/data/logs 挂载，端口8080
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
2. **会话在内存**：进程重启后所有登录态失效（含 web_token 重新登录，无感）。
3. **HTMX 页面脚本时序**：涉及首屏布局的脚本务必延迟（rAF/load/settle）执行，Tailwind CDN 异步注入样式，教训见 65/66 批。
4. **Tailwind/HTMX 走 CDN**：离线或 CDN 不可达环境页面会裸奔（布局丢失但功能可用）；内网部署需自行本地化这两个脚本。
5. **数据导出**仅 SQLite 存储支持（zip 打包 data 目录）。
6. 真机主动消息配额、群 Markdown 权限等平台侧限制见 README「已知问题」。
7. 仓库根有两个未入库的用户产物：`scripts/panel_ids - 101989338.json`、词云导出 PNG——非项目文件，勿误提交。

## 九、未完成 / 建议方向（承接 PROJECT.md 建议节）

- 群推送设置的 QQ 群指令入口（当前仅 WebUI 可改本群设置）
- 后台数据导出粒度细化（按房间/按时间）
- 大屏图表主题跟随日间模式（当前恒深色）
- 会话持久化（重启不掉登录态）
- MySQL/PG 下的数据导出方案（当前仅 SQLite）

## 十、真机验证状态

- 已验证：登录体系（管理员/前台用户/游客越权拒绝）、后台开关即时生效、群独立设置翻转/恢复、词云各范围与导出、数据大屏渲染、动态页交互、开播推送降级链、弹幕连接看门狗、Redis 预检、终端 stop。
- 待复验：第六十六批（词云容器自适应）刚发布，等真机确认；新环境部署时勿忘 jieba 与 web_admin_password。
