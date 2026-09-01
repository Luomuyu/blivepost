# BLivePost 项目审查报告

> 第一轮审查：2026-09-01 ｜ src/ 23个模块、tests/ 12个文件 ｜ 结论：**通过（附修复与建议）**
> 第二轮审查：2026-09-02 ｜ 含第十五批（官方API对齐/数据量监控）后的全量复检 ｜ 结论：**通过**

## 一、审查方法

| 手段 | 覆盖内容 |
|------|----------|
| pyflakes 静态扫描 | 未用导入、未用变量、未定义名称（已全清，扫描结果为0） |
| 引用追溯（grep） | 候选死代码的全部引用点核对 |
| 装配审计 | `__main__` 主流程逐行核对变量作用域与生命周期 |
| 链路核对 | 群命令/私聊命令/弹幕事件/动态/失效轮换/维护循环/Web面板 8条链路 |
| 回归测试 | 125个用例全量通过（pytest + pytest-asyncio，37秒） |
| 默认配置一致性 | 自动生成的 config.yaml 与解析器逐段核对（8个段全部一致） |

## 二、发现并已修复的问题

### 严重（会在真实部署中触发）

| # | 问题 | 位置 | 修复 |
|---|------|------|------|
| S1 | `fingerprint.py` 缺失 `import random` 与 `Tuple` 导入（此前WBI重构误删）——`get_cookie()` 内 `get_fingerprint()` 调用 `random.randrange` 时直接 NameError，**主进程启动即崩溃** | services/fingerprint.py | 补齐导入；新增 `test_get_cookie_full_chain` 回归（mock网络，覆盖 buvid/fingerprint/sid 全链路） |
| S2 | `daily_report`/`live_watcher`/`consumer`/`web_panel` 在条件分支内赋值但 `finally` 无条件引用——**未启用对应功能时正常退出路径抛 NameError** | `__main__.py` | 全部预初始化为 None；`finally` 补齐 `consumer.stop()`/`web_panel.stop()`（此前消费者任务从不优雅停止） |

### 中等（性能/体验）

| # | 问题 | 修复 |
|---|------|------|
| M1 | 无 Redis 环境时 `FingerprintService` 构造**阻塞21秒**（redis-py 对 localhost 双栈地址族逐一连接重试） | `RedisCache` 连接超时收紧至1秒、禁用超时重试、**进程级降级记忆**（失败一次后本进程内直接走内存缓存） |
| M2 | `database/service.py` 的 `get_top_super_chat` 类型注解引用未导入的 `List/Tuple`（Python 3.14 延迟注解下不报错，但 `typing.get_type_hints` 会炸） | 补齐导入 |

### 轻微（代码卫生）

- 删除死代码 4 项：`utils/logger.log_to_file`（旧兼容函数，零引用）、`push.MessageContext`（零引用）、`vendor/importer.import_open_live_models`（零引用）、`vendor/patch._original_handle`（保存原方法但从未使用）
- 清理无用导入约 20 处（pyflakes 复扫为 0）
- 测试桩缺陷修正：指纹测试真实网络请求导致套件85秒（现37秒）、consumer测试缺 `binding_store`/`bot_client` 注入

## 三、功能实现核对（8条链路全部验证通过）

| 链路 | 路径 | 状态 |
|------|------|------|
| 群指令 | qqbot事件 → CommandHandler → 绑定/监控/取消/解绑/动态订阅/状态/收入/SC榜/汇总 → 被动回复 | ✅ 125个用例覆盖 |
| 私聊指令 | on_c2c_message_create → AdminCommandHandler：/登录（扫码）/账号/移除/恢复/导出/我 | ✅ 权限分级正确 |
| 弹幕事件 | 子进程blivedm → handler（日志+统计+落库，5类事件仅入库不推送）→ 队列 → consumer → 按房间绑定路由推送 | ✅ |
| 开播/下播 | LIVE/PREPARING + 子进程90秒状态校正合成事件 → 富媒体推送（封面）+ 统计落库 | ✅ |
| 动态监控 | dynamic_monitor开关（默认关）→ 每UID子进程wbi签名轮询 → 摘要/首图提取 → 富媒体推送订阅群；开播联动暂停；LiveStateWatcher批量状态共享 + 开播提醒 | ✅ |
| 凭证失效轮换 | 子进程LOG_IN_NOTICE → 队列 → 账号标记expired → 主管理员私聊预警 → 亲和/LRU换号重建 → 池空暂停 | ✅ |
| Cookie维护 | 6小时循环：临近过期（<7天或>25天）无头浏览器重载刷新 → 失效nav复核 → 标记+分级提醒 | ✅ |
| Web面板 | web_panel开关 → aiohttp（HTML + /api/status JSON）：账号池/房间/动态/下播记录 | ✅ 默认仅本机 |

装配完整性：chromium阻塞安装 → 配置 → 数据库 → 绑定/账号池加载 → 推送服务 → 指纹 → MonitorManager → DynamicManager → 机器人 → 消费者 → 结算/watcher/面板 → 恢复房间与订阅 → 常驻等待；`finally` 全组件安全停止。默认配置模板与解析器逐段一致。

