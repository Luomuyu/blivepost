# BLivePost 项目审查报告

## 审查轮次索引

| 轮次 | 日期 | 范围 | 结论 |
|------|------|------|------|
| 第一轮 | 2026-09-01 | 首轮全量（src 23模块、tests 12文件） | 通过（严重2、中等2、轻微约25处已修复） |
| 第二轮 | 2026-09-02 | 第十五批后全量复检 | 通过（修复2项严重：数据量提醒链路断裂、分级丢失） |
| 第三轮 | 2026-09-02 | 第十六批后全量 + wheel构建验证 | 通过（高1、中1、低2；py.typed缺失补齐） |
| 第四轮 | 2026-09-02 | 联系人管理批次专项 | 通过（154用例全过） |
| 第五轮 | 2026-09-02 | 过期内容专项清理 | 通过（README配置示例与代码模板同步） |
| **第六轮** | **2026-09-03** | **qq-botpy-v2替换 / union链路 / chromium目录 / 绑定风控 / 文档重建** | **通过（union回填补接线、风控接口替换、2个日期边界测试缺陷修复）** |

## 当前全量基线（第六轮审查后）

| 检查 | 结果 |
|------|------|
| pyflakes（src+tests） | 0告警 |
| flake8（src+tests） | 0告警 |
| mypy | 0错误（40文件） |
| pytest | **159个用例全过**（约48秒，无网络依赖） |
| wheel | `blivepost-1.1.0-py3-none-any.whl`，含py.typed |

### 功能链路核对（9条，第六轮全量复检）

| 链路 | 路径 | 状态 |
|------|------|------|
| 群指令 | qqbot事件 → CommandHandler → 绑定/监控/收入/SC榜/汇总等 → 被动回复 | ✅ |
| 私聊指令 | on_c2c_message_create → AdminCommandHandler（/登录 /账号 /导出 /体积 /我） | ✅ |
| 弹幕事件 | 子进程blivedm → handler（日志+统计+落库）→ 队列 → consumer → 按绑定路由推送 | ✅ |
| 开播/下播 | LIVE/PREPARING + 90秒状态校正合成事件 → 富媒体推送 + 统计落库 | ✅ |
| 动态监控 | wbi签名轮询（每UID子进程）→ 摘要/首图富媒体推送；开播联动暂停 | ✅ |
| 凭证失效轮换 | LOG_IN_NOTICE → 标记expired → 主管理员预警 → 亲和/LRU换号 | ✅ |
| Cookie维护 | 6小时循环：临近过期刷新 → 失效nav复核 → 分级提醒 | ✅ |
| Web面板 | aiohttp只读面板（token鉴权、SVG趋势图、账号健康、库体积） | ✅ |
| **union身份链路（第六轮新增核对）** | 消息author原生解析union_openid → extract_sender（union优先）→ 群/私聊权限判定；friend_add事件author取union入库；私聊消息自动回填好友表 | ✅ 本轮补接线 |

### 保留的"看似无用"代码（有意保留，非遗留）

| 项 | 理由 |
|----|------|
| `core/monitor.py`（LiveMonitor） | 顶层导出的库API；主流程已不使用。建议下个大版本评估移除 |
| `models/live.py` 的 UserInfo/DanmakuInfo/GiftInfo/SuperChatInfo/GuardInfo/DynamicInfo | 导出的库级数据模型，供库用户 |
| `services/wbi.py`（WbiSigner/wbi_sign/derive_wbi_key） | 第六轮确认非死代码：`dynamics.py`/`fingerprint.py` 在用，`test_wbi.py` 覆盖。bilibili.py 摘除签名调用不影响其存续 |
| `push.send_group_image` | 预留API，与单聊files接口对称 |
| `QueueDbBridge.__getattr__` 动态分发 | 子进程侧无需枚举主进程save_*方法清单 |

---

# 第六轮审查（2026-09-03）详细记录

