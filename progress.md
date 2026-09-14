# 当前进展

- 冻结干净基线 71db26e。
- 隔离 PostgreSQL 初始化成功；沙箱启动 pg_ctl 受限，按审批提升后仅启动测试端口55444。
- 基础状态/历史隔离红测明确失败，修复后通过。
- 候选数据库批次仓储和成员租约测试已加入。
- 最新隔离基础检查加既有 Operator 回归：83 passed，包含重叠并发和嵌套清理检查。没有计为完整八场景通过。
- 完整八场景尚未全部贯通，连续发布通过轮数 0/3。
- 生产服务、指针、清单及展示均未修改。

## 用户要求自行测试

- 对恢复/重复终态先跑出 4 个失败，并修复原始结果持久化与终态幂等。
- 对历史隔离后的 CSV finalize 跑出 4 个失败，并修复输出列投影。
- 修改后运行全套现有检查（包含26个隔离批次基础检查）：722 passed，55 skipped，2 deselected。JUnit：D:/woring/爬虫/.test-tmp/operator-independent-full.xml。
- 55项未运行与2项排除均不计通过；没有把上述结果计入三轮完整场景验收。
- 部署门仍关闭：实际启动器/Worker/大屏尚未接入新数据库批次模型，完整矩阵轮数0/3。

## 2026-09-13

- 只读核对主仓库、现行部署、MCP capability、loopback health、历史 Operator 批次回执和 2W child receipt。
- 未启动 campaign、未访问 Amazon、未读取代理凭据、未访问生产数据库、未修改已部署 release。
- 确认 Agent campaign API 支持最多 5,000 ASIN，但当前服务运行态为单 Worker。
- 用历史回执复现出两个独立根因：分子 Worker 容量预约导致第二个 Worker 被拒；Operator 历史投影与本轮 run evidence 不一致导致“页面完成、运行失败”。
- 发现当前隔离重构目录的 Git worktree 管理路径已丢失；在用户确认公共 seam 和权威源码前暂停产品代码/测试变更。
- 用户确认 Agent campaign 与 Operator start/status/stop 为公共验收 seam。
- 建立外置 Git 索引基线；未提交、未部署。
- TDD slice 1：WorkerCount 控制入口红测 1 failed；实现后与容量降级、Console 启动失败保护合计 3 passed。
- 完成共享模块设计：campaign 是权威事实源，Operator artifacts 是投影；Agent service pool 是唯一 Worker 编排者。
- TDD slice 2–7 完成：容量不足前置拒绝、campaign-only 进度、停止取消、loopback readiness、Operator/Agent 端口传播、Worker pool readiness、外部过期失败、轮询失败取消、blocked 分类均有红绿证据。
- 聚焦回归：106 passed；随后 Operator runner 当前测试 7 passed。PowerShell AST 检查四个改动入口均为 0 error。
- README、Collection API、Operator 手册与 CHANGELOG 已同步为共享 campaign 路线。
- 尝试启动既有隔离 PostgreSQL 55444：第一次命令引号错误；第二次被环境拒绝绑定 loopback。没有连接 55443/5432，也没有用生产库替代测试库。
- 最终非 PG 套件在补齐本地 raw 目录和 geckodriver 测试夹具后达到 713 passed、56 skipped；另有 12 个不依赖 PG 的 Operator 状态矩阵通过。17 个必须使用隔离 55444 的测试未执行。
- 修复 Windows loopback RST：鉴权用例 20/20 通过；campaign API 幂等提交/取消用例 10/10 通过；Client/API/Operator campaign 聚焦回归 18 passed。

## 2026-09-14

