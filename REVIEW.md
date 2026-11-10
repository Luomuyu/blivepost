# BLivePost 项目审查报告

## 审查轮次索引

| 轮次 | 日期 | 范围 | 结论 |
|------|------|------|------|
| 第一轮 | 2026-09-01 | 首轮全量（src 23模块、tests 12文件） | 通过（严重2、中等2、轻微约25处已修复） |
| 第二轮 | 2026-09-02 | 第十五批后全量复检 | 通过（修复2项严重：数据量提醒链路断裂、分级丢失） |
| 第三轮 | 2026-09-02 | 第十六批后全量 + wheel构建验证 | 通过（高1、中1、低2；py.typed缺失补齐） |
| 第四轮 | 2026-09-02 | 联系人管理批次专项 | 通过（154用例全过） |
| 第五轮 | 2026-09-02 | 过期内容专项清理 | 通过（README配置示例与代码模板同步） |
| 第六轮 | 2026-09-03 | qq-botpy-v2替换 / union链路 / chromium目录 / 绑定风控 / 文档重建 | 通过（union回填补接线、风控接口替换、2个日期边界测试缺陷修复） |
| **第七轮** | **2026-11-10** | **全项目深度审查+四阶段整改（P0修复/可靠性/安全/拆分重构）** | **通过（高危5+存量4修复；251用例全绿；pyflakes/mypy零告警；wheel 76文件）** |
| **第八轮** | **2026-11-10** | **遗留清零 + 开播提醒艾特全体新功能** | **通过（每群每日1次@全体；push并发安全/N+1/动态轮询/日志分离/会话持久化/群指令入口/大屏主题；293用例全绿）** |
| **第九轮** | **2026-11-10** | **全项目复审（新代码安全+正确性+文档核对）** | **通过（修复/绑定动态指令失效等1高5中3低+文档9处；「平台标签剥离」按用户结论回退）** |
| **第十轮** | **2026-11-10** | **遗留清零（大屏N+1/退群清理/动态语义/DB死代码/迁移/CI）** | **通过（318用例全绿；pyflakes/mypy零告警）** |
| **第十一轮** | **2026-11-11** | **v1.2.0全项目审查（时间戳迁移残留/安全/核心服务/数据库/文档五路并行深审）** | **通过（P0×8+P1×9修复并回归；含线程池耗尽、配置静默失效、公网API死代码等；376用例全绿）** |
| **第十二轮** | **2026-11-11** | **审查遗留逐项修复（架构/性能/加固/杂项）** | **通过（浏览器双页分离；v8索引+SQL分桶聚合；队列水位降级；PBKDF2 600k+口令8位；?token收窄/login；Docker非root；upsert写锁；config_dir锚定等）** |

## 当前全量基线（第十二轮审查后，v1.2.0）

| 检查 | 结果 |
|------|------|
| pyflakes（src+scripts+tests） | 0告警 |
| mypy | 0错误（59文件） |
| pytest | **380+用例全过**（约38秒，无网络依赖） |
| wheel | `blivepost-1.2.0-py3-none-any.whl`，含py.typed |

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
| （已于v1.2.0移除）`core/monitor.py`（LiveMonitor） | 顶层导出的库API；主流程已不使用。建议下个大版本评估移除 |
| `models/live.py` 的 UserInfo/DanmakuInfo/GiftInfo/SuperChatInfo/GuardInfo/DynamicInfo | 导出的库级数据模型，供库用户 |
| `services/wbi.py`（WbiSigner/wbi_sign/derive_wbi_key） | 第六轮确认非死代码：`dynamics.py`/`fingerprint.py` 在用，`test_wbi.py` 覆盖。bilibili.py 摘除签名调用不影响其存续 |
| `push.send_group_image` | 预留API，与单聊files接口对称 |
| `QueueDbBridge.__getattr__` 动态分发 | 子进程侧无需枚举主进程save_*方法清单 |

---

# 第七轮审查（2026-11-10）详细记录

