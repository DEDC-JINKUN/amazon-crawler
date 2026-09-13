# 批次状态链重构与隔离验收

基线：71db26e。当前处于开发阶段，禁止将基础测试通过解释为部署通过。

## 已确认问题

1. operator_export 读取全局 item_state/product_latest，把历史数据算成本次完成。
2. BatchManager 将 progress.json 终态覆盖 batch.json，造成完成和失败提示并存。
3. 清理等待超时会放行下一批，文件回执未覆盖双 Worker 的嵌套授权。
4. 双 Worker 逐个预约；第一个可能耗尽整个代理池，第二个启动失败。操作级授权又被两个 child 覆盖。
5. recovery_job 以商品身份为键；重传 Excel 不会创建独立的采集意图，也不应直接清零风控预算。

## 决策

批次任务与商品历史分表。新增 collection_batch / collection_batch_member / collection_batch_run，数据库事务负责发布、成员领取和最终提交。现有商品表仍保存最新商品事实和历史。

提交 API 必须先发布批次及成员，再异步启动；返回 batch_id 后大屏立即能读取 queued 状态。初始成员 pending；历史信息为独立 history 字段，不参与当前完成计数。

一个 lifecycle coordinator 拥有状态转换。canary、Worker、exporter 只能提交事实，不能各自决定批次终态。正常完成、失败、取消均进入 finalizing/releasing；所有子进程退出且数据库租约释放后才允许 completed/failed/stopped。释放失败保持 cleanup_pending，重启由 coordinator 重试清理。

容量按实际并发窗口分配，并在批次层统一获取/释放。成员通过数据库领取，不能将两份 Excel 切片视为批次隔离的替代。容量不足要等待或显式降并发，不允许先启动半个双 Worker 批次再失败。

重叠清单默认表示新的采集意图，但沿用未过期的访问控制/恢复限制；复用结果必须显式标记 reused 并带原证据时间。该策略不得隐藏为 completed。

## 验收层次

- 基础不变量：真实 BatchManager 状态投影、真实 PostgreSQL 历史隔离、释放回执覆盖全部授权。
- 全链路：真实上传 API、coordinator、PostgreSQL、Worker、exporter、主大屏；仅外部网络响应替换为本机可控端点。
- Windows 进程故障：真实 terminate/kill、启动门、Job Object、服务重启。

基础不变量测试不是下面的完整矩阵，不计入三轮验收。

## 完整矩阵

normal / canary_failure / captcha / stop_canary / stop_collecting / process_crash / next_batch / restart。
各场景交叉 workers=1/2、size=1/3/10/20、history=new/repeated/overlap；每条检查批次状态、成员证据、进程、容量、回执、页面。

同一代码指纹连续三轮：clean、history、recovery。任一失败或代码变更清零；缺测试、skip、仅 fake runner 通过都不能放行。

## 当前隔离环境

- 专用数据库：127.0.0.1:55444/operator_matrix。
- 专用数据目录：D:/woring/爬虫/.test-tmp/operator-matrix-postgres。
- 无生产 DSN、DPAPI vault 或代理凭据；不访问 Amazon。
- 生产 8765/8770/8772 及展示维持原状。
