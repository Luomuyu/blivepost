# BLivePost

[![Python Version](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-GPL--3.0-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-1.2.0-orange.svg)](https://github.com/Luomuyu/blivepost)

B站直播监控与推送系统 - 一个功能完整、架构清晰的现代化Python应用

## ✨ 功能特性

### 核心功能
- 🔴 **直播监控** - 实时监控B站直播间，支持弹幕、礼物、上舰、SC、红包等消息
- 📢 **消息推送** - 通过QQ官方机器人（自建[qq-botpy-v2](https://github.com/tencent-connect/botpy)适配版，接口兼容官方botpy）主动推送消息到QQ群，开播推送支持图片富媒体（失败自动回退文本）
- 📊 **数据统计** - 弹幕/礼物/舰长/SC/红包入库，支持群指令查询、跨房间汇总、每日/每周/每月自动结算与CSV/JSON导出；**弹幕词云**（jieba分词，按天/近3/7/14天/按月/按年范围+全部或单独直播间）
- 🖥️ **Web状态面板** - 多页面控制台：概览/直播监控/动态监控/账号池/下播记录/收入趋势/观众分析（活跃时段热力图/高频发言榜/弹幕日分布）/词云/群列表；**游客默认只读**（AppID与账号池明细等运营情报需登录），登录前台用户后可操作（添加直播间/推送群/动态订阅/扫码登录等），管理员（config账号密码）可进后台管理用户、全局推送开关（状态+是否允许前台修改，即时生效）与数据导出；`web_token`为管理员快捷令牌（`/login?token=`即管理员登录，API/动作用`X-Web-Token`头——URL查询串令牌已收窄到登录页防凭证经URL传播）

### 扩展功能
- 📱 **动态监控** - 群指令订阅B站UP主动态（`/绑定动态 UP主UID`），独立子进程轮询，新动态推送到订阅群
- 🔐 **WBI签名** - 支持B站WBI签名，保障API调用安全
- 👥 **Cookie账号池** - 多账号cookie池轮转供监控使用，支持扫码登录、临近过期自动刷新、失效自动轮换
- 🤝 **联系人管理** - 自动跟踪好友与群（加/删、入群/退群、消息接收开关8个事件），退群自动解绑监控，发送前过滤未开启接收的目标
- 💾 **Redis缓存** - 可选Redis缓存支持，提升性能
- 🔧 **Vendor管理** - 自动化blivedm库依赖管理
- 🩺 **运维接口** - `/healthz`健康检查（Docker HEALTHCHECK）；定时脱敏备份（`backup_keep`滚动保留）；REST API v1（`/api/v1/rooms|summary|trend|analytics|wordcloud`，默认仅私网，`web_api_public`+`web_api_token`开放公网）

## 📦 安装

### 从源码安装

```bash
# 克隆仓库
git clone https://github.com/Luomuyu/blivepost.git
cd blivepost

# 安装（自动安装全部依赖，含PyPI的qq-botpy-v2；blivedm由vendor机制运行时下载）
pip install -e .
```

### 依赖要求

- Python 3.11+
- 扫码登录需要无头浏览器内核（获取完整cookie必需）：`pip install`时自动安装playwright依赖，**chromium内核在程序启动时自动下载到运行目录`ms-playwright/`**（已装时秒级跳过），也可手动执行`playwright install chromium`
- blivedm (自动安装)
- requests
- PyYAML
- redis (可选)

### Docker 部署

```bash
git clone https://github.com/Luomuyu/blivepost.git
cd blivepost
docker compose up -d --build   # 构建并启动（非root运行，宿主机端口127.0.0.1:18080）
```

> 国内网络直连 pypi.org 不可达时：
> `docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple`

## 🚀 快速开始

### 1. 创建配置文件

首次运行会自动生成默认配置文件 `config.yaml`：

```yaml
# BLivePost 配置文件
# 版本: 1.2.0

Admin: # 管理员设置（权限组逐级包含：main ⊇ openids ⊇ default）
  openids: [] # 管理员名单 (私聊管理命令+群控制指令+普通通知；包含default用户权限)
  main_openids: [] # 主管理员名单 (包含openids全部权限；额外接收Cookie失效等敏感通知；/登录命令仅此组可用)

QQBot: # QQ官方机器人设置 (基于botpy)
  app_id: "" # 机器人AppID
  app_secret: "" # 机器人AppSecret
  # 推送目标群通过群指令（@机器人 /监控 房间号）或Web面板添加，无需在此配置

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
  backup_hour: 4 # 定时备份时刻(时)
  backup_minute: 30 # 定时备份时刻(分)
  backup_keep: 7 # 备份保留份数(0=关闭定时备份)
  web_api_public: "N" # REST API v1公网开关 (N=仅私网, Y=对外开放强制X-API-Token)
  web_api_token: "" # REST API独立token (公网模式必填)
  timezone: "Asia/Shanghai" # 展示层时区 (IANA名称)
  weekly_report: "N" # 每周结算推送 (每周一00:05推送上周汇总)
  monthly_report: "N" # 每月结算推送 (每月1日00:05推送上月汇总)
  web_panel: "N" # Web状态面板 (游客只读/登录可操作, 默认仅本机访问)
  web_host: "127.0.0.1" # Web面板监听地址
  web_port: 8080 # Web面板端口
  web_token: "" # Web面板管理员快捷令牌 (设置后 /login?token= 即管理员登录; API/动作可用X-Web-Token头; 亦作Web会话签名密钥来源, 配置后登录态跨进程重启保留, 修改即全量登出)
  web_admin_user: "admin" # Web后台管理员账号 (管理员来自config不入库)
  web_admin_password: "" # Web后台管理员口令 (非空才启用登录与后台; 前台用户由后台添加)
  data_retention_days: 0 # 数据保留天数 (0=永久保留所有数据, 永不自动删除)
  db_size_warn_mb: 500 # 数据库体积预警阈值MB (超过时私聊提醒管理员, 0=不提醒)
  ignore_cmds: [] # 忽略的未知命令列表 (不打印日志)
  debug: "N" # debug模式 (输出QQ消息发送者身份明细: openid/union_openid等)

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
from blivepost.database import DatabaseService, DatabaseManager
```

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
│   │   └── handler.py       # 直播事件处理（日志/统计/落库/推送开关）
│   ├── services/
│   │   ├── qqbot.py         # QQ机器人客户端（qq-botpy-v2封装）
│   │   ├── commands.py      # 群指令
│   │   ├── admin_commands.py# 管理员私聊指令
│   │   ├── bindings.py      # 群绑定+动态订阅存储
│   │   ├── contacts.py      # 联系人管理（好友/群状态、union_openid）
│   │   ├── cookie_pool.py   # Cookie账号池
│   │   ├── bili_login.py    # 扫码登录（Playwright无头）
│   │   ├── dynamics.py      # UP主动态轮询管理
│   │   ├── push.py          # 推送服务（模板/路由/富媒体）
│   │   ├── report.py        # 日/周/月结算推送
│   │   ├── bilibili.py      # B站API（免签名直播间/用户接口）
│   │   ├── fingerprint.py   # 指纹服务
│   │   ├── wbi.py           # WBI签名
│   │   └── webpanel.py      # Web状态面板
│   ├── database/            # 模型/引擎/服务（23张表：主库11+房间子库11+动态子库1）+ Schema迁移器
│   ├── models/live.py       # 数据模型
│   ├── utils/               # 日志、sender提取（union优先）、TCP预检
│   └── vendor/              # blivedm下载与补丁
├── tests/                   # 单元测试（370+个）
├── pyproject.toml
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
│ 存储层   database/（SQLAlchemy异步，23张表）        │
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
| weekly_report | 每周结算推送（每周一00:05推送上周汇总） | N |
| monthly_report | 每月结算推送（每月1日00:05推送上月汇总） | N |

开播/下播推送始终开启（含封面富媒体），不设开关。注意`daily_report`会消耗主动消息配额（未放量约4条/月/群），按需开启。

开播提醒**自动艾特全体**：每个群每个自然日仅1次 @全体成员 配额，当天首次开播提醒（房间开播推送或UP主动态开播提醒）自动嵌入 `<qqbot-at-everyone />` 艾特全体。艾特模式按群三选一（`/艾特全体`指令配置）：`每日`（默认，当日首次开播艾特）/`仅 房间号...`（指定房间的首次开播才艾特）/`关闭`。仅群主/管理员身份的机器人实际发送；发送失败自动去标签降级重发且**不消耗**当日配额（失败即退还）。配置跨进程重启保留（主库 `group_at_all` 表）。

消息形态说明：下播消息不含直播地址行；开播消息在Markdown形态下仅保留「进入直播间」按钮作为跳转入口（按钮跳转 `https://live.bilibili.com/<房间号>`），富媒体与纯文本形态保留直播地址行。

### 群命令（@机器人发送）

在群里@机器人发送以下指令，实现多群绑定与多房间监控。

**权限分级**：控制指令（/监控 /取消监控 /绑定动态 /取消动态 /推送设置 /艾特全体）仅 `Admin.openids` ∪ `Admin.main_openids` 名单内的用户可执行（名单为空时不限制）；查询指令（/状态 /绑定列表 /收入 /SC榜 /汇总 /帮助）不受限。直播间的绑定/解绑已移至Web面板操作。


| 指令 | 说明 |
|------|------|
| `/监控 房间号` | 开始监控并推送该直播间到本群（短号自动归一化为真实房间号） |
| `/取消监控 房间号` | 取消向本群推送该直播间 |
| `/收入 房间号 [日期/周/月]` | 查询该直播间收入汇总（日期格式YYYYMMDD或YYYY-MM-DD；`周`=近7天、`月`=近30天） |
| `/SC榜 房间号 [日期]` | 查询该直播间SC金额排行（前5，默认今日，日期格式YYYYMMDD或YYYY-MM-DD） |
| `/状态` | 查看本群监控中的直播间及监控任务状态 |
| `/绑定列表` | 查看本群全部绑定关系 |
| `/汇总 [周/月]` | 全部监控房间合计（今日/近7天/近30天） |
| `/推送设置 [房间号 [开关名 开/关\|恢复]]` | 查看本群推送开关：无参数=各绑定房间概览，带房间号=该房间7项开关明细（天选/大航海/系统事件/房间信息/日周月报，标注是否跟随全局）；指定开关名与开/关可修改本群独立设置，`恢复`=清除独立设置回落跟随全局 |
| `/绑定动态 UP主UID` | 订阅UP主动态（需开启`dynamic_monitor`开关；输入房间号自动归一化为该主播UID），新动态含内容摘要与图片推送（浏览器截图>本地卡片>原图降级） |
| `/取消动态 UP主UID` | 取消订阅UP主动态（订阅后UP主开播自动提醒到订阅群，无需额外指令） |
| `/艾特全体 [模式]` | 查看本群艾特模式；`每日`/`关闭`/`仅 房间号...`三模式（发送失败不耗配额） |
| `/帮助` | 查看全部指令 |
| `/登录`（私聊） | **仅管理员**：扫码登录B站账号，二维码图片私聊回复，成功后Cookie写入账号池 |
| `/账号`（私聊） | **仅管理员**：查看账号池状态（账号/状态/过期时间） |
| `/移除账号 UID`（私聊） | **仅管理员**：从账号池删除账号 |
| `/恢复账号 UID`（私聊） | **仅管理员**：将失效账号恢复为可用（先验证登录态） |
| `/导出 房间号 [日期] [json]`（私聊） | **仅管理员**：导出当日明细到exports目录（默认CSV带BOM可直接Excel打开，加json为JSON格式；无数据不生成文件） |
| `/体积`（私聊） | **仅管理员**：查询主库+全部子库体积合计 |
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

### v1.2.0 (2026-11)

- ✨ 群内体验：`@全体`模式三选一（每日/仅指定房间/关闭，`/艾特全体`指令），发送前按机器人群内角色门控（群主/管理员才艾特），发送失败不消耗当日配额并退还
- ✨ 未知指令模糊提示（difflib）；`/帮助`按权限分组
- ✨ 观众分析页（7天×24小时活跃热力图/高频发言榜/弹幕量日分布）
- ✨ REST API v1（`/api/v1/rooms|summary|trend|analytics|wordcloud`，默认仅私网；`web_api_public=Y`+`web_api_token`开放公网并强制令牌+限速）
- ✨ 运维小件：`/healthz`健康检查（Docker HEALTHCHECK）、定时脱敏备份（滚动保留）、按房间×日期范围CSV/JSON导出（MySQL/PG可用）
- 💥 破坏性清理：移除`LiveMonitor`旧库API（扩展走`PushService`/`DatabaseService`）；botpy私有属性收敛为`botpy_compat`适配层；会话支持jti吊销（登出即失效）
- 💥 全库13位毫秒时间戳：31处DateTime列改BigInteger(ms)消除时区歧义；查询层统一半开区间`[start,end)`；展示层统一`Setting.timezone`转换
- 🔧 动态取数升级全局单浏览器（feed+opus渲染+截图+PIL卡片降级链）；动态富媒体经素材管理嵌入Markdown
- 🔧 配置链修复：`backup_*`/`web_api_*`/`timezone`此前未从YAML读入（静默失效），现已解析并支持环境变量覆盖
- 🔧 子进程稳定性：房间子进程停等改纯异步轮询（修复线程池耗尽导致的静默失效与僵尸进程）；房间子库启用WAL
- 🔧 Web面板游客脱敏：账号池页与AppID需登录可见（与`/api/status`口径一致）
- 💥 迁移器重置（2026-11-11决策）：未公开发布无生产库，历史迁移（v2~v8）全部清退、版本表归一v1，数据库结构由create_all直接建出——**仅支持全新库或已毫秒化的库**
- 🔧 审查遗留逐项修复：动态取数浏览器取数/渲染双页分离（消除prepare阻塞fetch的串行瓶颈）；高频事件表timestamp单列索引（create_all自动建）+分析/趋势SQL侧整数分桶聚合（大房间不再全表扫描/全量拉内存）；发送队列高水位本地图降级文本（防~800MB内存尖峰）；PBKDF2升至600k+口令最短8位；`?token=`收窄到`/login`页（API/动作用`X-Web-Token`头；曾把`/screen?token=`配进OBS源的用户需先在浏览器完成一次`/login?token=`换取7天cookie）；Docker镜像非root运行；upsert写锁串行化；子库数据根锚定配置目录（不随CWD漂移）；房间子进程队列入队/会话关闭/kill兜底等健壮性加固

### v1.1.0 (2026-09)

- ✨ Cookie账号池：扫码登录、多账号轮转、房间亲和、临近过期自动刷新、失效轮换+每日重扫
- ✨ 动态监控：UP主订阅、富媒体推送、开播联动；Web状态面板（token鉴权、趋势图）
- ✨ 联系人管理：好友/群状态跟踪、退群自动解绑、发送前接收过滤、union_openid链路
- ✨ 数据治理：Schema迁移器、数据永久保留、库体积监控、CSV/JSON导出
- 🔧 QQ机器人SDK切换为自建qq-botpy-v2（对齐官方API v2：分片上传、指令面板、msg_seq）
- 🔧 绑定命令改用免签名B站接口，规避acc/info风控（-352）
- 🔧 chromium内核安装到运行目录ms-playwright/（便携部署，Docker烘焙进镜像）

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

本项目基于 [GPL-3.0](LICENSE) 发布。


### 第三方声明

本项目运行时依赖并包含改编自第三方项目的代码（如 blivedm 的兼容与
Monkey Patching 部分），各上游项目的版权与许可见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 与 `licenses/` 目录；
原项目代码仍受其原始许可约束。

## 🙏 致谢

- [blivedm](https://github.com/xfgryujk/blivedm) - B站直播弹幕库
- [botpy](https://github.com/tencent-connect/botpy) - QQ机器人官方Python SDK（本项目使用的qq-botpy-v2基于其接口风格适配最新API v2文档）