> 背景：QQ机器人SDK由官方 `qq-botpy 1.2.1` 替换为自建 `qq-botpy-v2 1.0.0`（基于官方API v2文档适配、接口兼容，已发布PyPI）前后的全量审查；随后真机反馈「绑定」命令风控失败，一并处理并重建三文档。
> 结论：**通过**。发现并修复：union回填链路断裂（存量设计未接线）、friend_add读错层级（存量）、绑定命令风控失败（真机）、2个日期边界测试缺陷（存量）。

## 一、qq-botpy-v2替换验证

- 官方 `qq-botpy 1.2.1` 卸载，自建 `qq-botpy-v2 1.0.0` 安装（替换期本地wheel钉版 `==1.0.0`，PyPI发布后由维护者放宽为 `>=1.0.0`；**PyPI wheel与本地构建的botpy包内容逐字节一致**——48个.py文件零差异）。
- **导入包名仍为 `botpy`**，全部 `import` 零改动；发行名引用更新于 pyproject（`qq-botpy-v2==1.0.0`）与 Dockerfile。
- 接口面核对：`botpy.http.Route`/`Client`/`Intents`/`message.C2CMessage/GroupMessage`/`manage.C2CManageEvent` 全部在位；pip依赖（aiohttp/PyYAML/APScheduler）复用现有包，`pip check` 无冲突。
- union解析面核对：`C2CMessage._User`/`GroupMessage._User` 原生解析 `union_openid`（另含 `union_user_account`/`member_role`/`username`）；`C2CManageEvent._FriendAuthor` 原生解析好友事件author的 `union_openid`（官方1.2.1的C2CManageEvent仅有openid/timestamp，无author对象）。

## 二、发现并修复的问题

| 级别 | 问题 | 位置 | 修复 |
|------|------|------|------|
| 高（存量） | **union「消息自动回填」从未接线**：`update_friend_union` 自第二十三批引入起全仓库无调用方（`git log -S` 核实），文档宣称的设计未实现——好友表union永远为空 | services/qqbot.py、contacts.py | `on_c2c_message_create` 补接线（author.user_openid为键、union_openid回填）；新增 `ContactEventHandler.backfill_friend_union` |
| 高（存量） | **回填实现有幻影创建风险**：`ContactStore.update_friend_union` 走 `upsert_friend`，对未知openid会凭空创建 receive=1 的缓存记录，可能导致向非好友推送 | services/contacts.py | 改为仅更新已有记录；与缓存一致时跳过DB写入 |
| 高（存量） | **friend_add读错层级**：读 `event.union_openid` 顶层属性，官方1.2.1与qq-botpy-v2的C2CManageEvent顶层均无此字段——两条SDK下都永远得到空串 | services/qqbot.py | 改读 `event.author.union_openid`（空author安全回退） |
| 高（真机） | **绑定命令风控失败**：`/绑定` 走 `x/space/wbi/acc/info`（Wbi签名+登录态），真机返回-352「风控校验失败」 | services/bilibili.py | `get_room_info` 改用免签名接口：`/room/v1/Room/get_info`（uid/长号/标题/封面user_cover/观看人数，短号兼容）+ `/x/web-interface/card` 用户名片（昵称）；**实测原失败房间9646546成功返回完整信息** |
| 中（存量） | monkeypatch union_openid解析在qq-botpy-v2下冗余（消息_User类已原生解析，补丁只剩重复赋值） | services/qqbot.py | 移除补丁30行 |
| 低（存量） | **2个导出测试写死日期 `20260902`**：实现按当天日期生成导出文件名，测试零点后必然失败（2026-09-03 00:00起触发；日期边界测试缺陷，非产品bug） | tests/test_dynamics_and_reports.py、tests/test_export_and_pool.py | 期望文件名改为按当天日期生成；修复后159用例首次全绿 |

## 三、chromium安装位置变更

- `PLAYWRIGHT_BROWSERS_PATH` 默认值设为 `<运行目录>/ms-playwright`（`os.environ.setdefault`，显式配置优先——Docker经此接入）。
- Dockerfile ENV 改为 `/app/ms-playwright`（容器内运行目录，构建期安装随镜像烘焙）；`.dockerignore`/`.gitignore` 补 `ms-playwright`——**防止本地已下载的Windows版浏览器经 `COPY . .` 混入Linux镜像**。
- `playwright install --dry-run chromium` 实测安装位置正确（chromium与ffmpeg均指向运行目录）。
- 顺带发现：venv中playwright包此前未实际安装（声明了依赖但缺失），已补装并随 `-e .` 验证。

