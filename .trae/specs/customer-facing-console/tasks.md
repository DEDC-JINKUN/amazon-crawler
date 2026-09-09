# 实施任务

## Task 1：后端 API — 创建批次（POST /api/batches）

**对应 AC**: AC-1, FR-5
**优先级**: high

### 改动范围
- `scripts/collection_console.py`: 新增 `do_POST` 处理 `/api/batches` 路径，接收 multipart/form-data，解析 CSV，调 `BatchStore.create_batch`

### 实现要点
1. 解析 multipart/form-data（无第三方库，用 email.message 或手动 boundary 解析）
2. CSV 解析：支持标准 CSV（逗号分隔、双引号转义），每行一个 ASIN，首行可跳过（BOM 检测）
3. ASIN 校验：10 位大写字母数字，用现有 ASIN_RE
4. 调 `BatchStore.create_batch(rows)` 创建批次，返回 batch_id
5. 临时文件用完删除
6. 错误处理：空清单、ASIN 格式错误、重复 ASIN、数据库异常

### 测试要求
- TR-1: 上传合法 CSV → 返回 200 + batch_id
- TR-2: 上传空文件 → 返回 400
- TR-3: 上传含非法 ASIN → 返回 400 + 具体行号
- TR-4: 上传 5800 行 CSV → 3 秒内返回
- TR-5: loopback 外请求 → 拒绝

---

## Task 2：后端 API — 协调器进程托管（spawn/health/stop）

**对应 AC**: AC-2, AC-3, AC-5, FR-6, FR-7, FR-8, FR-9, FR-10
**优先级**: high

### 改动范围
- `scripts/collection_console.py`: 新增 CoordinatorManager 类，管理协调器子进程生命周期
- 新增 `/api/coordinator` GET/POST/DELETE 路由

### 实现要点
1. CoordinatorManager 单例：spawn 协调器子进程（batch_coordinator.py run --tenant-id xxx）
2. 子进程 stdout/stderr 重定向到临时日志文件（`state/coordinator.log`）
3. 用 `CREATE_NEW_PROCESS_GROUP` + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`（Windows）确保 console.py 退出时协调器自动终止
4. GET /api/coordinator：返回 `{status, pid, started_at, last_heartbeat, recent_log}`（recent_log 读日志文件最近 50 行）
5. POST /api/coordinator/start：幂等——已运行返回当前状态，否则 spawn
6. DELETE /api/coordinator：优雅停止（给数据库发 stop_request 信号，协调器自己收尾）
7. 健康检查：pid 存在 + 数据库 coordinator_state 心跳 30 秒内 → online
8. 崩溃检测：pid 不存在 → 标记 offline，前端显示"重新启动"

### 测试要求
- TR-1: POST start → 协调器 spawn → GET 显示 online
- TR-2: 重复 POST start → 返回当前状态，不重复 spawn
- TR-3: DELETE → 协调器优雅退出 → GET 显示 offline
- TR-4: kill console.py → 协调器子进程自动终止（无残留）
- TR-5: 模拟协调器崩溃（kill 子进程）→ GET 检测到 offline
- TR-6: recent_log 返回最近 50 行，无日志时返回空

---

## Task 3：后端 API — 停止批次

**对应 AC**: AC-4, FR-4
**优先级**: medium

### 改动范围
- `scripts/collection_console.py`: 新增 `POST /api/batches/{id}/stop`

### 实现要点
1. 调 `BatchStore.request_stop(batch_id)` 设置 stop_requested=true
2. 返回当前批次状态
3. 批次不存在 → 404
4. 已终态批次 → 400 + 说明

### 测试要求
- TR-1: 运行中批次 → stop_requested=true 返回
- TR-2: 不存在批次 → 404
- TR-3: 已 completed 批次 → 400

---

## Task 4：前端 — 上传清单 + 创建批次按钮

**对应 AC**: AC-1
**优先级**: high

### 改动范围
- `console/index.html`: 新增上传表单
- `console/app.js`: 新增上传逻辑

### 实现要点
1. 批次面板标题旁加"上传清单"按钮
2. 点击弹出隐藏的 `<input type="file" accept=".csv">`
3. 选文件后 fetch POST /api/batches（FormData）
4. 成功 → 刷新批次列表 + 自动选中新批次
5. 失败 → alert 显示错误信息

### 测试要求
- TR-1: 点按钮 → 文件选择器弹出
- TR-2: 选 CSV → 批次出现在列表
- TR-3: 非法文件 → 错误提示
- TR-4: 控制台无 JS 报错

---

## Task 5：前端 — 协调器启动/停止控制

**对应 AC**: AC-2, AC-3
**优先级**: high

### 改动范围
- `console/app.js`: 协调器徽章变为可点击控制

### 实现要点
1. 协调器徽章根据状态显示不同按钮：
   - offline → "启动采集"（绿色按钮）
   - online → "运行中 · 停止"（灰色按钮 + 小红点 + 停止链接）
2. 点击启动 → POST /api/coordinator/start → 2 秒后轮询状态
3. 点击停止 → DELETE /api/coordinator → 确认后执行
4. 状态轮询：5 秒一次（复用现有刷新机制）

### 测试要求
- TR-1: offline 状态显示"启动采集"按钮
- TR-2: 点击启动 → 5 秒内变 online
- TR-3: online 状态显示停止链接
- TR-4: 控制台无 JS 报错

---

## Task 6：前端 — 批次行停止按钮

**对应 AC**: AC-4
**优先级**: medium

### 改动范围
- `console/app.js`: 批次表格操作列

### 实现要点
1. 每行最后加操作列，运行中批次显示"停止"按钮
2. 点击 → 确认对话框 → POST /api/batches/{id}/stop
3. 按钮仅对 pending/running/stopping 批次显示，终态不显示

### 测试要求
- TR-1: running 批次有停止按钮
- TR-2: completed 批次无停止按钮
- TR-3: 点击停止 → 批次状态变 stopping

---

## Task 7：回归验证

**对应 AC**: AC-7, AC-8
**优先级**: high

### 改动范围
- 新增 pytest 测试文件 `tests/test_console_control_api.py`
- 跑完整测试集

### 测试要求
- TR-1: 完整测试集通过（现有 326 + 新增 ≥ 10）
- TR-2: 手动 E2E：上传清单 → 启动协调器 → 看进度 → 停止批次 → 批次收尾
- TR-3: 无新增 JS 报错 / Python 异常
