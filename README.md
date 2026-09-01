# BLivePost

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.0.0-orange.svg)](https://github.com/luomuyu/blivepost)

B站直播监控与推送系统 - 一个功能完整、架构清晰的现代化Python应用

## ✨ 功能特性

### 核心功能
- 🔴 **直播监控** - 实时监控B站直播间，支持弹幕、礼物、上舰、SC、红包等消息
- 📢 **消息推送** - 通过QQ官方机器人（[botpy](https://github.com/tencent-connect/botpy)）主动推送消息到QQ群，开播推送支持图片富媒体（失败自动回退文本）
- 📊 **数据统计** - 弹幕/礼物/舰长/SC/红包入库，支持群指令查询、跨房间汇总、每日/每周/每月自动结算与CSV/JSON导出
- 🖥️ **Web状态面板** - 可选只读面板：账号池健康（剩余有效期/使用次数/关联房间）、监控房间当日汇总、收入趋势折线图（SVG）、动态订阅、下播记录；支持token鉴权与 `/api/history` JSON接口（默认关闭，仅本机访问）

### 扩展功能
- 📱 **动态监控** - 群指令订阅B站UP主动态（`/绑定动态 UP主UID`），独立子进程轮询，新动态推送到订阅群
- 🔐 **WBI签名** - 支持B站WBI签名，保障API调用安全
- 👥 **Cookie账号池** - 多账号cookie池轮转供监控使用，支持扫码登录、临近过期自动刷新、失效自动轮换
- 🤝 **联系人管理** - 自动跟踪好友与群（加/删、入群/退群、消息接收开关8个事件），退群自动解绑监控，发送前过滤未开启接收的目标
- 💾 **Redis缓存** - 可选Redis缓存支持，提升性能
- 🔧 **Vendor管理** - 自动化blivedm库依赖管理

## 📦 安装

### 从源码安装

```bash
# 克隆仓库
git clone https://github.com/luomuyu/blivepost.git
cd blivepost

# 安装依赖
pip install -e .

# 或者使用 pip 安装
pip install blivepost
```

### 依赖要求

- Python 3.11+
- 扫码登录需要无头浏览器内核（获取完整cookie必需）：`pip install`时自动安装playwright依赖，**chromium内核在程序启动时自动下载**（已装时秒级跳过），也可手动执行`playwright install chromium`
- blivedm (自动安装)
- requests
- PyYAML
- redis (可选)

## 🚀 快速开始

### 1. 创建配置文件

首次运行会自动生成默认配置文件 `config.yaml`：

```yaml
# BLivePost 配置文件
# 版本: 1.0.0

Admin: # 管理员设置
  openids: [] # 管理员用户openId列表 (私聊命令、普通通知接收者)
  main_openids: [] # 主管理员openId列表 (Cookie失效等敏感通知接收者, 留空则发全部管理员)

QQBot: # QQ官方机器人设置 (基于botpy)
  app_id: "" # 机器人AppID
  app_secret: "" # 机器人AppSecret
  # 推送目标群通过群指令建立绑定（@机器人发送 /绑定 房间号），无需在此配置

# 动态监控通过群指令订阅（@机器人 /绑定动态 UP主UID），无需配置

Live: # 直播间设置
  live_id: "" # 直播间房间号

Setting: # 消息推送设置
  # 弹幕/礼物/舰长/SC/红包 仅记录日志并存入数据库，不推送消息
  anchor: "N" # 天选时刻推送
  guard_achievement: "Y" # 大航海成就推送
  system_event: "Y" # 系统事件推送 (切断/警告/禁言/房管)
  room_change: "N" # 房间信息变更推送
  dynamic_monitor: "N" # 动态监控总开关 (关闭时/绑定动态 不可用)
  daily_report: "N" # 每日结算推送 (每天00:05, 仅推有数据的房间, 消耗主动消息配额)
  weekly_report: "N" # 每周结算推送 (每周一00:05推送上周汇总)
  monthly_report: "N" # 每月结算推送 (每月1日00:05推送上月汇总)
  web_panel: "N" # Web状态面板 (只读, 默认仅本机访问)
  web_host: "127.0.0.1" # Web面板监听地址
  web_port: 8080 # Web面板端口
  web_token: "" # Web面板访问令牌 (留空不鉴权; 设置后需 ?token= 或 X-Web-Token 头)
  data_retention_days: 0 # 数据保留天数 (0=永久保留所有数据, 永不自动删除)
  db_size_warn_mb: 500 # 数据库体积预警阈值MB (超过时私聊提醒管理员, 0=不提醒)
  ignore_cmds: [] # 忽略的未知命令列表 (不打印日志)

Redis: # Redis配置 (可选)
  host: "localhost"
  port: 6379
  password: ""
  db: 0

Database: # 数据库配置
  type: "sqlite" # 数据库类型: sqlite, mysql, postgresql
  # SQLite配置 (type为sqlite时使用)
  sqlite_path: "data/blivepost.db" # SQLite数据库文件路径
  # MySQL/PostgreSQL配置 (type为mysql或postgresql时使用)
  host: "localhost"
  port: 3306 # MySQL默认3306, PostgreSQL默认5432
  user: "root"
  password: ""
  database: "blivepost"

Auth: # 认证配置
  sessdata: "" # 默认的B站登录凭证 (可被Redis覆盖)

Template: # 推送文案模板 (可选, 留空使用内置模板; str.format占位符, 可用字段见README)
  # 开播模板占位符: {name} {title} {room_id} {online} {time} {cover} {url}
  live_start: ""
  # 下播模板额外占位符: {like_count} {duration} {danmaku_count} {gold_count} {ship_count} {sc_count}
  live_end: ""
```

### 2. 运行程序

```bash
# 使用默认配置运行
blivepost

# 指定配置文件
blivepost -c /path/to/config.yaml

# 指定SESSDATA
blivepost -s YOUR_SESSDATA

# 指定日志目录
blivepost --log-dir ./logs
```

### 3. 作为库使用

```python
from blivepost import Settings, run

# 方式1: 直接运行（推荐，完整主进程编排）
run()

# 方式2: 自定义配置运行
import asyncio
from blivepost.__main__ import main_async

asyncio.run(main_async(config_path="config.yaml", sessdata=""))

# 库级组件也可单独使用
from blivepost import CookiePool, PushService, WbiSigner, BindingStore
```

> 说明：`LiveMonitor` 等早期库API仍保留导出，但推荐的使用方式是CLI启动完整主进程（含子进程监控、账号池与群指令体系）。

## 📁 项目结构

```
blivepost/
├── src/blivepost/
│   ├── __init__.py          # 包入口与导出
│   ├── __main__.py          # CLI入口与主进程编排
│   ├── config/
│   │   └── settings.py      # 配置管理（Admin/QQBot/Setting/Template等段）
│   ├── core/
│   │   ├── manager.py       # 子进程管理/队列消费/Cookie维护循环
│   │   ├── monitor.py       # LiveMonitor（早期库API，主流程已不用）
│   │   └── handler.py       # 直播事件处理（日志/统计/落库/推送开关）
│   ├── services/
│   │   ├── qqbot.py         # QQ机器人客户端（botpy封装）
│   │   ├── commands.py      # 群指令
│   │   ├── admin_commands.py# 管理员私聊指令
│   │   ├── bindings.py      # 群绑定+动态订阅存储
│   │   ├── cookie_pool.py   # Cookie账号池
│   │   ├── bili_login.py    # 扫码登录（Playwright无头）
│   │   ├── dynamics.py      # UP主动态轮询管理
│   │   ├── push.py          # 推送服务（模板/路由/富媒体）
│   │   ├── report.py        # 日/周/月结算推送
│   │   ├── bilibili.py      # B站API
│   │   ├── fingerprint.py   # 指纹服务
│   │   ├── wbi.py           # WBI签名
│   │   └── webpanel.py      # Web状态面板
│   ├── database/            # 模型/引擎/服务（14张表）
│   ├── models/live.py       # 数据模型
│   ├── utils/logger.py      # 日志（按天轮转）
│   └── vendor/              # blivedm下载与补丁
├── tests/                   # 单元测试（125个）
├── pyproject.toml
└── PROJECT.md               # 详细项目信息与审查记录
```

## 🏗️ 架构设计

### 分层架构

```
┌──────────────────────────────────────────────────┐
│ CLI (__main__.py)：配置/数据库/机器人/消费者/       │
│ 结算/watcher/面板/恢复 —— 主进程不监控直播间        │
├──────────────────────────────────────────────────┤
│ 命令层   commands（群） / admin_commands（私聊）    │
│ 服务层   push / bindings / cookie_pool /           │
│         bili_login / dynamics / report / webpanel │
│ 接入层   qqbot(botpy) / bilibili / fingerprint    │
├──────────────────────────────────────────────────┤
│ 进程层   RoomProcess（房间监控）/ 动态轮询子进程     │
│         跨进程队列：推送/落库/失效上报/新动态        │
├──────────────────────────────────────────────────┤
│ 存储层   database/（SQLAlchemy异步，14张表）        │
└──────────────────────────────────────────────────┘
```

### 设计原则

- **单一职责**：每个模块专注于单一功能
- **依赖注入**：通过构造函数注入依赖，便于测试
- **配置驱动**：所有行为通过配置文件控制
- **类型安全**：使用类型注解和数据类

## 🔧 配置说明

### QQBot 推送配置

```yaml
QQBot:
  app_id: "你的AppID"          # QQ开放平台机器人AppID
  app_secret: "你的AppSecret"  # 机器人AppSecret
```

需要先在 [QQ开放平台](https://q.qq.com/) 创建机器人并将机器人加入目标群。
群消息通过官方接口主动推送，受官方主动消息频控限制。

### 推送文案模板（可选）

在配置文件中自定义各类推送文案，留空使用内置模板。占位符使用 `str.format` 语法：

```yaml
Template:
  live_start: "[{name}]开播啦！标题：{title} 房间：{room_id} {url}"
  live_end: "[{name}]下播了，时长{duration}，SC共{sc_count}笔{sc_total}元"
  anchor: "天选：{award} 弹幕:{danmu} 要求:{require}"
  guard_achievement: "大航海成就：{content}"
  cut_off: "直播被切断：{reason}"
  warning: "收到警告：{reason}"
```

可用占位符：`live_start`/`live_end`：`name` `title` `room_id` `online` `cover` `url` `time`（下播另含 `like_count` `duration` `danmaku_count` `gold_count` `ship_count` `sc_count`）；`anchor`：`award` `danmu` `require`；`guard_achievement`/`cut_off`/`warning`：`content`/`reason`。模板含未知占位符时自动回退内置文案。

### 消息推送范围

弹幕、礼物、舰长（上舰）、SC（醒目留言）、红包仅**记录日志并存入数据库**（供`/收入`、`/SC榜`查询），不推送消息。

| 设置 | 说明 | 默认 |
|------|------|------|
| anchor | 天选时刻推送 | N |
| guard_achievement | 大航海成就推送 | Y |
| system_event | 系统事件推送（切断/警告/禁言/房管） | Y |
| room_change | 房间信息变更推送 | N |
| daily_report | 每日结算推送（每天00:05，仅推送有数据的房间；模板可用`Template.daily_report`自定义） | N |

开播/下播推送始终开启（含封面富媒体），不设开关。注意`daily_report`会消耗主动消息配额（未放量约4条/月/群），按需开启。

### 群命令（@机器人发送）

在群里@机器人发送以下指令，实现多群绑定与多房间监控：

| 指令 | 说明 |
|------|------|
| `/绑定 房间号` | 绑定当前群与直播间（记录主播UID，短号自动归一化为真实房间号） |
| `/监控 房间号` | 开始监控并推送该直播间到本群 |
| `/取消监控 房间号` | 取消向本群推送该直播间 |
| `/解绑 房间号` | 解除当前群与直播间的绑定 |
| `/收入 房间号 [日期/周/月]` | 查询该直播间收入汇总（日期格式YYYYMMDD或YYYY-MM-DD；`周`=近7天、`月`=近30天） |
| `/SC榜 房间号` | 查询该直播间今日SC金额排行（前5） |
| `/状态` | 查看本群监控中的直播间及监控任务状态 |
| `/我的绑定` | 查看本群全部绑定关系 |
| `/汇总 [周/月]` | 全部监控房间合计（今日/近7天/近30天） |
| `/绑定动态 UP主UID` | 订阅UP主动态（需开启`dynamic_monitor`开关），新动态含内容摘要与图片富媒体推送 |
| `/开播提醒` | 订阅动态后UP主开播自动提醒到订阅群（房间号+标题，无需额外指令） |
| `/取消动态 UP主UID` | 取消订阅UP主动态 |
| `/帮助` | 查看全部指令 |
| `/登录`（私聊） | **仅管理员**：扫码登录B站账号，二维码图片私聊回复，成功后Cookie写入账号池 |
| `/账号`（私聊） | **仅管理员**：查看账号池状态（账号/状态/过期时间） |
| `/移除账号 UID`（私聊） | **仅管理员**：从账号池删除账号 |
| `/恢复账号 UID`（私聊） | **仅管理员**：将失效账号恢复为可用（先验证登录态） |
| `/导出 房间号 [日期] [json]`（私聊） | **仅管理员**：导出当日明细到exports目录（默认CSV带BOM可直接Excel打开，加json为JSON格式；无数据不生成文件） |
| `/我`（私聊） | 回复你的openid（所有人可用，方便收集配置管理员名单） |

说明：
- 每个直播间全局只存在一个监控任务：收到`/监控`命令时不存在则创建，已存在则忽略
- 推送只依赖绑定关系：向该直播间绑定列表中处于监控状态的群openid推送
- 监控子进程从Cookie账号池轮转取用登录账号（池空时游客连接）；凭证失效时自动标记并轮换池中其他账号，全部失效才暂停对应房间监控
- Cookie相关提醒（失效、账号池耗尽）**仅私聊发送给管理员**，不在群里发送
- 管理员私聊发送`/登录`触发扫码登录（基于Playwright无头浏览器，获取完整cookie），Cookie写入账号池并每6小时自动检查：临近过期自动刷新续期
- 所有群取消监控/解绑后，对应直播间的监控任务自动停止
- 程序重启后自动恢复重启前处于监控状态的房间
- 每个直播间由独立的子进程监控（主进程只负责机器人、命令与推送路由），监控进程死亡时自动即时重建
- 子进程内每90秒用REST接口核对直播状态，与弹幕连接不一致时自动合成开播/下播事件
- 子进程产生的推送与落库请求经跨进程队列发回主进程统一处理（避免SQLite多进程写冲突）

## 🔌 扩展开发

### 自定义推送机器人

`PushService` 通过 `bot_client` 鸭子类型解耦，实现 `push`/`push_media` 即可替换推送后端：

```python
from blivepost import PushService, Settings

class MyBot:
    def push(self, content, groups):
        ...  # 文本推送

    def push_media(self, image_url, content, groups):
        ...  # 富媒体推送

settings = Settings.load("config.yaml")
ps = PushService(settings)
ps.bot_client = MyBot()
```

### 自定义推送模板

见上方"推送文案模板"配置段；占位符渲染失败自动回退内置文案。

### 事件处理定制

直播事件处理逻辑集中在 `core/handler.py` 的 `BaseEventHandler`
（继承 blivedm.BaseHandler 后由子进程实例化），可继承覆写单个
`_on_*` 回调实现自定义行为（如接入自己的统计系统）。

### 自定义推送服务

```python
from blivepost import PushService

class MyPushService(PushService):
    def send_message(self, message: str) -> bool:
        # 自定义推送逻辑
        print(message)
        return True
```

## 📝 更新日志

### v1.0.0 (2026-03)

- 🎉 完全重构项目架构
- ✨ 新增指纹服务和WBI签名支持
- ✨ 新增动态监控模块
- ✨ 新增Redis缓存支持
- ✨ 新增Vendor依赖管理
- 🔧 采用现代化包管理（pyproject.toml）
- 📚 完善类型注解和文档

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

[MIT License](LICENSE)

## 🙏 致谢

- [blivedm](https://github.com/xfgryujk/blivedm) - B站直播弹幕库
- [botpy](https://github.com/tencent-connect/botpy) - QQ机器人官方Python SDK