- 用户确认授权生产发布、DPAPI受控启动、会话槽扩容和 3→20→1093 自动放行；任一门禁失败停止。
- 直接前台启动已有隔离 PostgreSQL 数据目录成功绑定 `127.0.0.1:55444`；完整 Operator 矩阵 29 passed，其中包含此前缺失的17项PG引擎测试。
- 测试后以 Ctrl+C 关闭隔离 PostgreSQL；`pg_ctl status`确认无服务，55444无监听。
- 已派发一次只读独立 R3 差异审查；未访问生产库/代理凭据，尚未部署或提交 campaign。
- 经用户授权用 DPAPI 向一次性子进程只注入生产 DSN，执行只读探针：schema无缺表、目标 tenant 当前0条、旧 local tenant 3条；两个 tenant 均无活动 campaign/job/item lease/capacity reservation，目标 tenant 尚无 canary。
- 独立R3审查：P0=0，P1=3，发布继续保持停止。P1为多进程readiness未汇总内部健康/持续容量、子Worker无监督恢复、Runner丢失后Operator不能独立取消campaign。
- 纠正容量口径：当前per-ASIN策略下每 Worker 5-action pass需要10槽，三 Worker需要30个唯一槽；12槽结论作废。
- 第一轮P1修复后聚焦回归114 passed；完整非PG回归719 passed、56 skipped；隔离PG矩阵再次29 passed，测试PostgreSQL已用fast shutdown关闭。
- 第二轮独立delta复审仍为STOP：业务心跳不能证明Worker线程未卡死；cancel失败仍可能被BatchManager记为stopped；BatchManager可能把已完成campaign被晚到stop改写。按用户门禁停止，未部署、未改生产配置、未提交campaign。
- 用户授权继续第三轮代码修复。新增Worker线程业务心跳与超时有限重启；取消未确认保持`cancel_pending`且阻止下一批；后端completed优先于晚到stop。第三轮聚焦回归117 passed，尚待全套、PG和独立审查。
- 第三轮最终非PG套件724 passed、56 skipped、0 failed；隔离PG矩阵29 passed并fast shutdown。一次中间聚焦运行出现旧loopback job查询超时，单测复跑通过，最终全套无复现。
- 第三轮修复冻结提交`6b84fa1`。独立R3审查仍STOP：requested 3降为effective 1/2时仍可能ready；cancel调用返回completed时Runner仍可能写stopped；campaign_status completed但成员投影暂缺时BatchManager会写failed。另有Supervisor强杀/Job Object/控制文件竞态P2。未发布、未改生产配置、未提交campaign。
- 用户要求先保持上线爬虫运行再继续修复。核对release `592a65a`：`/healthz`与`/readyz`均200，单Worker为running。1093条清单校验为1093个唯一合法ASIN；使用稳定幂等键提交完整campaign时生产API以`asin_not_found`原子拒绝，未创建campaign。服务继续运行，等待核对现行ASIN导入入口。
- 未改变生产租户边界；改为对上线版已登记的3个ASIN提交后台campaign `campaign-579718cdd550503aba7e5ea996179b80`。API返回202，状态active，3项queued；release仍为`592a65a`、单Worker，未使用候选代码。
- 生产只读诊断确认3个refresh/recovery job预算均为new且未暂停；当前唯一容量事实已过期。随后按门禁对10个批准槽执行一次非Amazon canary：10/10为`network_error`，available/unique=0，Gate=`denied:unique_capacity_insufficient`。未访问Amazon、未重复探测；campaign保留queued，不能宣称正在实际抓取。
- 第四轮针对独立复审3个P1完成TDD：requested/effective/alive降级时API与`/readyz`阻断；cancel返回completed时后端终态优先；BatchManager不再把权威completed因本地成员投影暂缺改成failed。Windows 503拒绝路径补齐有界请求体读取，相关循环20轮稳定。
- 第四轮验证：相关模块122 passed；完整非PG套件727 passed、56 skipped；专用55444两文件29 passed并fast shutdown，确认55444无监听；Python编译与`git diff --check`通过。待冻结提交与独立R3 delta review。
- 第四轮冻结提交`328e8f9`。独立R3 delta review确认原3个P1已关闭，但新增2个P1：部分Worker计数字段fail-open；拒绝POST drain缺固定总时间上限。按用户“继续修”授权进入定点TDD，仍未部署。
- 两个复审P1均完成红绿：Worker计数字段全无时才允许旧单Worker兼容，部分出现即503；拒绝body drain使用固定总deadline。新增场景2 passed，Collection API文件25 passed；文档与CHANGELOG同步。
- 复审修复后的新鲜验证：相关模块124 passed；完整非PG 729 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；两个新增边界连续10轮均2/2通过。待冻结delta并由原独立reviewer复核。
- delta提交`55fc6ce`复核确认部分字段与固定drain deadline两个P1均关闭；遗留1个P1：完整计数使用`>=`而非严格相等，发布继续阻断。
- 最后一个P1完成红绿：完整计数字段改为`requested == effective == alive`。四个readiness/drain边界4 passed；修复后完整非PG 730 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；待最终只读复核。
- 最终冻结代码提交`b714556`经独立R3复核：P0=0、P1=0，可继续发布门禁。未部署；生产仍为`592a65a`单Worker，3-ASIN campaign因当天10槽非Amazon canary 0/10可用而queued，`/readyz`为503 `canary_fact_denied`。
- 继续诊断生产容量阻断：DNS、代理TCP和直连非Amazon目标正常；部署vault与旧源码vault分别进行一次脱敏CONNECT，均返回407。根因确定为供应商拒绝现有凭据。已在outputs准备仅替换代理字段的交互式DPAPI轮换入口，尚未运行、未改vault。
- 用户提供新代理凭据后首次交互轮换在临时文件ACL阶段因PowerShell 7 API兼容失败；原vault未替换。轮换入口改用Windows原生`icacls`，待重新执行。
- 第二次轮换成功：只替换代理字段，保留生产DSN/API Key，并生成加密回滚副本。新凭据的脱敏CONNECT返回`407 NO_USER`；官方文档确认应使用购买套餐后在Proxy Access生成的代理login/password。未运行canary、未重启服务、未访问Amazon。
- 用户通过截图提供Proxy Access生成凭据；已写入DPAPI vault且不在日志/仓库复述。端口823与粘滞端口10000均CONNECT 200/TLS1.3；10槽canary 10/10唯一可用。
- 重启旧上线版`592a65a`加载新generation；3-ASIN campaign最终2 succeeded、1 variant、0 failed，三条均为本请求后evidence。第三条一次租约到期恢复后成功。
- 新建独立30槽配置，离线确认15 action需要30槽；目标tenant非Amazon canary为30/30可用且唯一。随后事务初始化1093条，目标无活动campaign/job/lease/reservation。
- 候选archive `release-e345468.zip`含258项，SHA256固定为`9d79121ed025c2d80b4179df1f634819deaa3148bb6be561dd1fda42f0d0f62c`；提升激活脚本预检通过。部署releases目录保持管理员/SYSTEM只写；Codex触发UAC未显示可操作提示，pointer仍为`592a65a`，候选尚未安装。
- 用户通过ASCII/CRLF自提升启动器激活`e345468`；候选未在45秒内ready，脚本自动回滚pointer/wrapper并重启`592a65a`。备用8875/8870真实启动复现三个子Worker均因状态JSON原子替换WinError 5退出，且监督器耗尽重启预算；诊断服务随后关闭。
- TDD修复状态文件Windows短暂读锁：旧代码红测1 failed；20×10ms有界`PermissionError`重试与临时文件清理后目标测试1 passed、Agent服务文件28 passed。待全套、PG、独立review和新release。
- 修复后完整非PG套件730 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；Python编译与diff检查通过。待冻结提交与独立R3 delta review。
- 独立review对`7c731cf`发现1个P1：监督器重启仍预删可能被Windows锁住的状态文件。新增红测旧代码1 failed；移除非必要unlink并依靠PID匹配fail closed后，相关3 passed。待重新跑全套与最终delta复核。
- 监督器delta修复后完整非PG 731 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；Python编译与diff检查通过。待冻结提交和最终delta复核。
- 对`08e381c`复核仍有1个P1：PID复用时旧新鲜状态可能冒充新Worker。新增PID复用红测旧代码1 failed；每次spawn绑定不可复用launch token，子进程状态回传token且监督器同时匹配token/PID后，相关4 passed。
- launch token修复后完整非PG 732 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；Python编译与diff检查通过。待冻结提交和最终delta复核。
- 对`f62d315`复核仍有1个P1：token不匹配的旧failed/blocked仍会污染内部状态。PID复用测试扩展为running/failed/blocked，旧代码2 failed/1 passed；仅在token+PID匹配时采纳状态后3 passed。
- 旧终态污染修复后完整非PG 734 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；Python编译与diff检查通过。待冻结最终delta和复核。
- `30fa5ce`冻结release在备用端口显示3个受管Popen均存活、状态心跳也新鲜，但Popen父PID与实际Python子PID不同导致healthy=0。新增PID分层红测旧代码1 failed；以launch token绑定状态、Popen负责生命周期后4 passed。工作树备用8875/8870真实启动达到requested/effective/alive/healthy=3/3/3/3、restart=0、Console 200，随后已关闭。
- PID分层修复后完整非PG 735 passed、56 skipped；专用55444矩阵29 passed并fast shutdown；Python编译与diff检查通过。待冻结最终提交和只读delta复核。
- 最终代码`4021a56`独立R3 delta review为P0=0/P1=0。新release archive SHA256 `8981bb73ed5dfd2685b4442cd88be0eb1bf99076bd9d9d6bce03935bed1df2f3`，已安装到受保护releases目录但未激活。
- 30槽复测28可用被Gate拒绝；34槽配置复测34/34唯一可用且Gate allowed。冻结release备用端口最初因venv父子PID不等保持starting，最终代码工作树备用端口真实达到3/3/3/3。
- 生产计划任务激活暴露wrapper编码问题：UTF-8无BOM被Windows PowerShell5.1误读中文路径。PowerShell7直接运行生产wrapper时目标tenant三Worker严格ready；管理员BOM续接未形成可靠终态并回到旧pointer。
- 按多次发布失败停止自动重试。显式恢复并验证线上`592a65a`单Worker ready；目标tenant仍1093条已登记、0 active campaign/job/lease/reservation，全量未提交。
- 用户拍板长期方案：系统级安装PowerShell7并迁移计划任务，不依赖Codex缓存。已从官方/winget确认稳定WiX版本7.6.6.0，准备带任务XML备份与动作回滚的管理员安装脚本；尚未安装、任务动作未改。
- 系统级微软签名PowerShell 7.6.6已安装到`C:/Program Files/PowerShell/7/pwsh.exe`；`AmazonCollectionAgentService`和`AmazonCollectionPostgres`均完成XML备份并迁移到固定系统pwsh路径，旧release重启验证通过。
- 生产激活`4021a56`成功，目标tenant三Worker初始readiness为requested/effective/alive/healthy=3/3/3/3、restart=0，34槽事实中32个唯一槽可用，容量Gate allowed。
- 新3-ASIN门禁`campaign-0fa7b5ae1eb658c58e9ef8bae81ec2c6`运行中2 succeeded；随后三个Worker都在固定`worker-N.json`的`os.replace`遇到持续WinError 5，至少一个Worker重启且服务最终503。按门禁规则停止20条与1093条提交。
- 失败候选pointer已回滚到Collection `592a65a`、Operator `71db26e`；旧tenant的API `/readyz`再次返回200 `ok=true`。管理员恢复脚本未形成回执，当前端口进程存活但计划任务显示Ready，计划任务托管状态仍需在下一次发布前恢复为Running。
- 持续读锁复现稳定得到WinError 5（20次重试总耗时约0.22秒）。独立根因复核同意固定目标覆盖是协议级竞态；长期方案改为不可变launch-token/sequence快照，Supervisor单线程读取并缓存，API只读内存。
- 新状态协议完成两个TDD纵向切片：Windows持有旧固定文件读锁时Worker仍能发布；最新快照损坏时Supervisor回退到上一完整快照并由缓存响应。Agent服务文件当前35 passed；尚待全套、隔离PG、独立review、新release与重新3/20门禁。
- 初版长期协议验证为默认非PG 737 passed/56 skipped、隔离55444两文件29 passed并fast shutdown；Windows三真实子进程在3个锁定旧固定文件和8,000次并发status调用下持续15秒保持alive/healthy=3/3、restart=0。
- 独立review报告2个P1并停止发布：非整数JSON sequence可能被Python相等语义接受；跨launch锁定快照可能无界增长。两项均按TDD修复：sequence读写严格类型校验；每Worker跨launch总配额64文件/1 MiB、单文件4 KiB，启动前预留失败即阻止spawn，清理失败与积压进入readiness。Agent服务文件现39 passed，待重新跑扩大回归与delta复核。
- P1修复后新鲜验证：Agent服务40 passed；Agent/API/MCP/Windows入口94 passed；默认非PG 742 passed、56 skipped；隔离55444两文件29 passed并fast shutdown；三真实子进程在锁定旧固定文件和3,424次并发status调用下持续10秒保持3/3、restart=0、每Worker 3份快照。Python编译、diff检查通过；待原reviewer delta复核。
- delta复核原两P1后新增2个P1并继续停止发布：inventory glob/stat失败会低估积压，读取端接受sequence=0。新增红测3 failed；inventory不完整直接计为超限、读写统一正整数后3 passed。主代理自审另补多Worker启动事务性回收红绿，Agent服务现44 passed；待重新跑扩大/全量/PG/压力验证和再次delta复核。
- 最终P1修复后验证：相关98 passed；默认非PG 746 passed、56 skipped；隔离55444两文件29 passed并fast shutdown；三真实子进程在3个锁定旧固定文件和4,606次并发status调用下持续10秒保持3/3、restart=0、每Worker 3份快照。待最后delta复核。
- 最终独立delta复核：非整数/零sequence、跨launch无界增长、inventory未知fail-open均关闭；多Worker部分启动回收有效，P0=0、P1=0，建议CONTINUE到冻结提交与新release。审查者独立复跑Agent服务44 passed和diff检查。
- 长期状态协议代码、测试和同步文档冻结提交`8c1d9d7`（`fix: publish immutable worker status snapshots`）；未纳入工作区既有未跟踪数据/目录。下一步制作新的不可变release。
- 文档回执提交`33fd636`制作不可变release，archive SHA256`98867ce15c695b979dd05b557eee6b462bd2a76fab042cde15fe13e84aa1efa8`，文件集合相对`4021a56`仅新增`campaign_worker_status.py`。首次激活在PostgreSQL半就绪窗口的认证清理连接失败并自动回滚；增加认证只读probe后第二次激活成功。
- 激活事务先取消旧失败门禁并清零目标active campaign/job/lease/reservation，再刷新34槽canary得到31 available/unique、Gate allowed。两项计划任务均为系统PowerShell7托管Running；三Worker连续30样本及业务期间均3/3、restart=0，快照积压各3、cleanup failure=0。
- 新3-ASIN campaign`campaign-bd26b05e4b8055a691adc80d42c22561`终态1 succeeded、2 failed并取消。失败为4个canary可用会话到Amazon连接/响应头9–12秒无状态/无字节的transport_error；成功会话返回200/426001 bytes。按规则停止，20与1093未提交；release`33fd636`保持上线空闲。
- 2026-09-14 22:06用户明确要求开始全量。提交前重新核对release/operator`33fd636`、两项任务Running、三Worker3/3且restart0、31槽capacity allowed、目标tenant 1093 active ASIN和0 active campaign/job/lease/reservation；清单1093条全部唯一合法。
- 使用稳定幂等键`prod-33fd636-full1093-20260914-v1`一次提交单一后台campaign`campaign-b14bef6a4b39509a9c92e1ff2629386b`，API返回202。首个进度快照为total1093、pending1092、running1、terminal0，Worker仍3/3、restart0；未提交独立20条campaign。已知风险仍是此前3条门禁2/3 transport失败，未将其误报为已解决。
