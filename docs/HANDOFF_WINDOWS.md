# Amazon US 采集项目 Windows 交接说明

## 当前状态

- 输入：`amazon_us_asin_manifest.csv`，1,892 个美国站 ASIN。
- 断点：`state/amazon_us.sqlite3`，随包交付，可直接续跑。
- 当前进度：1,891 个 `pending`；`B00RCPDCQU` 为 `failed/empty_review_page`（商品主体已采集，评论页返回 0 条，保留原评论 URL，最多重试 3 次）。
- 当前 CSV 在 `data/amazon_us/`；SQLite 是唯一事实源，CSV 可重复物化。
- 媒体只保存 URL 和元数据，不下载图片/视频文件。

## Windows 环境要求

1. Windows 10/11。
2. Python 3.11 或更高版本，安装时勾选 Python Launcher (`py`)。
3. 正常网络环境。静态页面优先走 HTTP；只有动态字段缺失时才需要 Mozilla Firefox，Selenium Manager 会自动管理 geckodriver。
4. 可以访问 Amazon.com 的正常网络环境；不要使用随机 IP、代理规避、CAPTCHA 处理或浏览器 Profile/Cookie 导出。

## 首次安装

双击：

```text
setup_windows.bat
```

脚本会：

- 创建 `.venv`；
- 安装 `requirements.txt`；
- 校验 1,892 条 manifest；
- 初始化/迁移 SQLite；
- 执行离线验收。

## 手动运行一次

双击：

```text
run_once_windows.bat
```

每次最多处理配置中的 10 个页面 action。遇到 429 保存当前 ASIN/评论页断点并退出，下一次续跑；403/CAPTCHA/明确访问拒绝标记为 blocked，不绕过、不重试。

## 安装 Windows 每小时任务

先完成首次安装，再双击：

```text
install_hourly_task_windows.bat
```

创建任务名：`Amazon US Collection Hourly`。

删除任务：

```bat
schtasks /Delete /F /TN "Amazon US Collection Hourly"
```

## 只做验收/重新生成 CSV

双击：

```text
verify_windows.bat
```

验收报告：

```text
data/amazon_us/verification/latest_verification.json
```

## 关键文件

- `scripts/amazon_us_worker.py`：采集、断点、解析和 CSV 物化。
- `scripts/amazon_us_verify.py`：SQLite/CSV 确定性验收。
- `scripts/verify_agent_review.py`：脱敏验收摘要。
- `config/amazon_us.windows.toml`：Windows 配置。
- `tests/`：离线回归测试。
- `docs/amazon_us_pipeline.md`：流水线说明。
- `docs/amazon_us_data_contract.md`：字段与状态契约。

## 安全边界

- 不包含 `.venv`、浏览器 Profile、Cookie、Token、密码或任何个人登录资料。
- 不读取个人 Chrome/Firefox 会话。
- 当前 POC 不启用代理池、随机/轮换 IP、验证码代解、身份伪装或模拟人类操作节奏；生产方案如接入 IP 代理池，只允许使用经过批准、来源可追溯的备用出口。
- User-Agent 保留透明标识 `Agent/amazon-us-worker`。
- 遇 429 停止本次运行并保留断点；遇 403/CAPTCHA/robot check/access denied 停止并标记 blocked。

## 重新跑测试

```bat
call .venv\Scripts\activate.bat
python -m py_compile scripts\*.py tests\*.py
python -m unittest discover -s tests -p "test_*.py" -v
python scripts\validate_us_manifest.py
python scripts\amazon_us_verify.py --once
```