> 背景：三路并行深度审查（数据库/进程层、服务层、测试/部署）发现多个"主路径正确、分支路径翻车"级缺陷与系统性技术债，随后按四阶段整改。
> 结论：**通过**。修复高危9项（含5项亲测复核确认的现网bug）、中危十余项；三大千行文件拆分；测试盲区补齐。

## 一、P0修复（全部亲测复核确认 + 回归测试）

| 级别 | 问题 | 位置 | 修复 |
|------|------|------|------|
| 高危 | `await bot.broadcast(...)` 等待同步方法必抛 TypeError——纯文本动态推送与UP开播提醒自上线起100%失败（旧测试桩把broadcast写成async导致从未暴露） | core/manager.py:533,552 | 去 await；测试桩改同步签名作回归防线 |
| 高危 | `/绑定`/`/解绑` 指令已移除但正则仍捕获，落入else分支被当作**取消监控**反向执行 | commands.py 正则 | 正则删除两词+注释警示；头部过时docstring同步 |
| 高危 | 机器人被移出群时直接 remove 房间监控，未检查其他群仍监控同一房间（A退群误停B群推送） | contacts.py on_group_del | unbind后查 get_monitored_groups，非空保留 |
| 高危 | docker-compose 设 BLIVEPOST_WEB_HOST=0.0.0.0 但 ENV_MAP 无此键，环境变量静默无效、端口映射不通 | config/env.py | ENV_MAP 补键+文档 |
| 高危 | `maintainer`/`db_service`/`dynamic_manager` try中途才赋值，启动早期异常时 finally NameError 掩盖原始异常并跳过后续清理 | __main__.py | 预清名单补全+None守卫（新增main_async冒烟测试7例覆盖） |

## 二、可靠性加固（阶段1）

- **热路径缓存**：房间→子库名进程内缓存（写穿透），弹幕/礼物落库从每事件一次主库会话降为零
- **事件循环去阻塞**：`verify_nav`（同步requests）4处调用全部 `asyncio.to_thread`
- **日结算重试**：失败当日60秒重试+运行日去重（原异常后直接顺延次日=静默丢一整天）
- **崩溃重建退避**：指数退避（5s起步/300s封顶/6次上限放弃），稳定运行10分钟后计数重置；`ensure(manual=)` 区分用户主动与重建回调
- **优雅关闭**：房间子库 engine close_all（含registry并发首建锁）、dynamic_manager 纳入 finally、stop_all 并行化、_watch 本地引用消除 exitcode 竞态
- **推送背压**：qqbot 发送改有界队列（200）+2 worker，满则丢弃告警；stop 哨兵排空30秒超时
- **SQLite**：WAL+synchronous=NORMAL+busy_timeout 15s（连接池事件重放）；MySQL/PG 用户/密码 quote_plus 转义

## 三、安全加固（阶段2）

- **登录防爆破**：IP+用户名双维度失败计数（5次/5分钟窗口→锁5分钟），err=3 提示
- **扫码会话隔离**：登录流程归属发起用户，他人不可查看二维码/状态/取消（原全局单会话可窥探抢登）
- **导出脱敏**：默认 VACUUM INTO 一致性快照+清除 cookie_account（原一个zip=全部B站凭证明文且热文件可能不一致）；`?credentials=1` 显式备份；后台双按钮+警示
- **web_token**：常量时间比较；长效令牌不再落 cookie、cookie 通道不再接受（仅 ?token= 与 X-Web-Token 头）
- **/api/status 需登录**（原对游客暴露 app_id/账号UID/到期天数/群规模）

## 四、拆分重构（阶段3，纯代码搬移，AST逐实体比对一致）

| 原文件 | 拆分后 |
|--------|--------|
| database/service.py（1940行） | service.py(100) + service_events.py(435) + service_stats.py(410) + service_admin.py(1075)；DatabaseService(EventWriterMixin, StatsMixin, AdminMixin) |
| services/webpanel.py（1482行） | webpanel.py(186) + webpanel_{common,auth,actions,pages,admin,screen}.py；WebPanel(五Mixin) |
| core/manager.py（1336行） | manager.py(446) + room_process.py(257) + consumer.py(322) + maintenance.py(392)；原导入路径全部 re-export |