## 四、真机验证状态

- **已验证**（第二十/二十二/二十五批 + 本轮 + 第三十批）：access_token获取、websocket鉴权与心跳、终端stop、/登录权限判定（main_openids单配）、扫码流程启动、日志输出、**stop优雅关闭全序列**（房间停止→进程停止→数据库关闭→退出；退出噪音于第三十批修复）。
- **待复验**：`/绑定` 端到端（风控修复后）、分片上传富媒体、`/v2/panels` 指令面板、stop退出无噪音（第三十批修复后）。

## 五、第六轮量化摘要

| 指标 | 值 |
|------|-----|
| 测试 | 159用例全过（本轮修复2个日期缺陷后首次全绿；154→159为此前批次增量） |
| 静态检查 | pyflakes/flake8/mypy 三项零告警（mypy 40文件） |
| 本次移除 | botpy monkeypatch 30行；bilibili.py 的 Wbi 签名调用链约40行 |
| 本次新增 | 私聊消息union回填写线、chromium运行目录机制 |
| 真机 | 绑定房间9646546经新接口链路实测成功 |

---

# 历史轮次记录（第一~第五轮，细节见git历史）

## 第一轮（2026-09-01，首轮全量）

- **严重**：S1 `fingerprint.py` 缺 `import random`/`Tuple`（WBI重构误删）——启动即崩溃；S2 `daily_report`/`live_watcher`/`consumer`/`web_panel` 条件分支赋值但finally无条件引用——正常退出路径NameError。均修复。
- **中等**：无Redis环境FingerprintService构造阻塞21秒 → 连接超时收紧+进程级降级记忆；`get_top_super_chat` 注解引用未导入类型 → 补齐。
- **轻微**：死代码4项删除；无用导入约20处清理；测试桩缺陷修正。
- 8条功能链路核对通过；`LiveMonitor`/models数据类等"看似无用"代码确立保留结论。

## 第二轮（2026-09-02，第十五批后复检）

- **严重**：`CookieMaintainer._check_db_size` 从 `db_service.db_path` 取值（实际在 `db_manager` 上）——数据量提醒从未生效；`_notify_admins` 分级丢失——预警误发全部管理员。均修复并补端到端测试。
- 死代码审计：`send_group_image` 确立预留API结论；`_db_service_ref` 残留清除。
- 数据量监控、msg_seq递增、动态轮询、迁移器复检通过。

## 第三轮（2026-09-02，工程化批次后）

- **高**：`_check_db_size` 无参重复调用绕过 `db_size_warn_mb=0` 开关——删除残留。
- **中**：`notify_admins` 对字符串入参逐字符发送——加防御包装。
- **低**：动态昵称回填去重；**py.typed缺失补齐**（pyproject声明了package-data但文件不存在，PEP 561无效）。
- wheel构建验证：43文件、依赖核对、关键模块抽查通过。

## 第四轮（2026-09-02，联系人管理批次）

- friend/bot_group两表、8个平台事件接线、退群自动解绑清理、发送前接收过滤、机器人详情面板——审查通过；lint归零；wheel 44文件。

## 第五轮（2026-09-02，过期内容专项）

- README配置示例严重过时 → **从代码 `_create_default_config` 同步**，确立"配置示例以代码模板为唯一真源"原则（第六轮复核仍一致）。
- push.py过时注释更新；PROJECT.md过期建议清理；保留项复核维持。
- 过期内容清理原则固化：配置示例从模板生成；接口描述标注"已核对/待真机验证"；批次细节留git历史与REVIEW.md。

## 遗留风险（代码外）

- 真机联调部分完成：见第六轮"真机验证状态"，绑定/分片上传/指令面板待复验。
- vendor第三方镜像时效性依赖（blivedm运行时下载）。
- chromium首次安装约180MB（现装于运行目录；Docker镜像已预装）。
- Web面板未配置web_token时不鉴权（默认仅本机，远程访问应配置token或反代）。
