# 客户/运营人员可用的前端控制台

## Problem

当前项目是开发者工具：前端只读（do_POST 返回 405），创建批次/启动采集/停止批次全靠命令行。客户/运营人员无法使用，需要远程桌面敲命令。

## Users

- 客户项目管理员：上传 ASIN 清单、启动采集、查看进度、停止批次
- 运营人员：查看采集进度、识别被拦商品、导出结果

## Goals

1. 前端可上传清单文件（CSV）创建批次
2. 前端可一键启动/停止协调器（采集引擎）
3. 前端可对指定批次请求停止
4. 协调器进程由 console.py 托管（spawn/health/stop），不依赖独立命令行窗口
5. 启动简化为一条命令：`.\crawler.ps1 console` 或 `python scripts\collection_console.py`

## Non-Goals

- 不改采集核心（worker/协调器/批次存储逻辑）
- 不做用户登录/权限（loopback-only 安全边界足够）
- 不做历史批次管理/结果导出（后续迭代）
- 不改数据库 schema

## Functional Requirements

| ID | 需求 | 类型 |
|---|---|---|
| FR-1 | 前端提供"上传清单"按钮，选择 CSV 后调 POST /api/batches 创建批次 | rule |
| FR-2 | 前端提供"启动采集"按钮，调 POST /api/coordinator/start，后端 spawn 协调器子进程 | rule |
| FR-3 | 前端协调器徽章从只读展示变为可点击控制（启动/停止） | rule |
| FR-4 | 前端每个批次行增加"停止"按钮，调 POST /api/batches/{id}/stop | rule |
| FR-5 | 后端 POST /api/batches 接收 multipart/form-data，解析 CSV，调 BatchStore.create_batch | rule |
| FR-6 | 后端 POST /api/coordinator/start 幂等：已运行则返回状态，否则 spawn 子进程 | rule |
| FR-7 | 后端 POST /api/coordinator/stop 请求协调器优雅退出 | rule |
| FR-8 | 后端协调器进程管理：记录 pid/启动时间，GET /api/coordinator 返回在线状态 | rule |
| FR-9 | 协调器子进程 stdout/stderr 重定向到临时日志文件，前端可查看最近 50 行 | rule |
| FR-10 | 协调器崩溃后 console.py 检测到（pid 不存在），标记离线，前端显示"重新启动"按钮 | rule |
| FR-11 | 所有新增 API 仅允许 loopback 访问（与现有安全策略一致） | rule |
| FR-12 | crawler.ps1 保持为一键入口，自动起 console | rule |

## Non-Functional Requirements

| ID | 需求 | 类型 |
|---|---|---|
| NFR-1 | 上传 5800 行 CSV 解析耗时 < 3 秒 | rule |
| NFR-2 | 协调器启动到第一个 worker spawn < 5 秒 | rule |
| NFR-3 | 前端按钮操作后 2 秒内给出反馈（成功/失败/进行中） | rule |
| NFR-4 | console.py 托管的协调器进程在 console.py 退出时自动终止（同进程组） | rule |
| NFR-5 | 新增代码必须有测试覆盖，现有测试全过 | rule |

## Constraints & Dependencies

- Python 3.8（运行环境）
- PostgreSQL 必须运行
- 协调器 CLI 参数已存在（create-batch / run / stop / status）
- BatchStore.create_batch / start_batch / request_stop / finalize_batch 已存在
- 安全边界：loopback-only，无外部暴露

## Acceptance Criteria

| ID | 验收标准 | 类型 |
|---|---|---|
| AC-1 | 前端页面有"上传清单"按钮，点击后弹出文件选择，选 CSV 后批次出现在列表 | rule |
| AC-2 | 协调器离线时前端显示"启动采集"按钮，在线时显示"运行中 · 停止" | rule |
| AC-3 | 点击"启动采集"后 < 5 秒，协调器徽章变绿，worker 开始 spawn | rule |
| AC-4 | 每个批次行有"停止"按钮，点击后批次状态变 stopping → stopped | rule |
| AC-5 | console.py 进程被 kill 后，协调器子进程自动终止（无僵尸进程） | rule |
| AC-6 | 5800 行 CSV 上传创建批次成功，进度条正确显示 | rule |
| AC-7 | 全部现有测试通过 + 新增 API/前端测试通过 | rule |
| AC-8 | 无 JS 报错、无 Python 异常日志 | rule |
