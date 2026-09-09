# 亚马逊美国站批量采集系统 · 部署说明

面向运维/操作员的日常使用文档。照着做就能跑起来。

## 一、系统组成

| 组件 | 是什么 | 怎么启动 |
|------|--------|----------|
| 控制台（网页） | 统一入口：上传清单、启动/停止采集、看进度、下载结果 | 双击 `启动前端.bat` |
| 协调器（后端） | 常驻进程，自动领取网页上传的批次，拉 Worker 并发采集 | 双击 `启动后端.bat`，或网页按钮"启动协调器" |
| PostgreSQL | 数据库，存批次、商品、评论、证据 | `启动前端.bat` 会自动拉起 |

数据流：网页上传清单 → 批次入库 → 协调器领批次 → Worker 采集（美国位置 cookie，强制 USD）→ 结果落库 → 网页看进度 → 下载 CSV。

## 二、环境要求

- Windows 10/11
- Python 3.11+（装好并加入 PATH）
- PostgreSQL 15+（本机默认装在 `C:\tools\pgsql`，数据目录 `C:\tools\pgdata`，密码 `123456`）

## 三、首次部署（只做一次）

### 1. 装 Python 依赖

```powershell
cd C:\Users\Administrator\Desktop\amazon-crawler-main
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pytest tests -q   # 自检，全绿才算装好
```

（不用 venv 的话，直接 `pip install -r requirements.txt`，系统 Python 装了 psycopg、openpyxl 即可。）

### 2. 初始化数据库

```powershell
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_postgres.ps1
```

需要重跑表结构升级时（比如新增了字段），执行：

```powershell
python _alter_canary.py
```

### 3. 确认连接串

两个启动脚本里已经写死：

```
AMAZON_US_POSTGRES_DSN=postgresql://postgres:123456@localhost:5432/amazon_us
```

密码不同就改脚本里这一行。

## 四、日常启动

1. 双击 `启动前端.bat` → 浏览器打开 http://127.0.0.1:8770
2. 双击 `启动后端.bat`（保持窗口开着 = 后端在运行；嫌麻烦可以不开，改在网页右上角点"启动协调器"）

> 两个入口等价：`启动后端.bat` 和网页按钮启动的是同一个协调器，走同一套批次系统，不存在绕开网页的通道。

## 五、日常操作流程（操作员视角）

### 1. 上传清单

- 网页右上角"上传清单"，支持 `.csv` 和 `.xlsx`（首行 `asin` 列，只有 ASIN 也行，URL 自动补全）
- 旁边的"采集评论"复选框：勾选 = 商品+评论都采；不勾 = 只采商品，批次快很多
- 上传后立即显示本批次全部待采集商品

### 2. 采集过程

- 批次先跑 **canary 预检**（少量探针成员），探针全过才放开全量，避免整批被拦
- 成员状态：待定 → 采集成功 / 已拦截 / 失败 / 变体
- "变体"表示页面跳到了同变体组的另一个 ASIN（比如默认颜色），不算失败，会记录实际跳到的 ASIN
- 想中途停：批次详情里点"停止批次"

### 3. 导出结果

批次完成后，点"下载 CSV"导出商品数据（标题、价格、货币、评分、BSR 排名、评论数、批次状态等）。

## 六、Agent API（程序调用）

```bash
# JSON 建批次（幂等：同 idempotency_key 24小时内重复调用返回同一批次）
curl -X POST "http://127.0.0.1:8770/api/batches/json?tenant=amazon_us_local" \
  -H "Content-Type: application/json" \
  -d '{"asins":["B01LWJ0JIC","B06VWMP73S"],"idempotency_key":"my-task-001"}'

# 查进度
curl "http://127.0.0.1:8770/api/batches/{batch_id}?tenant=amazon_us_local"

# 查成员明细（含库内快照时间，可判断哪些能复用）
curl "http://127.0.0.1:8770/api/batches/{batch_id}/items?tenant=amazon_us_local&limit=6000"

# 导出 CSV
curl -o result.csv "http://127.0.0.1:8770/api/batches/{batch_id}/export?tenant=amazon_us_local"
```

注意：
- 关键词排名采集**不支持**，接口会明确报错（只支持 ASIN 商品详情/评论/BSR）
- 配了只读 key（`AMAZON_CONSOLE_READ_KEY`）时，只读 key 只能查询，建批次/启停会被拒绝

## 七、常见问题

| 现象 | 原因 | 处理 |
|------|------|------|
| 网页打不开 | 控制台没启动 | 双击 `启动前端.bat` |
| 批次一直 pending | 协调器没启动 | 双击 `启动后端.bat` 或网页点"启动协调器" |
| 数据库连接失败 | PostgreSQL 服务没跑 | 启动脚本会自动拉起；手动：`C:\tools\pgsql\bin\pg_ctl.exe -D C:\tools\pgdata start` |
| 成员被拦截（blocked） | 亚马逊反爬 | 正常现象，等批次重试；整批被拦会自动收尾并标注 blocked |
| 评论显示 skipped/login_wall | 亚马逊限制未登录浏览评论 | 属预期：商品数据正常入库，不影响成员成功 |
| 采出来货币不是美元 | 位置 cookie 失效 | 系统会自动设美国地址（邮编 30322）+ USD cookie；个别仍错的成员会走浏览器兜底 |

## 八、性能参考

限速是内置的（亚马逊反爬决定的天花板），实测吞吐参考 `测试报告.md` 的 P2-12 证据（20 条预跑 + 100 条正式实测）。
