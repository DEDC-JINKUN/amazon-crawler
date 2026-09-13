# 持久化恢复交付账本（候选代码，生产未部署）

基线：`f9d2e1e4c6f3782e21153406991fc943bc3fcc86`，起始 tracked 干净。
风险门：R3；唯一 writer，冻结后独立只读 review。本轮不访问 Amazon/DataImpulse，
不迁移生产 schema，不回填/重排生产历史，不 push/tag。

## 新鲜证据

- 完整读取已批准阶段 1–2 计划及 task-package-delivery/TDD。
- 本机 PostgreSQL 服务运行；通过既有 DPAPI 创建并核验独立库
  `amazon_recovery_test_20260904_13c0`，有专属 ownership comment。
  测试子进程仅接收测试 DSN，不传生产 DSN/代理凭据/API Key。
- 生产审计使用 REPEATABLE READ READ ONLY 事务：当前 failed/blocked 共20，
  7 access_control、13 strict_variant；20/20 raw hash验证通过；生产写入0。
  dry-run绑定 tenant/ASIN/最新evidence ID/hash/状态时间/lease/snapshot与幂等键，禁止apply。
- 审计入口测试经历缺模块红灯→绿灯。
- PG跨重启冷却测试经历红灯→SQL参数类型修正→绿灯；随后预算/并发测试3通过。
- HTTP内部重试测试经历“第二次请求未经许可仍执行”红灯→绿灯。
- 中途全量离线：506 passed、10 skipped、1 failed；失败为既有relay关闭竞态，
  尚未作为最终验收；10 skip包含7旧PG与3当时新增PG，需独立库另跑。

## 验收映射

| 合同 | 公共验收边界 | 状态 |
|---|---|---|
| 生产只读历史分类/hash/dry-run | recovery_audit CLI | 20条hash通过，0生产写入 |
| 跨run/restart/refresh总预算、到期领取 | PostgresWorkerStorage claim/save/status | 独立PG与进程重启fixture通过 |
| 并发、租约、暂停/半开、Retry-After | 独立PG多consumer | 通过 |
| 内部HTTP/Firefox/换槽统一预算 | transport边界+Worker fixture | 通过，含旧任务延迟回调拒绝 |
| 大队列分批与Agent自动恢复 | Controller/Agent入口 | 分批、到期refresh与租约回收通过；常驻边界见下 |
| unknown流量、API/Console/receipt一致 | 投影/入口fixture | 通过，unknown不写0 |
| 迁移兼容/回滚说明 | 版本化SQL，专用测试DB | 独立库应用通过；生产未执行 |
| 新鲜全量/AST/Node/compile/diff/凭据 | 冻结候选 | 见最终新鲜证据 |
| 独立review与delta、本地commit | 冻结diff | 独立审查完成，确认项由红绿与最终回归关闭 |

不能用这份离线候选账本宣布生产问题已解决。下一真实固定20条由主控冻结cohort、
版本、预算与供应商计费基线后执行，本实现lane不启动真实网络采集。

## 首轮独立审查及修复

- 冻结 tree `d91acaaaf76816d7a470c926977b6cdf2f5c610f`：26文件。
  新鲜全量512 passed/17 skipped；17项在独立测试库全部通过；Node行为4项、
  compileall、Node语法、PowerShell AST、diff-check、凭据扫描0匹配。
- 独立只读审查 REQUEST_CHANGES：无P0，2个P1。
  Agent wrapper的run内排除集合改用底层公开方法传递；PG红绿证明同run不再领取、
  新run仍从attempt2继续。Console启动的历史evidence自动backfill已移除；真实PowerShell
  入口fixture红灯`legacyWrites=1`→绿灯`0`，未连接生产DB或启动服务。
- 自查loopback慢流证明socket timeout不能充当绝对deadline：许可1秒却在1.406秒返回200。
  现加入原生连接/套接字的绝对时钟，不改变TLS上下文或请求身份；慢响应头和慢正文
  两个红灯均已转绿。使用临时自签测试CA的本机HTTPS+CONNECT入口证明证书校验正常、
  代理认证只发送给CONNECT代理而不进入origin headers；测试密钥不进入Git。
- 预算中止路径另补PG红绿：旧投影将unknown误报0，现保留null并写不可变失败evidence。
- 修复期间新鲜全量516 passed/18 skipped、隔离PG18 passed；随后追加的中止用例
  独立通过。最终候选仍需再跑完整19项PG及全量，不能用数学相加代替新鲜验收。

## 部署与行为边界

- 生产schema/历史状态未改。应用版本化迁移与重启入口必须由下一阶段批准，
  缺迁移的正式Worker/Agent在claim前拒绝。
- 原有已知历史没有可证明的完整旧请求预算时，进入`legacy_budget_unknown`，
  不会给它一个新空预算；13条变体仅为dry-run建议，不自动回填。
- Controller的`--once`仍是有界run，不成为常驻守护进程；恢复领取由存活的consumer执行。
  Agent对已排队refresh会在到期后继续，不领取普通pending；到期时canary已过期则仍需
  新鲜授权，不能绕过Gate。没有承诺控制器退出后任务会自行执行。
- 预算口径：3次claim、12次HTTP尝试/Firefox导航许可、240个获准Firefox资源请求，
  HTTP压缩响应每次最多4MiB、CONNECT不透明payload累计64MiB后停止转发。
  后者可能包含拒绝前已收到的末尾分块，不等于供应商计费；计费仍unknown。
  HTTP导航尝试共享至少5秒间隔；stock Firefox子资源保持原生加载，但受请求/流量上限控制。