- 公共接口不变：`from blivepost.core.manager import PushQueueConsumer`、`from ..database.service import GROUP_PUSH_SWITCHES`、tests 全部直接属性访问原样通过
- spawn pickle 安全：`_room_process_main` 迁至 room_process.py，pickle round-trip 验证

## 五、测试盲区补齐

- **test_qqbot_send.py（18例）**：send_group_message（msg_seq/去按钮重发/40034105低频提示）、send_group_media、push/broadcast/push_media 队列链路、_broadcast_message 降级、notify_admins、c2c 消息/图片——原850行零测试的核心出口链路
- **test_main_smoke.py（11例）**：22个重量组件全桩装配+finally顺序断言、早期异常无NameError、退出码语义、SESSDATA优先级

## 六、其他

- 顺带修复：`_handle_admin_export` 先 `Path(None)` 后判None的顺序错误（非SQLite时500而非409）；mypy 清零（原4个存量错误：`raise denied` 类型标注、Path(None)、本次新增2处在验证中即修）
- config.example.yaml 入库（由代码模板生成）；配置模板修正（移除已废弃 /绑定 提示、补 Template 5键）；.gitignore 覆盖 `scripts/panel_ids*.json`；PROJECT.md 过时条目同步

## 七、量化摘要

| 指标 | 值 |
|------|-----|
| 测试 | 200→**251用例**全过（+51，其中审查回归37+盲区29-覆盖重复合并计） |
| 静态检查 | pyflakes 零告警；mypy **0错误**（54文件，清零4个存量） |
| wheel | blivepost-1.1.0-py3-none-any.whl，76文件（新增12模块+16模板+py.typed） |
| 移除/收缩 | service.py -1940→100行、webpanel.py -1482→186行、manager.py -1336→446行（合计迁出约3800行至12个领域模块） |

---

# 第九/十轮审查（2026-11-10）详细记录

> 第九轮：三路复审第七~八批新增代码（安全/正确性/文档），发现并修复1高危（`CommandHandler` 未传 `dynamic_manager` 致 /绑定动态 指令自拆分后失效——亲测复核确认）、5中危（正文标签注入疑虑、UP提醒配额未过滤收群、DynamicManager缺log_dir、stop收尾窗口断言、Wbi外层TTL与内层日缓存矛盾）、3低危与9处文档不一致。
> **回退决定（用户结论）**：@全体每群每日1次为**平台硬性限制**，正文注入 `<qqbot-at-everyone />` 无法绕过（平台侧拒发/计数），第九轮的"平台标签剥离"修复前提不成立，已完整回退（strip_platform_tags 及其调用与测试）。
> 第十轮（遗留清零）：大屏 `_screen_context` N+1 并行化（3N串行→1批量+N并行）；退群清理补全（动态订阅解绑+无他群停轮询+at_all记录清理，ContactEventHandler 接线 dynamic_manager）；动态语义修复（重新订阅 `baseline_first=True` 不再补推停订期间动态、空列表≠异常告警、seen表保留500条修剪+写失败记日志、WbiSignerCache 改 monotonic 时钟根治偶发测试失败）；指令 UX（漏开关名引导文案、异常回显收敛为通用文案）；数据库层（primary_key_column/set_db_type 死代码移除→`BigInteger().with_variant(Integer,"sqlite")`、12处冗余索引删除、`_migrate_legacy_table` on_conflict_do_nothing 幂等化（实证旧实现崩溃重跑必卡IntegrityError）、information_schema 按 dialect 过滤当前库）；CI（Python 3.11/3.12/3.14 矩阵、vendor 预下载使补丁测试不再skip、wheel 构建冒烟）。
> 量化：**318用例全绿**（302→318）、pyflakes/mypy 双零、wheel 构建/打包资源核验通过。

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
