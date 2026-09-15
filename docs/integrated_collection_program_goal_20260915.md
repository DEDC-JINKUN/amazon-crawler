# PDP、独立评论与统一采集平台：可执行 Goal 方案

日期：2026-09-15（Asia/Shanghai）

本文件把《PDP与独立评论融合上线及操作手册》编译为四个顺序 Goal。源码归一、评论只读交付、评论批次与统一编排、生产迁移与 Amazon 业务门禁具有不同回滚边界；前一 Goal 的验收回执是后一 Goal 的输入，任何 Goal 失败都不能通过跳过步骤或降低门槛继续。

当前运行态不是本文件的权威内容。每次动作前必须重新读取 deploy pointer、服务 readiness、数据库计数和同一 PDP campaign；时间快照写入回执，不在本文复制“当前”数字。

## 全局不可变约束

- PDP 全量唯一权威仍是 `campaign-b14bef6a4b39509a9c92e1ff2629386b`。不得重提、拆分、换幂等键、删除或取消；只有用户明确要求停止时才可取消其未开始成员。
- PDP、Woot Reviews、西柚数据分别拥有 Worker、租约、状态、证据与存储。只在读取层按 `tenant + marketplace + ASIN`组合，不交叉覆盖真相表。
- 不读取个人浏览器资料、Cookie 或登录态；不做 TLS 指纹伪装、验证码绕过或自动换出口规避访问控制。UA 必须保留 `Agent/<name>`。
- 非 Amazon canary 只证明代理连通容量，不能冒充 Amazon 业务成功。
- 新代码必须有红绿测试；完整非 PostgreSQL 与按合同隔离的 PostgreSQL 套件不得有新失败；非平凡改动需独立 review 达到 P0=0、P1=0。
- 生产迁移、云端发布、真实 Woot 请求、Amazon live 门禁分别是独立外部写入门。代码完成不等于已部署；没有对应授权和回滚回执不得执行。
- 不清理现有未跟踪运行目录、测试集群、证据或历史数据；清理必须在完整汇报后另行取得用户确认。

## Goal 1：统一源码并建立可重建基线

任务：以`D:/woring/爬虫/Amazon Scraping`的公司 Git 主线为目标仓库，建立一个可从 Git 完整重建的 integration 基线；只完成来源裁决、边界化导入与代码审查，不同时开发 Review API、统一编排或部署能力。

完成标准：

- [ ] 生成机器可读 source manifest，逐项记录 PDP、独立评论、云端 v5、本机部署代码的路径、Git remote/branch/commit、tracked 状态、SHA256、采用或拒绝原因；未知来源明确标记，不能猜测。
- [ ] `Amazon Scraping`建立独立 integration 分支；公司主线、PDP `fc1a45f`、独立 Woot、云端 v5 审计副本之间的采用文件全部逐文件可追溯，不整体硬合并任一目录。
- [ ] 独立 Woot Provider、Worker、PostgreSQL storage、migration、导入/聚合/状态脚本、测试和文档全部成为 Git tracked 文件；不得带入真实 ASIN、outputs、pytest临时目录、凭据、dump或`.env`。
- [ ] `amazon_us`与`amazon_reviews`的schema、Worker、租约、状态和证据边界形成代码级合同；静态检查证明 Woot 不写 `amazon_us.item_state/product_snapshot`，PDP不占用Review租约。
- [ ] 云端 v5 的认证、Nginx、systemd工件只形成对账表；没有Git提交与源文件hash对应证据的内容不得直接合入。
- [ ] integration 分支从 clean checkout 可重建；`git ls-files`覆盖全部候选生产源码，`git status --short`仅允许manifest明确排除的运行数据/测试产物。
- [ ] 每个引入切片的聚焦测试、完整离线套件和独立review达到P0=0/P1=0；输出未部署回执和下一Goal可消费的commit/hash清单。

限制：本 Goal 不访问生产库、不迁移云端数据、不启动 Review/PDP Worker、不访问 Woot/Amazon、不修改 Nginx/登录网关，不实现`review_campaign/analysis_run`，不把外部目录中的未跟踪文件直接当作已审核源码。

## Goal 2：交付评论只读 API 与统一结果投影

任务：在 Goal 1 的可重建基线上交付评论数据读取、分页、来源和证据展示；先让团队安全使用历史数据，不启动 Review Worker。

完成标准：

- [ ] `amazon_reviews`迁移在全新 UTF-8 隔离 PostgreSQL 上可前向执行两次且第二次无副作用；迁移不写 `amazon_us.product_snapshot/item_state`。
- [ ] 历史评论导入先完成离线/隔离演练，回执包含 jobs、records、observations、raw evidence、hash mismatch、orphan 数；只有实测值可进入验收，手册中的 34,529/34,873 是待验证期望值。
- [ ] Review 只读 API 支持健康、ASIN 汇总、评论分页和证据引用；`limit`有界，1,093 ASIN/大评论集不会一次性加载。
- [ ] 统一 ASIN 只读投影同时呈现 PDP 与 Review 的独立时间、coverage、quality、source 和 error，不生成模糊的“全部已更新”标志。
- [ ] viewer 只能读取；所有评论 start/stop/Worker 管理路由在本 Goal 中不存在或返回明确拒绝。
- [ ] 聚焦测试、完整套件、隔离 PostgreSQL、权限矩阵、分页性能和独立 review 全部通过，并输出未部署回执。