- 全局窗口按最近20个唯一ASIN/stage最终结果计数，重复同ASIN不伪造独立样本；
  连续2阻断或至少3个样本且3阻断、比例至少15%暂停，未放宽原阻断阈值。

## Delta审查与授权期限定点验证

- `b4d3d8b`新鲜全量516 passed/19 skipped；独立测试库19 passed，编译/Node/AST/diff/凭据扫描通过。
- 第二轮已确认首轮2个P1关闭；另提示deadline handler安装时序。
  其“pool新槽没有回调”的前提经正式池构造顺序loopback测试否证：池先构造、
  run/action开始、绑定hooks、再fetch建槽，慢headers会在许可内中止。
- 独立HTTP adapter的晚绑定路径则确实有缺口：补2个红灯，再增加幂等
  `enable_recovery_deadline()`于fetch前重建opener，保留Cookie jar和CONNECT认证。
  初始/晚绑定慢headers、慢body、真实TLS+CONNECT以及原adapter聚焦36项通过。
- 因为该项涉及授权TTL安全边界，仅追加此seam的定点核验，不开启第三轮全仓审查。

## 最终租约归因与消费端收口

- `d4a8b546`新鲜全量520 passed/19 skipped，独立库19 passed；定点审查自行跑
  recovery transport 9 passed，确认deadline修复，另提出复用槽hook归因疑点。
- 同步路径的正式`run_postgres_actions`+真实PG租约证明同槽两任务各计1次。
  再以旧任务延迟回调构造红灯，发现循环变量闭包能消费新任务预算；已改不可变task绑定、
  显式pool hook同步，并在新action前关闭旧browser/relay（代理出口与HTTP匿名cookie scope不变）。
  普通/延迟回调两个真实PG场景均通过，延迟旧lease必须被拒绝。
- 该测试最初在`begin_run`重建opener后覆盖了外部传输替身，意外尝试占位代理
  `proxy.example`而失败。测试库核查HTTP状态null、无raw、两槽network_error各1、Firefox次数0；
  宿主代理绕过检查为false，无付费代理凭据。现测试在opener重建边界固定替身，并硬禁
  Python网络/DNS和浏览器，只允许独立PG连接；修复后的证明不依赖真实外网。
- Agent对启动后才过期的claimed lease原先没有再扫，红灯显示一直不消费；现每次poll回收
  到期lease，保持refresh-only领取边界。加入测试验证无需第二次重启即可消费。
- 此后只做已确认风险的回归和最终证据汇总，不再泛化审查。
- 收口回归补充：耗尽HTTP transport把未知总字节从0改为null（已观测partial bytes另存），
  红绿通过；refresh结果状态与recovery预算、业务结果改为同事务提交，两个红灯证明
  进程在后续metrics/receipt回调前退出也不会遗留claimed，修复后均通过。

## 最终新鲜证据（本轮，不沿用此前505/7）

代码冻结tree：`b333ac2fddad7792fad0bf79351ad6ab53b3354a`。此后仅补本文验收记录与SQL回滚注释。

- `python -m pytest tests -q --tb=short -p no:cacheprovider --basetemp=.pytest_tmp_s1`：523 passed，23 skipped，61.54s。
- 通过DPAPI入口运行 `scripts/run_isolated_postgres_tests.py`，选择
  `tests/test_recovery_scheduler_integration.py tests/test_postgres_worker_integration.py`：23 passed，42.56s；
  全部skip已在明确命名的独立库补跑，不把skip当成功。
- `python -m compileall -q scripts tests`：通过；`node --check console/app.js`：通过。
- `node --test tests/console_frontend_auth.test.js`：4 passed。
- PowerShell AST：crawler、secure_dpapi_launcher、agent_service_control、run_owned_full_secure四个入口通过。
- 暂存diff-check、源码/测试新增行高置信凭据扫描通过；没有确认后未修复的P0/P1。
  原始独立审查REQUEST_CHANGES记录保留；最后的不可变hook、到期回收、事务收口由主writer新鲜回归验收，未再无限派发review。

未验收：生产schema部署、真实Amazon/stock Firefox实际页面表现、固定20成本/稳定性、供应商计费差额。
下一阶段先审查并批准生产迁移与预算，保持已声明的cohort不替换；新canary Gate通过后由主控测试。
回滚必须先停采集，保留预算和历史；不启动忽略新预算的旧Worker。测试库保留供复验。

## 12:21后的上线纠偏补丁

基础提交`951a2b52bf5906e6467a4ba01bf9114ec1b523d4`保留；用户要求14:00前完整发布包，
17:00 Go/No-Go、18:00目标上线。普通批次缺存活consumer的P1不能以存储层已有due claim替代。
现补一个固定cohort有界consumer：持久化ASIN集合/deadline，最多5-ASIN分批，冷却后自动恢复；
Controller与Agent存活消费的真实PG+硬禁网fixture通过。恢复后的run以唯一ASIN计分母，所有attempt raw/hash/授权保留。
部署操作、正式目录用户文档冲突、迁移dry-run/回滚、服务/Agent、固定20与100命令见
`docs/production_release_20260904.md`。本补丁不执行生产迁移或真实Amazon。

最终存活consumer补丁验证：代码tree `b559b2ff4072441c280de45da2bc744b465897cd`，
528 passed/28 skipped，独立库28 passed；compileall、Node语法/行为4项、PowerShell AST、diff/凭据扫描通过。
独立审查APPROVE，5400秒上限已统一。本阶段交付候选，不宣称17:00真实Go/No-Go或18:00上线已经完成。