## 四、保留的"看似无用"代码（有意保留，非遗留）

| 项 | 理由 |
|----|------|
| `core/monitor.py`（LiveMonitor） | 顶层导出的库API（`from blivepost import LiveMonitor`），单房间库级接入场景；主流程已不使用。**建议下个大版本评估移除** |
| `models/live.py` 的 UserInfo/DanmakuInfo/GiftInfo/SuperChatInfo/GuardInfo/DynamicInfo | 导出的库级数据模型，当前内部仅 DynamicInfo 历史引用已清；保留供库用户 |
| `QueueDbBridge.__getattr__` 动态分发 | 设计意图：子进程侧无需枚举主进程的save_*方法清单 |
| `push.py` 中已移除5类推送方法 | 弹幕/礼物/舰长/SC/红包仅入库为产品决策，方法与模板已同步删除，无残留 |

## 五、新功能建议（按优先级）

1. **按群推送粒度**（延续建议）：绑定关系增加 `push_types` 字段，各群自选订阅开播/天选等事件
2. **Web面板鉴权与趋势图**：token认证 + 按日聚合的折线数据（`/api/history?days=30`）
3. **账号池健康页**：Web面板增加Cookie剩余有效期分布、各账号轮换次数
4. **失效凭证自动重扫**：expired账号每日nav复核一次，密码重登等场景恢复后自动回到可用池（当前需手动`/恢复账号`）
5. **工程化**：CI（lint+mypy+pytest）、Dockerfile、`.env`密钥覆盖、重新构建wheel、**完成首次git提交**（当前全部改动未提交，为最高工程风险）

## 六、量化摘要

| 指标 | 值 |
|------|-----|
| 源码规模 | src/ 23个模块，约5600行 |
| 测试 | 12个文件，125个用例，37秒（无网络依赖） |
| 静态扫描 | pyflakes 0告警 |
| 数据表 | 14张 |
| 本次修复 | 严重2、中等2、轻微约25处 |
| 本次删除 | 死代码4项 + 未用导入约20处 |

## 七、遗留风险（代码外）

- 仓库无任何commit，全部工作仅存于工作区——**建议立即首次提交**
- 真机联调未做（botpy连接、扫码登录、真实群推送）
- vendor第三方镜像时效性依赖


---

# 第二轮审查（2026-09-02）

> 背景：第十五批（官方API对齐、数据量监控）合入后的全量复检。
> 结论：**发现并修复1个真实bug**（数据量提醒链路断裂）；其余链路与静态检查全部通过。

## 一、静态检查（三项全绿）

| 检查 | 结果 |
|------|------|
| pyflakes（src+tests） | 0告警 |
| flake8（src+tests） | 0告警 |
| mypy（37文件，阻塞级） | 0错误 |

## 二、发现并已修复的问题

| 级别 | 问题 | 位置 | 修复 |
|------|------|------|------|
| 严重 | **数据量提醒链路断裂**：`CookieMaintainer._check_db_size` 从 `db_service.db_path` 取文件路径，但该属性实际在 `DatabaseManager` 上——提醒功能从未生效 | core/manager.py | 修正取值链为 `db_service.db_manager.db_path`，并新增3个端到端测试（真链路触发/阈值内不触发/无管理员仅记录） |
| 严重 | `CookieMaintainer._notify_admins` 丢失分级逻辑（外部修改回滚导致）——数据库体积预警发给了全部管理员而非主管理员 | core/manager.py | 恢复sensitive分级；`_check_db_size` 的提醒补传 `sensitive=True`，测试断言修正为验证MAIN名单 |

## 三、死代码审计结果

| 项 | 结论 |
|----|------|
| `send_group_image`（群聊本地图片multipart直传） | 无内部调用——**预留API**，与单聊files接口对称，保留并注明 |
| `LiveMonitor` / models数据类 | 库级API，保留（同第一轮结论） |
| `_db_service_ref` | 无残留（已改为 `self.db_service` 直连） |
| 其余 | pyflakes零告警，无未用导入/变量 |

## 四、功能链路复检（全部通过）

- 数据量监控：面板体积展示（真链路）+ 超阈值私聊提醒（跨水位去重）+ sensitive分级 ✅
- msg_seq递增：登录流程文本(seq1)→二维码图(seq2)→结果(seq3+) ✅
- 动态轮询：wbi签名URL + 摘要/首图提取 + 状态文件联动 ✅（单测覆盖）
- 迁移器：v2 use_count加列对旧库升级/新库跳过 ✅
- 其余7条链路（群命令/私聊/弹幕事件/开播下播/失效轮换/Cookie维护/Web面板）第一轮结论维持 ✅

## 五、量化摘要（第二轮）

| 指标 | 值 |
|------|-----|
| 测试 | 12→13个文件，149个用例，38秒 |
| 静态检查 | pyflakes/flake8/mypy 三项零告警 |
| 本轮修复 | 严重2项（提醒链路断裂、分级丢失） |
| 保留项 | send_group_image（预留）、LiveMonitor（库API） |