限制：默认只做代码、文档和隔离测试；不启动Review Worker、不迁移生产数据、不发布云端。

## Goal 3：Review Campaign 与统一编排闭环

任务：为评论侧补齐安全批次控制，再实现只保存关联和汇总的 `amazon_control`统一编排层；网页、Agent 和 MCP 只使用 start/status/stop/results 四个深接口。

完成标准：

- [ ] `review_campaign/review_campaign_member`拥有唯一约束、幂等键、deadline、`stop_requested_at`、成员分页和来源终态；同一 ASIN 的活动 job 可共享但不重复抓取。
- [ ] Review claim 绑定 campaign membership 与租约；进程崩溃、租约过期和重启能恢复，且不产生孤儿 job/member/evidence。
- [ ] graceful stop 幂等：停止新 claim，只取消未被其他活动 campaign 共享的 queued 成员；running 成员完成当前 ASIN 或由租约安全回收；不删除数据和证据。
- [ ] Woot 的 401/403/429、HTML、坏 JSON 和结构缺失均为 `source_limited/blocked/failed`，绝不记录为 0 评论成功；稳定复合键与 `source_type=woot_ajax`保持。
- [ ] `amazon_control.analysis_run/source`只保存子任务引用和计数，不复制 PDP/评论正文；PDP 一侧 blocked 时 Review 可继续，Review 一侧 source_limited 时 PDP 可继续。
- [ ] 相同幂等键和相同请求返回同一 `analysis_run_id`；同键不同请求冲突；`available_sources`和`all_or_nothing`语义分别有自动化测试。
- [ ] start/status/stop/results 以及 ASIN overview/reviews 均有生产 Adapter 与内存 Adapter；状态和结果分页有固定上限。
- [ ] viewer/operator/admin 权限矩阵通过：viewer不能start/stop，operator不能强停服务或管理成员，admin强停不暴露给普通任务页面。
- [ ] 固定5-ASIN可控适配器矩阵覆盖 success、source_limited、retry、403/429停批、重复stop、崩溃恢复、两来源独立失败；全套与独立 review P0=0/P1=0。

限制：本 Goal 默认只做代码、文档和隔离测试。真实 Woot 小批、云端 systemd、生产 schema 和网页发布各需单独授权；Review Worker 默认 disabled/0 Worker。

## Goal 4：分阶段生产发布与 PDP 原 campaign 恢复门禁

任务：先发布只读面，再发布 Review Campaign/统一编排，最后用原 PDP campaign 的固定20个 pending 成员验证候选并决定是否恢复剩余全量。

完成标准：

- [ ] 发布前生成数据库备份标识、不可变 release、archive SHA256、current/previous pointer 和自动回滚脚本；代码可从 Git commit 重建且不包含凭据、真实 ASIN、outputs 或 pytest 临时目录。
- [ ] schema 与只读 API 先行发布并 live verify；Review Worker 在 Review Campaign/stop 协议上线前保持0；PDP Worker在业务门禁开始前保持停止新 claim。
- [ ] 新增 release-gate cohort seam：从原 campaign 事务选择并冻结20个 pending 成员，只允许候选 Worker领取该 cohort；不创建新 campaign、不更改原幂等键、不取消或伪终结其他成员；cohort ASIN 及选择条件写入不可变回执。
- [ ] 同一20成员的可信成功率≥90%；可信成功必须有本次请求后的 evidence、正确 ASIN 身份和必需 PDP 字段，旧快照不得计入。
- [ ] 同一20成员的 Amazon access-block 比例≤3%，browser fallback 比例≤10%。
- [ ] 从首个 claim 到第20个可信终态的有效窗口达到≥100 terminal/小时，即20条须在不超过12分钟的有效采集时间内终态；停机、人工等待和门禁准备时间不得混入分母。
- [ ] 运行期间一个 Worker/会话组熔断不终止其他健康 Worker；任何容量/凭据续期失败会先停止新 claim、回队未完成任务并清理 reservation/lease，不留下孤儿 Firefox/geckodriver。
- [ ] 原1093 campaign summary连续查询 p95≤2秒；Operator active只取summary，终态成员才分页。
- [ ] 若20条全部达标，才解除 cohort 限制并恢复原 campaign 剩余成员；若任一指标不达标，PDP保持fail-closed并输出机器JSON与人读Markdown失败报告，不降低阈值、不重试成另一组20条。
- [ ] live 验收覆盖登录、权限、start/status/stop/results、证据、导出、两来源独立健康与previous回滚；自动调度保持PAUSED，直到用户明确给出频率。

## 当前启动点

从 Goal 1 的 source manifest 与 clean-rebuild 审计开始。当前已完成的 PDP `fc1a45f`独立 review 是可复用输入，但不是整个 Goal 1 已完成；独立 Woot 核心文件目前主要位于另一仓库的未跟踪工作区，云端 v5 只有本地审计工件，二者都必须重新建立来源与审查证据。

Codex 控制面当前仍挂着旧的 PDP 效率 Goal。旧 Goal 未完成，不能被代理伪标完成。用户在界面停止旧 Goal 后，下一次启动应使用上面的 Goal 1 文本；在此之前只推进两者共同且可逆的只读盘点和文档对齐。
