# 2026-09-04 首轮优先与短冷却候选

本包仅供停止旧run后，由主控集成并运行另行授权的固定50条。旧20条是中止记录，不是通过。C唯一writer未部署D、未访问Amazon/代理、未修改生产DB。

## 合同与范围

- 普通cohort首轮优先尚未尝试ASIN；首轮后仅恢复due任务。Agent refresh不受全量首轮屏障影响。
- 策略版本`captcha-first-pass-v2`：无Retry-After的CAPTCHA首次300秒、再次900秒、指数递增硬上限3600秒。明确更长Retry-After不被截断；transport默认60秒不变。
- 共享窗口最多20个唯一ASIN/stage：连续3个最终阻断，或至少10样本且阻断率≥60%，暂停300秒后仅一个半开任务。3个零散阻断不再触发1小时全局暂停。参数是待50条校准的项目试验，不是供应商要求或行业标准。
- 人工暂停、凭据/Gate、3次claim、12次导航、240次Firefox资源、已有字节上限、24小时任务deadline、5400秒cohort期限不变。每批最多5条，50条不一次预约100槽。
- 策略快照进入canary配置hash，因此必须重做canary；每次完成写`state_history.reason=recovery_policy:...`，记录策略、原/新冷却、原/新pause与服务端Retry-After。run evidence包含策略版本；Console共享出口投影保留观察到的版本。
- 人工stop先在受验证run目录写`operator.stop.json`再停Worker。后续Controller直接从PG只读读取最终cohort/attempt/流量，不依赖已关闭Console；保留实际worker exit，终态为`interrupted/operator_interrupted`，未知值仍unknown。旧receipt不被回写。
- Console性能最小修复只让确需严格兄弟变体兼容证明的记录读取raw；普通成功/blocked/transport进度不再全文gzip解压/hash/HTML解析。没有加可能掩盖raw损坏的缓存，也不放宽身份门。

## 已证实的性能原因

旧18条采样中16条无identity，仍进入raw补判；随后17个候选文件共4,632,933压缩字节，一个530,959-byte文件补判0.1588秒且结果仍false。元数据SELECT约0.0189秒；同期3个连接idle-in-transaction/ClientRead最长18.07秒。代码在事务内逐条补判，Controller每次2秒超时后服务端仍计算/发送，日志10053证明已断开后的发送。EXPLAIN仅估算，未执行ANALYZE，不把它当实测SQL耗时。未对生产Console压测。

## 旧计时器迁移（默认保留）

无需新schema。`scripts/recovery_policy_transition.py`默认只读，旧预算/deadline/outcomes/计时器均保留。

主控通过现有DPAPI入口传入精确`--tenant-id`和`--asins`。仅在确认停妥后，可增加`--reschedule-captcha --legacy-no-retry-after-confirmed`生成dry-run：后者是人工确认旧HTTP200 CAPTCHA没有Retry-After，不允许把未知自动当0。已记录的明确Retry-After即使传此标志仍拒绝缩短。非CAPTCHA/非200、活跃run/租约、人工暂停、独立计时器不一致均拒绝。

dry-run输出旧/新值、保持不变的预算/deadline/outcomes与`plan_hash`；主控核对后，以相同参数加`--apply --confirm-stopped --expected-plan-hash <刚核对的hash>`执行。apply在共享出口锁及任务行锁下重算计划，任何漂移拒绝。只改变选定任务的next_retry_at及确已不满足新门的旧策略pause；不清计数/outcomes、不造新tenant、不改raw/evidence。每个选定ASIN追加完整`recovery_policy_transition:`审计记录。

如果不能确认旧响应没有Retry-After，不迁移旧计时器；新50条仍必须面对保留的共享安全门，不能绕过。新版本部署不自动调整任何旧记录。

## 验收与拆分

先红后绿覆盖：普通首轮20条含3处零散blocked、300/900冷却、重启后预算、真正连续/高比例阻断、较长Retry-After、显式迁移默认保留/人工暂停/计划漂移、Console停止后的20请求19记录及attempt、operator interrupted。

核心候选与一次性浏览器bootstrap/UI分开；后者不是50条启动前置。最终新鲜测试、隔离PG、独立delta review及commit SHA以交付回执为准，不沿用旧测试计数。

## 本次新鲜验收

- 冻结核心代码tree `b4f9d4d904a132e5005e3e9276a8e7678e0a8961`导出验证：权威tests 536 passed/32 skipped；全部32项PG测试在标记隔离库32 passed。后续仅补精确审查序列及PowerShell入口测试、本文记录。
- Console停止后的真实PowerShell最终投影入口：20请求、19记录、19 attempts，14商品快照/2身份失败/3访问阻断，1未请求；HTTP调用0，1项隔离PG通过。
- 已知Retry-After→默认保留迁移审计→再次迁移仍拒绝缩短：2项隔离PG通过。独立审查原唯一P1的LIKE匹配前提经精确SQL及序列否证，审查者已撤回；本轮未确认P0/P1。
- Python compileall、Node语法/4项行为、4入口PowerShell AST、diff-check和高置信凭据扫描通过。没有生产DB写入、代理/Amazon请求或部署。
- 验证副本最初缺本机raw junction/driver夹具，且过长Windows临时路径导致raw fixture创建失败；使用空本地raw junction、现有driver和短临时根后完成上述新鲜通过。旧失败日志不当作通过，也未用真实raw替代fixture。
