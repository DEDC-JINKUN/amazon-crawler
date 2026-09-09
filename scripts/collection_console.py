#!/usr/bin/env python3
"""Loopback-only 采集控制台：前端静态页面 + 后端 JSON API + 协调器进程托管。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "console"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
TENANT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")
ITEM_PATH = re.compile(r"^/api/items/([A-Za-z0-9]{10})$")
RUN_PATH = re.compile(r"^/api/runs/([A-Za-z0-9_-]+)$")
# 批次接口：batch_id 是标准 UUID
BATCH_ID = r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
BATCH_PATH = re.compile(rf"^/api/batches/{BATCH_ID}(?:/stop)?$")
BATCH_ITEMS_PATH = re.compile(rf"^/api/batches/{BATCH_ID}/items$")
# 批次结果导出：下载本批次全部成员的 CSV
BATCH_EXPORT_PATH = re.compile(rf"^/api/batches/{BATCH_ID}/export$")
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}
SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
RUNTIME_FINGERPRINT = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _context_value(row: dict[str, Any]) -> dict[str, Any]:
    context = row.get("context_json") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return context if isinstance(context, dict) else {}


def summarize_context_quality(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"full": 0, "partial": 0, "invalid": 0, "unknown": 0}
    for row in rows:
        quality = str(_context_value(row).get("context_quality") or "unknown")
        counts[quality if quality in counts else "unknown"] += 1
    return counts


def _explicit_sibling_identity(row: dict[str, Any]) -> bool:
    identity = _context_value(row).get("identity") or {}
    if not isinstance(identity, dict):
        return False
    requested = str(identity.get("requested_asin") or row.get("asin") or "").upper()
    observed = str(identity.get("observed_asin") or "").upper()
    canonical = str(identity.get("canonical_asin") or "").upper()
    parent = str(identity.get("parent_asin") or "").upper()
    children = {str(value).upper() for value in identity.get("child_asins") or []}
    return bool(
        ASIN_RE.fullmatch(requested)
        and ASIN_RE.fullmatch(observed)
        and ASIN_RE.fullmatch(parent)
        and requested != observed
        and canonical == observed
        and requested in children
        and observed in children
    )


def classify_evidence_outcome(row: dict[str, Any]) -> str:
    if row.get("block_reason"):
        return "blocked"
    if row.get("error_code") == "variant_redirect":
        return "variant_redirect"
    if row.get("error_code") == "asin_mismatch" and _explicit_sibling_identity(row):
        return "variant_redirect"
    if row.get("error_code"):
        return "failed"
    return "completed"


def project_price_status(product: dict[str, Any] | None) -> str:
    product = product or {}
    if str(product.get("price") or "").strip():
        return "available"
    availability = str(product.get("availability") or "").strip()
    buy_box = product.get("buy_box") or {}
    buy_box_text = str(buy_box.get("text") or "") if isinstance(buy_box, dict) else str(buy_box)
    actionable_buy_box = bool(
        isinstance(buy_box, dict) and any(buy_box.get(key) for key in ("seller", "ships_from", "coupon"))
    ) or bool(re.search(r"(?:add\s+to\s+cart|buy\s+now)", buy_box_text, flags=re.IGNORECASE))
    if not actionable_buy_box and re.search(
        r"(?:currently\s+unavailable|temporarily\s+out\s+of\s+stock|not\s+available)",
        availability,
        flags=re.IGNORECASE,
    ):
        return "unavailable"
    return "missing"


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def project_batch_durations(runs: list[dict[str, Any]]) -> dict[str, float | bool | None]:
    completed = [run for run in runs if _coerce_datetime(run.get("started_at")) and _coerce_datetime(run.get("finished_at"))]
    active = round(sum(float(run.get("duration_seconds") or 0) for run in completed), 2)
    starts = [_coerce_datetime(run.get("started_at")) for run in completed]
    finishes = [_coerce_datetime(run.get("finished_at")) for run in completed]
    wall = round((max(finishes) - min(starts)).total_seconds(), 2) if starts and finishes else None
    return {
        "active_duration_seconds": active if completed else None,
        "wall_span_seconds": wall,
        "wall_span_includes_idle": bool(completed),
    }


def project_run_durations(
    ledger: dict[str, Any] | None,
    observed_start: Any = None,
    observed_end: Any = None,
) -> dict[str, Any]:
    ledger = ledger or {}
    started = _coerce_datetime(ledger.get("started_at")) or _coerce_datetime(observed_start)
    finished = _coerce_datetime(ledger.get("finished_at")) or _coerce_datetime(observed_end)
    worker_duration = round((finished - started).total_seconds(), 2) if started and finished else None
    receipt = ledger.get("receipt_json") or {}
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except (TypeError, ValueError, json.JSONDecodeError):
            receipt = {}
    try:
        controller_duration = round(float(receipt.get("elapsed_seconds")), 2) if receipt.get("elapsed_seconds") is not None else None
    except (TypeError, ValueError):
        controller_duration = None
    source = "collection_run.started_at_to_finished_at" if ledger else "collection_evidence.first_to_last"
    if worker_duration is not None and controller_duration is not None and worker_duration > controller_duration:
        worker_duration = controller_duration
        source = "receipt_json.elapsed_seconds_backfill_cap"
        started = _coerce_datetime(receipt.get("started_at")) or started
        finished = _coerce_datetime(receipt.get("finished_at")) or finished
    return {
        "worker_duration_seconds": worker_duration,
        "controller_duration_seconds": controller_duration,
        "duration_source": source,
        "effective_started_at": started,
        "effective_finished_at": finished,
    }


def summarize_traffic(rows: list[dict[str, Any]]) -> dict[str, dict[str, int | None]]:
    accumulators = {
        "http_compressed_response": {"known_bytes": 0, "known_records": 0, "unknown_records": 0},
        "firefox_main_document": {"known_bytes": 0, "known_records": 0, "unknown_records": 0},
        "firefox_subresources": {"known_bytes": 0, "known_records": 0, "unknown_records": 0},
    }
    for row in rows:
        context = _context_value(row)
        context_traffic = context.get("traffic") if isinstance(context, dict) else {}
        context_traffic = context_traffic if isinstance(context_traffic, dict) else {}
        source = str(row.get("source_type") or "unknown")
        http_applicable = source == "http_html" or "http_compressed_response_bytes" in context_traffic
        if http_applicable:
            value = context_traffic.get("http_compressed_response_bytes")
            if value is None and source == "http_html":
                value = row.get("transfer_bytes")
            if value is None:
                accumulators["http_compressed_response"]["unknown_records"] += 1
            else:
                accumulators["http_compressed_response"]["known_bytes"] += max(0, int(value))
                accumulators["http_compressed_response"]["known_records"] += 1
        for category, byte_key, unknown_key, known_key in (
                ("firefox_main_document", "firefox_main_document_bytes", "firefox_main_document_unknown_count", "firefox_main_document_known_count"),
                ("firefox_subresources", "firefox_subresource_bytes", "firefox_subresource_unknown_count", "firefox_subresource_known_count"),
            ):
            applicable = source == "selenium_dom" or any(
                key in context_traffic for key in (byte_key, unknown_key, known_key)
            )
            if not applicable:
                continue
            value = context_traffic.get(byte_key)
            unknown = int(context_traffic.get(unknown_key) or 0)
            if value is None:
                accumulators[category]["unknown_records"] += max(1, unknown)
            else:
                accumulators[category]["known_bytes"] += max(0, int(value))
                accumulators[category]["known_records"] += 1
                accumulators[category]["unknown_records"] += unknown
    result = {
        category: {
            "bytes": None if values["unknown_records"] else values["known_bytes"],
            "known_records": values["known_records"],
            "unknown_records": values["unknown_records"],
        }
        for category, values in accumulators.items()
    }
    result["proxy_dashboard_bill"] = {"bytes": None, "known_records": 0, "unknown_records": 1}
    return result


# ---------------------------------------------------------------------------
# 协调器进程托管：console.py spawn/health/stop 协调器子进程
# ---------------------------------------------------------------------------

COORDINATOR_SCRIPT = ROOT / "scripts" / "batch_coordinator.py"
COORDINATOR_LOG = ROOT / "state" / "coordinator.log"
COORDINATOR_LOG.parent.mkdir(parents=True, exist_ok=True)


class CoordinatorManager:
    """协调器子进程生命周期管理（单例，console.py 持有一个实例）。

    设计要点：
    1. spawn 协调器 CLI（batch_coordinator.py run --tenant-id xxx）
    2. 子进程 stdout/stderr 重定向到 state/coordinator.log
    3. Windows 下用 CREATE_NEW_PROCESS_GROUP，console.py 退出时子进程自动终止
    4. 健康检查：pid 存在 + 数据库 coordinator_state 心跳 30s 内 → online
    """

    def __init__(self, dsn: str, tenant_id: str):
        self.dsn = dsn
        self.tenant_id = tenant_id
        self._proc: subprocess.Popen | None = None
        self._started_at: datetime | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _proxy_env() -> dict[str, str]:
        """从 .env 读 ZooProxy 凭证注入子进程环境（协调器是 worker 的环境变量来源）。"""
        env: dict[str, str] = {}
        try:
            for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
                m = re.match(r"^\s*ZOOPROXY_(HOST|USERNAME|PASSWORD)\s*=\s*(.+)$", line)
                if m:
                    env[f"ZOO_PROXY_{m.group(1)}"] = m.group(2).strip()
        except OSError:
            pass
        return env

    @staticmethod
    def _worker_config() -> Path:
        for name in ("amazon_us.windows.human.toml", "amazon_us.linux.human.toml", "amazon_us.human.toml"):
            p = ROOT / "config" / name
            if p.exists():
                return p
        raise FileNotFoundError("config/ 下没有 human.toml 类人类配置")

    @staticmethod
    def workers_setting() -> int:
        """当前持久化的 worker 数（UI 展示用；协调器启动参数的真实来源）。"""
        try:
            data = json.loads((ROOT / "state" / "coordinator_workers.json").read_text(encoding="utf-8"))
            return int(data.get("workers") or 2)
        except (OSError, ValueError):
            return 2

    @classmethod
    def _save_workers_setting(cls, workers: int) -> None:
        (ROOT / "state").mkdir(parents=True, exist_ok=True)
        (ROOT / "state" / "coordinator_workers.json").write_text(
            json.dumps({"workers": int(workers)}), encoding="utf-8"
        )

    @staticmethod
    def kill_all_crawler_processes() -> int:
        """杀掉全部协调器+worker 进程（含外部启动的，如 switch_to_proxy.ps1 拉起的）。

        返回杀掉的进程数。Windows 走 WMI 查命令行匹配；Linux 走 pkill -f。
        """
        count = 0
        if sys.platform == "win32":
            ps_script = (
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                "Where-Object { $_.CommandLine -match 'batch_coordinator\\.py|amazon_us_worker\\.py' -and "
                "$_.CommandLine -match 'amazon-crawler' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True, timeout=60, check=False,
                )
                count = -1  # 精确计数需二次查询，这里只要杀干净
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                subprocess.run(["pkill", "-f", "batch_coordinator.py"], capture_output=True, check=False)
                subprocess.run(["pkill", "-f", "amazon_us_worker.py"], capture_output=True, check=False)
                count = -1
            except OSError:
                pass
        time.sleep(3)
        return count

    def status(self) -> dict[str, Any]:
        """当前协调器状态（不查数据库心跳，只看进程存活）。"""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                self._started_at = None
                return {"status": "offline", "pid": None, "started_at": None}
            return {
                "status": "online",
                "pid": self._proc.pid,
                "started_at": self._started_at,
            }

    def start(self, workers: int | None = None) -> dict[str, Any]:
        """幂等启动：已运行则返回当前状态，否则 spawn 协调器。

        workers：并发 worker 数（持久化到 state/coordinator_workers.json）。
        spawn 必须带 --worker-config（类人类节奏）+ 代理凭证环境变量，
        否则 worker 会以默认配置直连裸跑（此前旧实现缺这两项，属隐患）。
        """
        with self._lock:
            if workers is None:
                workers = self.workers_setting()
            self._save_workers_setting(workers)
            if self._proc is not None and self._proc.poll() is None:
                return self.status()
            env = os.environ.copy()
            env["AMAZON_US_POSTGRES_DSN"] = self.dsn
            env.update(self._proxy_env())
            log_file = open(COORDINATOR_LOG, "a", encoding="utf-8")
            cmd = [
                sys.executable,
                str(COORDINATOR_SCRIPT),
                "run",
                "--tenant-id", self.tenant_id or "amazon_us_local",
                "--workers-per-batch", str(max(1, int(workers))),
                "--worker-config", str(self._worker_config()),
            ]
            creationflags = 0
            if sys.platform == "win32":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            self._proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(ROOT),
                creationflags=creationflags,
            )
            self._started_at = datetime.now(timezone.utc)
            return {"status": "online", "pid": self._proc.pid, "started_at": self._started_at}

    def restart(self, workers: int) -> dict[str, Any]:
        """带 worker 数重启：先杀全部协调器/worker（含外部启动的），再以新参数拉起。

        批次数据都在 Postgres，被中断的在途任务靠租约超时自动回收重爬。
        注意：不持锁调 start()（start 内部自己拿锁；threading.Lock 不可重入）。
        """
        with self._lock:
            self._proc = None
            self._started_at = None
        self.kill_all_crawler_processes()
        return self.start(workers)

    def stop(self) -> dict[str, Any]:
        """停止协调器：发停止信号（数据库 request_stop + 进程 terminate）。"""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = None
                self._started_at = None
                return {"status": "offline", "pid": None}
            # 先尝试优雅终止（协调器有 CTRL_BREAK/SIGINT 处理）
            if sys.platform == "win32":
                self._proc.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(subprocess.signal.SIGINT)
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
            self._proc = None
            self._started_at = None
            return {"status": "offline", "pid": None}

    def recent_log(self, n: int = 50) -> list[str]:
        """读取日志文件最后 n 行（供前端查看协调器最近输出）。"""
        try:
            text = COORDINATOR_LOG.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines = text.splitlines()
        return lines[-n:] if len(lines) > n else lines


# ---------------------------------------------------------------------------
# CSV 清单解析
# ---------------------------------------------------------------------------

def parse_manifest_csv(data: bytes) -> list[dict[str, str]]:
    """解析 CSV 清单文件，返回 [{asin, url}, ...]。

    支持：BOM 头、逗号分隔、双引号转义、首行标题跳过、空行跳过。
    每行必须包含 ASIN（10 位字母数字）和 URL（非空）。
    """
    # 处理 BOM + 解码
    text = data.lstrip(b"\xef\xbb\xbf").decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    rows: list[dict[str, str]] = []
    header_skipped = False
    for lineno, raw in enumerate(reader, start=1):
        # 空行跳过
        if not raw or all(not str(c).strip() for c in raw):
            continue
        cells = [str(c).strip() for c in raw]
        # 首行可能是标题（含 asin/ASIN/Asin 关键字）
        if not header_skipped and any(re.search(r"asin", c, re.IGNORECASE) for c in cells):
            header_skipped = True
            continue
        header_skipped = True  # 之后的行不再检查标题
        # 找 ASIN 和 URL 列（按内容匹配，不按列位置）
        asin = ""
        url = ""
        for c in cells:
            if ASIN_RE.fullmatch(c):
                asin = c
            elif c.startswith(("http://", "https://")):
                url = c
        if not asin:
            raise ValueError(f"第 {lineno} 行缺少有效的 ASIN（10 位字母数字）")
        if not url:
            # 自动补全亚马逊 URL
            url = f"https://www.amazon.com/dp/{asin}"
        rows.append({"asin": asin, "url": url})
    if not rows:
        raise ValueError("清单为空：没有有效的 ASIN 行")
    return rows


def parse_manifest_excel(data: bytes) -> list[dict[str, str]]:
    """解析 Excel(.xlsx) 清单文件，返回 [{asin, url}, ...]。

    规则与 CSV 版一致：按内容找 10 位 ASIN，URL 缺省自动补全。
    与 CSV 的差别：无 ASIN 的行（表头、说明、空行）跳过而不报错，
    因为 Excel 清单常带备注列或说明行；全部跳过才算空清单。
    """
    # 延迟导入：未装 openpyxl 时 CSV 路径不受影响
    try:
        from openpyxl import load_workbook
    except ImportError:
        raise ValueError("服务器缺少 openpyxl，无法解析 Excel，请另存为 CSV 上传")
    # 从内存加载工作簿（不落盘），data_only 取公式计算值
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    rows: list[dict[str, str]] = []
    try:
        # 遍历所有工作表，兼容用户把清单放在任意 sheet 的情况
        for ws in wb.worksheets:
            for raw in ws.iter_rows(values_only=True):
                # 单元格转字符串并去掉空白；None（空单元格）跳过
                cells = [str(c).strip() for c in raw if c is not None and str(c).strip()]
                if not cells:
                    continue
                # 按内容匹配 ASIN 和 URL（不依赖列位置）
                asin = ""
                url = ""
                for c in cells:
                    if ASIN_RE.fullmatch(c):
                        asin = c
                    elif c.startswith(("http://", "https://")):
                        url = c
                if not asin:
                    # 表头/说明行：跳过，不算错误
                    continue
                if not url:
                    # 自动补全亚马逊 URL
                    url = f"https://www.amazon.com/dp/{asin}"
                rows.append({"asin": asin, "url": url})
    finally:
        wb.close()
    if not rows:
        raise ValueError("Excel 清单为空：没有有效的 ASIN")
    return rows


def _rows_from_asin_payload(body: Mapping[str, Any]) -> list[dict[str, str]]:
    """把 Agent 的 JSON 载荷 {"asins": [...]} 转成清单行。

    校验规则与文件上传一致：10 位大写字母数字、自动补全 URL；
    同时保留上传路径的同批去重体验（重复 ASIN 只留一个）。
    """
    if not isinstance(body, dict):
        raise ValueError("请求体必须是 JSON 对象")
    # 关键词排名：明确拒绝（不是忽略字段，让调用方第一时间知道不支持，
    # 避免误以为会执行；支持的采集范围=商品详情+评论+BSR）
    if body.get("keywords") or body.get("keyword_ranking") or body.get("search_terms"):
        raise ValueError("unsupported_feature:关键词排名采集暂不支持，本接口仅支持 ASIN 商品详情/评论/BSR")
    raw_asins = body.get("asins")
    if not isinstance(raw_asins, list) or not raw_asins:
        raise ValueError("asins 必须是非空数组")
    if len(raw_asins) > 5800:
        raise ValueError("单批次最多 5800 个 ASIN")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for value in raw_asins:
        asin = str(value).strip().upper()
        if not ASIN_RE.fullmatch(asin):
            raise ValueError(f"无效 ASIN：{value}")
        if asin in seen:
            continue
        seen.add(asin)
        # URL 可选：body 里带 urls 数组则用，否则自动补全
        rows.append({"asin": asin, "url": f"https://www.amazon.com/dp/{asin}"})
    if not rows:
        raise ValueError("asins 为空")
    # 可选：显式 URL 数组（与 asins 一一对应）
    urls = body.get("urls")
    if isinstance(urls, list):
        if len(urls) != len(rows):
            raise ValueError("urls 数组长度必须与 asins 一致")
        for row, url in zip(rows, urls):
            u = str(url).strip()
            if u:
                if not u.startswith(("http://", "https://")):
                    raise ValueError(f"无效 URL：{u}")
                row["url"] = u
    return rows


# ---------------------------------------------------------------------------
# Console Repository 扩展：支持写操作
# ---------------------------------------------------------------------------


class PostgresConsoleRepository:
    """Purpose-built, read-only query surface for the local operations UI."""

    def __init__(self, dsn: str, tenant_id: str | None = None):
        if not dsn.strip():
            raise ValueError("PostgreSQL DSN must not be empty")
        self.dsn = dsn
        self.tenant_id = (tenant_id or "").strip()
        if self.tenant_id and not TENANT_RE.fullmatch(self.tenant_id):
            raise ValueError("invalid tenant_id")

    def for_tenant(self, tenant_id: str):
        tenant_id = str(tenant_id or "").strip()
        if not TENANT_RE.fullmatch(tenant_id):
            return None
        return type(self)(self.dsn, tenant_id)

    def _require_tenant(self) -> str:
        if not self.tenant_id:
            raise ValueError("tenant selection is required")
        return self.tenant_id

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL console requires optional dependency psycopg") from exc
        return psycopg.connect(
            self.dsn,
            row_factory=dict_row,
            options="-c default_transaction_read_only=on",
        )

    @staticmethod
    def _counts(cursor, sql: str, params: tuple[Any, ...]) -> dict[str, int]:
        cursor.execute(sql, params)
        return {str(row["key"]): int(row["count"]) for row in cursor.fetchall()}

    def list_tenants(self) -> list[dict[str, Any]]:
        state: dict[str, dict[str, Any]] = {}
        run_timings: dict[str, list[dict[str, Any]]] = {}
        operation_tenants: set[str] = set()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT tenant_id,status,COUNT(*) AS count FROM amazon_us.item_state "
                "GROUP BY tenant_id,status ORDER BY tenant_id,status"
            )
            for row in cursor.fetchall():
                item = state.setdefault(str(row["tenant_id"]), {"status_counts": {}, "requested": 0})
                count = int(row["count"])
                item["status_counts"][str(row["status"])] = count
                item["requested"] += count
            cursor.execute(
                "SELECT tenant_id,COUNT(*) AS count FROM amazon_us.product_latest GROUP BY tenant_id"
            )
            product_counts = {str(row["tenant_id"]): int(row["count"]) for row in cursor.fetchall()}
            cursor.execute(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (tenant_id,marketplace,asin,subject_type)
                         tenant_id,asin,error_code,block_reason,context_json,retrieved_at
                  FROM amazon_us.collection_evidence
                  ORDER BY tenant_id,marketplace,asin,subject_type,id DESC
                ), classified AS (
                  SELECT *,COALESCE((
                      error_code='variant_redirect'
                      OR (error_code='asin_mismatch'
                      AND context_json->'identity'->>'requested_asin'=asin
                      AND context_json->'identity'->>'observed_asin'<>asin
                      AND context_json->'identity'->>'canonical_asin'=context_json->'identity'->>'observed_asin'
                      AND COALESCE(context_json->'identity'->>'parent_asin','')<>''
                      AND jsonb_typeof(context_json->'identity'->'child_asins')='array'
                      AND (context_json->'identity'->'child_asins') ? (context_json->'identity'->>'requested_asin')
                      AND (context_json->'identity'->'child_asins') ? (context_json->'identity'->>'observed_asin'))
                    ),FALSE) AS is_variant
                  FROM latest
                )
                SELECT tenant_id,COUNT(*) AS recorded,MAX(retrieved_at) AS latest_at,
                       COUNT(*) FILTER (WHERE block_reason IS NOT NULL) AS blocked,
                       COUNT(*) FILTER (WHERE block_reason IS NULL AND is_variant) AS variant_redirect,
                       COUNT(*) FILTER (WHERE block_reason IS NULL AND error_code IS NOT NULL AND NOT is_variant) AS failed
                FROM classified GROUP BY tenant_id
                """
            )
            outcome_counts = {str(row["tenant_id"]): dict(row) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT tenant_id,COUNT(*) AS evidence_actions,COUNT(transfer_bytes) AS known_transfer_records,
                       COALESCE(SUM(transfer_bytes),0) AS known_transfer_bytes,
                       MIN(retrieved_at) AS started_at,MAX(retrieved_at) AS ended_at,
                       COUNT(*) FILTER (WHERE source_type='http_html') AS http_actions,
                       COUNT(*) FILTER (WHERE source_type='selenium_dom') AS firefox_actions
                FROM amazon_us.collection_evidence GROUP BY tenant_id
                """
            )
            traffic = {str(row["tenant_id"]): dict(row) for row in cursor.fetchall()}
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            ledger_rows: dict[str, dict[str, Any]] = {}
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(
                    """
                    SELECT DISTINCT ON (tenant_id) tenant_id,requested_actions,status,started_at,finished_at
                    FROM amazon_us.collection_run ORDER BY tenant_id,started_at DESC
                    """
                )
                ledger_rows = {str(row["tenant_id"]): dict(row) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT tenant_id,started_at,finished_at,
                           CASE
                             WHEN receipt_json->>'elapsed_seconds' ~ '^[0-9]+([.][0-9]+)?$'
                             THEN LEAST(
                               EXTRACT(EPOCH FROM (finished_at-started_at)),
                               (receipt_json->>'elapsed_seconds')::numeric
                             )
                             ELSE EXTRACT(EPOCH FROM (finished_at-started_at))
                           END AS duration_seconds
                    FROM amazon_us.collection_run WHERE finished_at IS NOT NULL
                    ORDER BY tenant_id,started_at
                    """
                )
                for row in cursor.fetchall():
                    run_timings.setdefault(str(row["tenant_id"]), []).append(dict(row))
            cursor.execute("SELECT to_regclass('amazon_us.operation_run') AS relation")
            if cursor.fetchone()["relation"] is not None:
                cursor.execute("SELECT DISTINCT tenant_id FROM amazon_us.operation_run")
                operation_tenants = {str(row["tenant_id"]) for row in cursor.fetchall()}
        results: list[dict[str, Any]] = []
        tenant_ids = set(state) | set(product_counts) | set(outcome_counts) | set(traffic) | set(ledger_rows) | operation_tenants
        for tenant_id in tenant_ids:
            item = state.get(tenant_id) or {"status_counts": {}, "requested": 0}
            outcomes = outcome_counts.get(tenant_id) or {}
            metrics = traffic.get(tenant_id, {})
            ledger = ledger_rows.get(tenant_id) or {}
            requested = int(item.get("requested") or ledger.get("requested_actions") or 0)
            recorded = int(outcomes.get("recorded") or 0)
            running = int((item.get("status_counts") or {}).get("running") or 0)
            started_at = metrics.get("started_at") or ledger.get("started_at")
            ended_at = metrics.get("ended_at") or ledger.get("finished_at") or ledger.get("started_at")
            durations = project_batch_durations(run_timings.get(tenant_id) or [])
            evidence_wall_span = (
                round((metrics.get("ended_at") - metrics.get("started_at")).total_seconds(), 2)
                if metrics.get("started_at") is not None and metrics.get("ended_at") is not None else None
            )
            if evidence_wall_span is not None:
                durations["wall_span_seconds"] = evidence_wall_span
                durations["wall_span_includes_idle"] = True
            terminal_status = (
                "running" if running or ledger.get("status") == "running"
                else str(ledger.get("status")) if not item.get("requested") and ledger.get("status")
                else "complete" if requested and recorded >= requested
                else "partial" if recorded else "pending"
            )
            results.append({
                "tenant_id": tenant_id,
                "requested": requested,
                "recorded": recorded,
                "product_succeeded": int(product_counts.get(tenant_id, 0)),
                "variant_redirect": int(outcomes.get("variant_redirect") or 0),
                "failed": int(outcomes.get("failed") or 0),
                "blocked": int(outcomes.get("blocked") or 0),
                "pending": max(0, requested - recorded),
                "running": running,
                "evidence_actions": int(metrics.get("evidence_actions") or 0),
                "known_transfer_bytes": int(metrics.get("known_transfer_bytes") or 0),
                "unknown_transfer_records": int(metrics.get("evidence_actions") or 0) - int(metrics.get("known_transfer_records") or 0),
                "http_actions": int(metrics.get("http_actions") or 0),
                "firefox_actions": int(metrics.get("firefox_actions") or 0),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": durations["active_duration_seconds"],
                **durations,
                "terminal_status": terminal_status,
            })
        results.sort(key=lambda row: (row.get("ended_at") is not None, row.get("ended_at") or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return results

    def load_overview(self, raw_html_dir: Path | None = None) -> dict[str, Any]:
        tenant_id = self._require_tenant()
        with self._connect() as conn, conn.cursor() as cursor:
            params = (tenant_id,)
            status_counts = self._counts(
                cursor,
                "SELECT status AS key,COUNT(*) AS count FROM amazon_us.item_state "
                "WHERE tenant_id=%s GROUP BY status ORDER BY status",
                params,
            )
            stage_counts = self._counts(
                cursor,
                "SELECT task_stage AS key,COUNT(*) AS count FROM amazon_us.item_state "
                "WHERE tenant_id=%s GROUP BY task_stage ORDER BY task_stage",
                params,
            )
            source_counts = self._counts(
                cursor,
                "SELECT COALESCE(source_type,'unknown') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY source_type ORDER BY source_type",
                params,
            )
            error_counts = self._counts(
                cursor,
                "SELECT COALESCE(error_code,'none') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY error_code ORDER BY error_code",
                params,
            )
            block_counts = self._counts(
                cursor,
                "SELECT COALESCE(block_reason,'none') AS key,COUNT(*) AS count "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY block_reason ORDER BY block_reason",
                params,
            )
            cursor.execute(
                "SELECT COUNT(*) AS total FROM amazon_us.item_state WHERE tenant_id=%s",
                params,
            )
            total = int(cursor.fetchone()["total"])
            cursor.execute(
                "SELECT COUNT(DISTINCT asin) AS touched,COUNT(*) AS actions,COUNT(transfer_bytes) AS known_transfer_records,"
                "COALESCE(SUM(transfer_bytes),0) AS transfer_bytes,MAX(retrieved_at) AS last_evidence_at "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s",
                params,
            )
            evidence = dict(cursor.fetchone())
            cursor.execute(
                "SELECT source_type,transfer_bytes,context_json FROM amazon_us.collection_evidence WHERE tenant_id=%s",
                params,
            )
            evidence_rows = [dict(row) for row in cursor.fetchall()]
            traffic_summary = summarize_traffic(evidence_rows)
            context_quality_counts = summarize_context_quality(evidence_rows)
            table_counts: dict[str, int] = {}
            for name, table in (
                ("products", "product_latest"),
                ("media", "media_asset"),
                ("content", "content_module"),
                ("review_summaries", "review_summary"),
                ("reviews", "review_record"),
            ):
                cursor.execute(f"SELECT COUNT(*) AS count FROM amazon_us.{table} WHERE tenant_id=%s", params)
                table_counts[name] = int(cursor.fetchone()["count"])
            cursor.execute(
                "SELECT asin,task_stage,lease_owner,lease_expires_at FROM amazon_us.item_state "
                "WHERE tenant_id=%s AND status='running' ORDER BY asin LIMIT 50",
                params,
            )
            running = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT run_id,COUNT(*) AS actions,MIN(retrieved_at) AS started_at,MAX(retrieved_at) AS ended_at,"
                "COUNT(*) FILTER (WHERE block_reason IS NOT NULL) AS blocked,"
                "COUNT(*) FILTER (WHERE context_json->>'context_quality'='partial') AS partial "
                "FROM amazon_us.collection_evidence WHERE tenant_id=%s GROUP BY run_id "
                "ORDER BY MAX(retrieved_at) DESC LIMIT 10",
                params,
            )
            recent_runs = [dict(row) for row in cursor.fetchall()]

        files = [*raw_html_dir.rglob("*.html"), *raw_html_dir.rglob("*.html.gz")] if raw_html_dir and raw_html_dir.exists() else None
        raw_bytes = sum(path.stat().st_size for path in files) if files is not None else None
        touched = int(evidence.get("touched") or 0)
        actions = int(evidence.get("actions") or 0)
        known_transfers = int(evidence.get("known_transfer_records") or 0)
        database_rows = sum(table_counts.values())
        return {
            "schema_version": "amazon-us-console-v1",
            "tenant_id": tenant_id,
            "observed_at": datetime.now(timezone.utc),
            "status_counts": status_counts,
            "stage_counts": stage_counts,
            "source_counts": source_counts,
            "context_quality_counts": context_quality_counts,
            "error_counts": error_counts,
            "block_counts": block_counts,
            "progress": {
                "total": total,
                "touched": touched,
                "percent": round(touched / total * 100, 2) if total else 0,
                "successful_products": table_counts["products"],
            },
            "table_counts": table_counts,
            "running": running,
            "recent_runs": recent_runs,
            "four_scale_metrics": {
                "page_actions": actions,
                "successful_asins": table_counts["products"],
                "database_rows": database_rows,
                "field_values": None,
                "field_values_reason": "business definition required",
            },
            "traffic": {
                "raw_html_files": len(files) if files is not None else None,
                "saved_raw_html_bytes": raw_bytes,
                "saved_raw_html_reason": None if files is not None else "not_aggregated_across_output_directories",
                "known_http_transfer_bytes": int(evidence.get("transfer_bytes") or 0),
                "unknown_transfer_records": actions - known_transfers,
                "proxy_billed_bytes": None,
                **traffic_summary,
            },
            "last_evidence_at": evidence.get("last_evidence_at"),
        }

    def load_identity(self) -> dict[str, Any]:
        if not self.tenant_id:
            tenants = self.list_tenants()
            return {
                "tenant_count": len(tenants),
                "default_tenant_id": tenants[0]["tenant_id"] if tenants else None,
            }
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM amazon_us.item_state WHERE tenant_id=%s",
                (self.tenant_id,),
            )
            task_count = int(cursor.fetchone()["count"])
        return {"tenant_id": self.tenant_id, "task_count": task_count}

    def list_items(
        self,
        *,
        status: str | None = None,
        stage: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        where = ["s.tenant_id=%s"]
        params: list[Any] = [tenant_id]
        if status:
            where.append("s.status=%s")
            params.append(status)
        if stage:
            where.append("s.task_stage=%s")
            params.append(stage)
        if query:
            term = f"%{query.strip()}%"
            where.append("(s.asin ILIKE %s OR COALESCE(p.title,'') ILIKE %s OR COALESCE(s.last_error,'') ILIKE %s)")
            params.extend([term, term, term])
        predicate = " AND ".join(where)
        join = (
            "LEFT JOIN amazon_us.product_latest p ON p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace "
            "AND p.asin=s.asin AND p.subject_type=s.subject_type "
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) AS count FROM amazon_us.item_state s {join} WHERE {predicate}", tuple(params))
            total = int(cursor.fetchone()["count"])
            cursor.execute(
                f"""
                SELECT s.asin,s.subject_type,s.status,s.task_stage,s.attempts,s.max_attempts,s.last_error,
                       s.block_reason,s.updated_at,s.lease_owner,s.lease_expires_at,
                       p.title,p.price,p.availability,p.buy_box,p.rating,p.reported_review_count,
                       e.source_type,e.http_status,e.error_code AS evidence_error,e.block_reason AS evidence_block,
                       e.context_json,e.raw_html_path,e.retrieved_at
                FROM amazon_us.item_state s
                {join}
                LEFT JOIN LATERAL (
                  SELECT source_type,http_status,error_code,block_reason,context_json,raw_html_path,retrieved_at
                  FROM amazon_us.collection_evidence ce
                  WHERE ce.tenant_id=s.tenant_id AND ce.marketplace=s.marketplace AND ce.asin=s.asin
                    AND ce.subject_type=s.subject_type
                  ORDER BY ce.id DESC LIMIT 1
                ) e ON TRUE
                WHERE {predicate}
                ORDER BY CASE s.status WHEN 'blocked' THEN 0 WHEN 'failed' THEN 1 WHEN 'running' THEN 2 ELSE 3 END,
                         s.updated_at DESC,s.asin
                LIMIT %s OFFSET %s
                """,
                tuple([*params, limit, offset]),
            )
            items = [dict(row) for row in cursor.fetchall()]
        for item in items:
            item["price_status"] = project_price_status(item)
            item["outcome"] = classify_evidence_outcome({
                **item,
                "error_code": item.get("evidence_error"),
                "block_reason": item.get("evidence_block") or item.get("block_reason"),
            })
        return {"tenant_id": tenant_id, "total": total, "limit": limit, "offset": offset, "items": items}

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                WITH recent AS (
                  SELECT run_id,MAX(retrieved_at) AS ended_at
                  FROM amazon_us.collection_evidence WHERE tenant_id=%s
                  GROUP BY run_id ORDER BY MAX(retrieved_at) DESC LIMIT %s
                )
                SELECT e.run_id,e.asin,e.source_type,e.transfer_bytes,e.retrieved_at,e.error_code,e.block_reason,
                       e.context_json,e.raw_html_path
                FROM amazon_us.collection_evidence e JOIN recent r ON r.run_id=e.run_id
                WHERE e.tenant_id=%s ORDER BY r.ended_at DESC,e.id
                """,
                (tenant_id, limit, tenant_id),
            )
            evidence_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            has_ledger = cursor.fetchone()["relation"] is not None
            ledger_rows: dict[str, dict[str, Any]] = {}
            if has_ledger:
                cursor.execute(
                    "SELECT * FROM amazon_us.collection_run WHERE tenant_id=%s ORDER BY started_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
                ledger_rows = {str(row["run_id"]): dict(row) for row in cursor.fetchall()}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in evidence_rows:
            grouped.setdefault(str(row["run_id"]), []).append(row)
        for run_id in ledger_rows:
            grouped.setdefault(run_id, [])
        results: list[dict[str, Any]] = []
        for run_id, rows in grouped.items():
            ledger = ledger_rows.get(run_id) or {}
            outcomes = {"completed": 0, "variant_redirect": 0, "failed": 0, "blocked": 0}
            for row in rows:
                outcomes[classify_evidence_outcome(row)] += 1
            observed_start = min((row["retrieved_at"] for row in rows), default=None)
            observed_end = max((row["retrieved_at"] for row in rows), default=None)
            started_at = ledger.get("started_at") or observed_start
            ended_at = ledger.get("finished_at") or observed_end
            requested = int(ledger.get("requested_actions") or len(rows))
            recorded = len(rows)
            status = str(ledger.get("status") or ("legacy_blocked" if outcomes["blocked"] else "legacy_complete"))
            duration_projection = project_run_durations(ledger, observed_start, observed_end)
            duration_seconds = duration_projection["worker_duration_seconds"]
            started_at = duration_projection.pop("effective_started_at")
            ended_at = duration_projection.pop("effective_finished_at")
            results.append({
                "run_id": run_id,
                "command": ledger.get("command") or "legacy",
                "requested_actions": requested,
                "recorded_actions": recorded,
                "evidence_actions": recorded,
                "unique_asins": len({row["asin"] for row in rows}),
                "product_succeeded": outcomes["completed"],
                "variant_redirect": outcomes["variant_redirect"],
                "failed": outcomes["failed"],
                "blocked": outcomes["blocked"],
                "pending": max(0, requested - recorded),
                "running": max(0, requested - recorded) if status == "running" else 0,
                "known_transfer_bytes": sum(int(row.get("transfer_bytes") or 0) for row in rows),
                "http_actions": sum(row.get("source_type") == "http_html" for row in rows),
                "firefox_actions": sum(row.get("source_type") == "selenium_dom" for row in rows),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                **duration_projection,
                "terminal_status": status,
                "termination_reason": ledger.get("termination_reason"),
            })
        results.sort(key=lambda row: row.get("started_at") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return results[:limit]

    # ------------------------------------------------------------------
    # 写操作：创建批次 / 请求停止（委托给 BatchStore）
    # ------------------------------------------------------------------

    def create_batch(
        self,
        rows: list[dict[str, str]],
        *,
        idempotency_key: str = "",
        uploaded_by: str = "console",
        collect_reviews: bool = True,
    ) -> dict[str, Any]:
        """从清单行创建批次（委托给 BatchStore.create_batch）。

        idempotency_key：Agent 提交时的幂等键，同键 24h 内重试返回同一批次。
        uploaded_by：来源标记（console 网页 / agent 接口），便于区分提交渠道。
        collect_reviews：评论采集开关，False 时商品完成即收尾不采评论。
        """
        from batch_store import BatchStore, BatchStoreError

        tenant_id = self._require_tenant()
        store = BatchStore(self.dsn, tenant_id, "own")
        try:
            result = store.create_batch(
                rows, uploaded_by=uploaded_by, idempotency_key=idempotency_key,
                collect_reviews=collect_reviews,
            )
        except BatchStoreError as exc:
            raise ValueError(str(exc)) from exc
        # 幂等重放（同键已有批次）时不再重复 start
        if result.get("idempotent_replay"):
            return result
        # 自动 start_batch（pending → running），协调器下次轮询就会 pick up
        store.start_batch(result["batch_id"])
        return result

    def request_stop(self, batch_id: str) -> dict[str, Any]:
        """请求停止指定批次（委托给 BatchStore.request_stop）。"""
        from batch_store import BatchStore

        tenant_id = self._require_tenant()
        store = BatchStore(self.dsn, tenant_id, "own")
        # 先查批次是否存在
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM amazon_us.batch WHERE batch_id=%s AND tenant_id=%s",
                (batch_id, tenant_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise KeyError(f"batch_not_found:{batch_id}")
        if row["status"] in ("completed", "failed", "blocked", "stopped"):
            raise ValueError(f"批次已终态（{row['status']}），无法停止")
        ok = store.request_stop(batch_id)
        return {"batch_id": batch_id, "stop_requested": ok}

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------

    def list_batches(self, limit: int = 20) -> list[dict[str, Any]]:
        """批次列表：进度全部来自 batch_progress 视图（唯一推导处）。"""
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 100))
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT v.batch_id,v.batch_status,v.stop_requested,v.requested_count,
                       v.succeeded,v.running,v.pending,v.blocked,v.failed_fetch,v.failed_system,
                       v.failed_total,v.cancelled,v.evidence_count,v.evidence_bytes,
                       v.created_at,v.started_at,v.finalized_at,
                       b.collect_reviews
                FROM amazon_us.batch_progress v
                JOIN amazon_us.batch b ON b.batch_id=v.batch_id
                WHERE v.tenant_id=%s
                ORDER BY v.created_at DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def load_coordinator_status(self) -> dict[str, Any] | None:
        """协调器在线状态：30 秒内有心跳算在线（一个部署只有一个协调器）。"""
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT coordinator_id::text AS coordinator_id,host,pid,status,heartbeat_at
                FROM amazon_us.coordinator_state
                WHERE status='running'
                  AND heartbeat_at > CURRENT_TIMESTAMP - INTERVAL '30 seconds'
                ORDER BY heartbeat_at DESC LIMIT 1
                """
            )
            row = cursor.fetchone()
            return dict(row) if row else None

    def load_batch(self, batch_id: str) -> dict[str, Any] | None:
        """单个批次进度 + 协调器在线状态（30 秒内有心跳算在线）。"""
        tenant_id = self._require_tenant()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT batch_id,batch_status,stop_requested,requested_count,
                       succeeded,running,pending,blocked,failed_fetch,failed_system,
                       failed_total,cancelled,evidence_count,evidence_bytes,
                       created_at,started_at,finalized_at,final_failure_class
                FROM amazon_us.batch_progress
                WHERE tenant_id=%s AND batch_id=%s
                """,
                (tenant_id, batch_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            progress = dict(row)
        progress["coordinator"] = self.load_coordinator_status()
        return progress

    def list_batch_items(
        self,
        batch_id: str,
        *,
        status: str | None = None,
        query: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any] | None:
        """批次成员列表：只看本批次 batch_item，商品信息取 own 优先的快照。"""
        tenant_id = self._require_tenant()
        # 上限 6000：覆盖 5800 量级清单一次看全（验收要求上传后立即显示全部待采集商品）
        limit = max(1, min(int(limit), 6000))
        offset = max(0, int(offset))
        where = ["i.batch_id=%s", "i.tenant_id=%s"]
        params: list[Any] = [batch_id, tenant_id]
        if status:
            where.append("i.status=%s")
            params.append(status)
        if query:
            term = f"%{query.strip()}%"
            where.append("(i.asin ILIKE %s OR COALESCE(p.title,'') ILIKE %s OR COALESCE(i.last_error,'') ILIKE %s)")
            params.extend([term, term, term])
        predicate = " AND ".join(where)
        # 批次成员没有 subject_type，商品快照按 own 优先取一行
        # collected_at 一并带出：作为"库内快照"展示（缓存复用提示——
        # 成员未跑但库里有近期快照 → 前端标"库内已有"，用户可自行决定是否需要重采）
        join = (
            "LEFT JOIN LATERAL ("
            "  SELECT title,price,availability,rating,collected_at FROM amazon_us.product_latest p"
            "  WHERE p.tenant_id=i.tenant_id AND p.marketplace=i.marketplace AND p.asin=i.asin"
            "  ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END"
            "  LIMIT 1"
            ") p ON TRUE "
        )
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM amazon_us.batch WHERE batch_id=%s AND tenant_id=%s",
                (batch_id, tenant_id),
            )
            if cursor.fetchone() is None:
                return None
            cursor.execute(
                f"SELECT COUNT(*) AS count FROM amazon_us.batch_item i {join} WHERE {predicate}",
                tuple(params),
            )
            total = int(cursor.fetchone()["count"])
            cursor.execute(
                f"""
                SELECT i.asin,i.url,i.status,i.task_stage,i.attempts,i.max_attempts,
                       i.error_class,i.block_reason,i.last_error,i.variant_asin,
                       i.fetched_review_count,i.review_pages_fetched,i.reported_review_count,
                       i.lease_owner,i.updated_at,i.finalized_at,
                       p.title,p.price,p.availability,p.rating,
                       p.collected_at AS snapshot_at
                FROM amazon_us.batch_item i
                {join}
                WHERE {predicate}
                ORDER BY CASE i.status WHEN 'running' THEN 0 WHEN 'failed' THEN 1
                                       WHEN 'blocked' THEN 2 ELSE 3 END,
                         i.updated_at DESC,i.asin
                LIMIT %s OFFSET %s
                """,
                tuple([*params, limit, offset]),
            )
            items = [dict(row) for row in cursor.fetchall()]
        return {"batch_id": batch_id, "total": total, "limit": limit, "offset": offset, "items": items}

    def export_batch(self, batch_id: str) -> bytes | None:
        """导出批次结果为 CSV 字节（UTF-8 带 BOM，Excel 直接打开不乱码）。

        列覆盖验收要求的商品字段：身份、标题、品牌、价格、库存、评分及数量，
        以及批次侧状态、错误类别、拦截原因、采集时间。批次不存在返回 None。
        """
        tenant_id = self._require_tenant()
        # 商品快照按 own 优先取一行（与成员列表口径一致）
        join = (
            "LEFT JOIN LATERAL ("
            "  SELECT title,brand,price,availability,rating,reported_rating_count,"
            "         reported_review_count,canonical_url,collected_at,"
            "         bsr_rank,bsr_category"
            "  FROM amazon_us.product_latest p"
            "  WHERE p.tenant_id=i.tenant_id AND p.marketplace=i.marketplace AND p.asin=i.asin"
            "  ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END"
            "  LIMIT 1"
            ") p ON TRUE "
        )
        with self._connect() as conn, conn.cursor() as cursor:
            # 校验批次归属当前租户
            cursor.execute(
                "SELECT 1 FROM amazon_us.batch WHERE batch_id=%s AND tenant_id=%s",
                (batch_id, tenant_id),
            )
            if cursor.fetchone() is None:
                return None
            cursor.execute(
                f"""
                SELECT i.asin,i.url,i.status,i.attempts,i.error_class,i.block_reason,
                       i.last_error,i.updated_at,i.variant_asin,
                       p.title,p.brand,p.price,p.availability,p.rating,
                       p.reported_rating_count,p.reported_review_count,
                       p.canonical_url,p.collected_at,
                       p.bsr_rank,p.bsr_category
                FROM amazon_us.batch_item i
                {join}
                WHERE i.batch_id=%s AND i.tenant_id=%s
                ORDER BY i.asin
                """,
                (batch_id, tenant_id),
            )
            items = [dict(row) for row in cursor.fetchall()]
        # 写 CSV：中文表头，时间转字符串
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "ASIN", "采集链接", "状态", "变体跳转ASIN", "标题", "品牌", "价格",
            "可售状态", "评分", "评分数", "评论数",
            "BSR排名", "BSR类目",
            "尝试次数", "错误类别", "拦截原因", "错误信息",
            "商品页地址", "采集时间", "更新时间",
        ])
        for item in items:
            writer.writerow([
                item.get("asin"),
                item.get("url"),
                item.get("status"),
                item.get("variant_asin") or "",
                item.get("title"),
                item.get("brand"),
                item.get("price"),
                item.get("availability"),
                item.get("rating"),
                item.get("reported_rating_count"),
                item.get("reported_review_count"),
                item.get("bsr_rank") if item.get("bsr_rank") is not None else "",
                item.get("bsr_category") or "",
                item.get("attempts"),
                item.get("error_class"),
                item.get("block_reason"),
                item.get("last_error"),
                item.get("canonical_url"),
                _json_default(item.get("collected_at")) if item.get("collected_at") else "",
                _json_default(item.get("updated_at")) if item.get("updated_at") else "",
            ])
        # utf-8-sig：带 BOM，Excel 双击打开中文不乱码
        return buf.getvalue().encode("utf-8-sig")

    def list_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        tenant_id = self._require_tenant()
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass('amazon_us.operation_run') AS relation")
            if cursor.fetchone()["relation"] is None:
                return []
            cursor.execute(
                """
                SELECT operation_id,tenant_id,operation_type,status,preflight_status,preflight_duration_ms,
                       failure_stage,error_class,egress_id,collection_run_id,http_status,response_bytes,
                       probe_elapsed_ms,started_at,finished_at,duration_ms
                FROM amazon_us.operation_run
                WHERE tenant_id=%s ORDER BY started_at DESC LIMIT %s
                """,
                (tenant_id, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
        for row in rows:
            row["duration_seconds"] = round(float(row.get("duration_ms") or 0) / 1000, 2) if row.get("duration_ms") is not None else None
        return rows

    def load_run(self, run_id: str) -> dict[str, Any] | None:
        tenant_id = self._require_tenant()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT e.id,e.run_id,e.asin,e.subject_type,e.url,e.http_status,e.transfer_bytes,e.context_json,e.retrieved_at,
                       e.source_type,e.block_reason,e.error_code,e.raw_html_path,e.content_hash,
                       s.status AS current_status,s.task_stage,s.last_error,s.updated_at,
                       p.title,p.price,p.availability,p.buy_box
                FROM amazon_us.collection_evidence e
                LEFT JOIN amazon_us.item_state s ON s.tenant_id=e.tenant_id AND s.marketplace=e.marketplace
                  AND s.asin=e.asin AND s.subject_type=e.subject_type
                LEFT JOIN amazon_us.product_latest p ON p.tenant_id=e.tenant_id AND p.marketplace=e.marketplace
                  AND p.asin=e.asin AND p.subject_type=e.subject_type
                WHERE e.tenant_id=%s AND e.run_id=%s ORDER BY e.id
                """,
                (tenant_id, run_id),
            )
            evidence_rows = [dict(row) for row in cursor.fetchall()]
            cursor.execute("SELECT to_regclass('amazon_us.collection_run') AS relation")
            ledger = None
            if cursor.fetchone()["relation"] is not None:
                cursor.execute(
                    "SELECT * FROM amazon_us.collection_run WHERE tenant_id=%s AND run_id=%s",
                    (tenant_id, run_id),
                )
                ledger_row = cursor.fetchone()
                ledger = dict(ledger_row) if ledger_row is not None else None
            if not evidence_rows and ledger is None:
                return None
            if not evidence_rows:
                duration_projection = project_run_durations(ledger)
                effective_started_at = duration_projection.pop("effective_started_at")
                effective_finished_at = duration_projection.pop("effective_finished_at")
                return {
                    "schema_version": "amazon-us-console-v2",
                    "tenant_id": tenant_id,
                    "run_id": run_id,
                    "requested_actions": int(ledger.get("requested_actions") or 0),
                    "recorded_actions": 0,
                    "inferred_actions": 0,
                    "terminal_status": ledger.get("status"),
                    "started_at": effective_started_at,
                    "ended_at": effective_finished_at,
                    **duration_projection,
                    "termination_reason": ledger.get("termination_reason"),
                    "items": [],
                }
            started_at = min(row["retrieved_at"] for row in evidence_rows)
            ended_at = max(row["retrieved_at"] for row in evidence_rows)
            evidence_asins = {row["asin"] for row in evidence_rows}
            items: list[dict[str, Any]] = []
            for row in evidence_rows:
                outcome = classify_evidence_outcome(row)
                quality = str(_context_value(row).get("context_quality") or "unknown")
                items.append({
                    **row,
                    "context_quality": quality,
                    "outcome": outcome,
                    "price_status": project_price_status(row),
                    "attribution": "evidence",
                })
            cursor.execute(
                """
                SELECT s.asin,s.subject_type,s.url,s.status AS current_status,s.task_stage,s.last_error,s.block_reason,
                       s.updated_at,p.title,p.price
                FROM amazon_us.item_state s
                LEFT JOIN amazon_us.product_latest p ON p.tenant_id=s.tenant_id AND p.marketplace=s.marketplace
                  AND p.asin=s.asin AND p.subject_type=s.subject_type
                WHERE s.tenant_id=%s AND s.updated_at BETWEEN %s AND %s
                ORDER BY s.updated_at,s.asin
                """,
                (tenant_id, started_at, ended_at),
            )
            for row_value in cursor.fetchall():
                row = dict(row_value)
                if row["asin"] in evidence_asins:
                    continue
                outcome = "blocked" if row.get("block_reason") else "failed" if row.get("last_error") else row.get("current_status")
                items.append({**row, "outcome": outcome, "attribution": "time_window_inference"})
        items.sort(key=lambda item: (item.get("retrieved_at") or item.get("updated_at"), item["asin"]))
        traffic_summary = summarize_traffic(evidence_rows)
        context_quality_counts = summarize_context_quality(evidence_rows)
        duration_projection = project_run_durations(ledger, started_at, ended_at)
        effective_started_at = duration_projection.pop("effective_started_at")
        effective_finished_at = duration_projection.pop("effective_finished_at")
        return {
            "schema_version": "amazon-us-console-v2",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "started_at": effective_started_at,
            "ended_at": effective_finished_at,
            "requested_actions": int((ledger or {}).get("requested_actions") or len(evidence_rows)),
            "recorded_actions": len(evidence_rows),
            "inferred_actions": sum(1 for item in items if item["attribution"] == "time_window_inference"),
            "known_transfer_bytes": sum(int(item.get("transfer_bytes") or 0) for item in evidence_rows),
            "traffic": traffic_summary,
            "context_quality_counts": context_quality_counts,
            "outcome_counts": {
                outcome: sum(item["outcome"] == outcome for item in items)
                for outcome in ("completed", "variant_redirect", "failed", "blocked")
            },
            "items": items,
            "terminal_status": (ledger or {}).get("status") or ("legacy_blocked" if any(item["outcome"] == "blocked" for item in items) else "legacy_complete"),
            "termination_reason": (ledger or {}).get("termination_reason"),
            **duration_projection,
            "warning": "time_window_inference is legacy fallback; new network failures write run evidence",
        }

    def load_detail(self, asin: str) -> dict[str, Any] | None:
        tenant_id = self._require_tenant()
        asin = asin.upper()
        with self._connect() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM amazon_us.item_state WHERE tenant_id=%s AND marketplace='US' AND asin=%s "
                "ORDER BY CASE subject_type WHEN 'own' THEN 0 WHEN 'competitor' THEN 1 ELSE 2 END LIMIT 1",
                (tenant_id, asin),
            )
            state = cursor.fetchone()
            if state is None:
                return None
            task = dict(state)
            subject_type = task["subject_type"]
            identity = (tenant_id, asin, subject_type)
            cursor.execute(
                "SELECT * FROM amazon_us.product_latest WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                identity,
            )
            product_row = cursor.fetchone()
            product = dict(product_row) if product_row is not None else None
            cursor.execute(
                "SELECT placement,entry_type,thumbnail_url,display_url,asset_url,poster_url,ordinal,is_primary,"
                "width,height,alt_text,variant_asin,load_status,failure_reason FROM amazon_us.media_asset "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s ORDER BY ordinal NULLS LAST LIMIT 300",
                identity,
            )
            media = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT module_type,position,order_index,text,image_url,link_url,status FROM amazon_us.content_module "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY position,order_index NULLS LAST LIMIT 300",
                identity,
            )
            content = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT * FROM amazon_us.review_summary WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                identity,
            )
            summary_row = cursor.fetchone()
            review_summary = dict(summary_row) if summary_row is not None else None
            cursor.execute(
                "SELECT review_id,rating,title,body,review_url,review_date,locale,verified,body_truncated,review_images,page "
                "FROM amazon_us.review_record WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY page NULLS LAST,review_id LIMIT 100",
                identity,
            )
            reviews = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT run_id,url,http_status,transfer_bytes,retrieved_at,source_type,content_hash,raw_html_path,"
                "block_reason,parser_version,error_code,context_json FROM amazon_us.collection_evidence "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s ORDER BY id DESC LIMIT 30",
                identity,
            )
            evidence = [dict(row) for row in cursor.fetchall()]
            cursor.execute(
                "SELECT snapshot_id,collected_at,price,availability,rating,review_count,status FROM amazon_us.product_snapshot "
                "WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s "
                "ORDER BY collected_at DESC,snapshot_id DESC LIMIT 30",
                identity,
            )
            history = [dict(row) for row in cursor.fetchall()]
        for row in evidence:
            row["outcome"] = classify_evidence_outcome(row)
        if product is not None:
            product["price_status"] = project_price_status(product)
        return {
            "schema_version": "amazon-us-console-v1",
            "tenant_id": tenant_id,
            "asin": asin,
            "task": task,
            "product": product,
            "media": media,
            "content_modules": content,
            "review_summary": review_summary,
            "reviews": reviews,
            "top_reviews": (product or {}).get("top_reviews") or [],
            "evidence": evidence,
            "history": history,
        }


class ConsoleHandler(BaseHTTPRequestHandler):
    server: "ConsoleServer"

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=_json_default).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _scoped_repository(self, parsed):
        repository = self.server.repository
        if not hasattr(repository, "for_tenant"):
            return repository
        query = parse_qs(parsed.query)
        tenant_id = str(query.get("tenant", [""])[0]).strip()
        if not tenant_id:
            identity = repository.load_identity()
            tenant_id = str(identity.get("default_tenant_id") or "").strip()
        scoped = repository.for_tenant(tenant_id)
        if scoped is None:
            raise ValueError("invalid tenant")
        return scoped

    def _authorized(self, write: bool = False) -> bool:
        """鉴权 + 读写权限区分。

        - 未配置任何 key：本机开发模式，全部放行（保持原行为）
        - 主 key（api_key）：读写全放行
        - 只读 key（read_key）：仅读接口放行；write=True 时返回 403，
          保证只读 Agent 不能启动/停止/建批次
        """
        expected = self.server.api_key
        read_expected = getattr(self.server, "read_key", "")
        if not expected and not read_expected:
            return True
        supplied = self.headers.get("X-Collection-API-Key", "")
        if expected and hmac.compare_digest(supplied, expected):
            return True
        if read_expected and hmac.compare_digest(supplied, read_expected):
            if write:
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"error": "insufficient_permission", "detail": "只读密钥不能执行写操作"},
                )
                return False
            return True
        self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _send_static(self, path: str) -> None:
        name = STATIC_FILES.get(path)
        if name is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        file_path = STATIC_ROOT / name
        try:
            body = file_path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "console_asset_unavailable"})
            return
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        if file_path.suffix == ".js":
            content_type = "text/javascript"
        self._send_bytes(HTTPStatus.OK, body, f"{content_type}; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path in STATIC_FILES:
            self._send_static(path)
            return
        if path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True, "schema_version": "amazon-us-console-v2"})
            return
        if path == "/readyz":
            try:
                identity = self.server.repository.load_identity()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "schema_version": "amazon-us-console-v2",
                        "runtime_fingerprint": RUNTIME_FINGERPRINT,
                        **identity,
                    },
                )
            except Exception:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "database_unavailable"})
            return
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        if not self._authorized():
            return
        try:
            if path == "/api/tenants":
                if hasattr(self.server.repository, "list_tenants"):
                    items = self.server.repository.list_tenants()
                else:
                    identity = self.server.repository.load_identity()
                    items = [{"tenant_id": identity.get("tenant_id"), "requested": identity.get("task_count", 0)}]
                self._send_json(
                    HTTPStatus.OK,
                    {"schema_version": "amazon-us-console-v2", "items": items},
                )
                return
            repository = self._scoped_repository(parsed)
            if path == "/api/overview":
                self._send_json(HTTPStatus.OK, repository.load_overview(self.server.raw_html_dir))
                return
            if path == "/api/runs":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["20"])[0])
                self._send_json(
                    HTTPStatus.OK,
                    {"schema_version": "amazon-us-console-v2", "items": repository.list_runs(limit)},
                )
                return
            if path == "/api/batches":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["20"])[0])
                coord = repository.load_coordinator_status() or {}
                mgr = self.server.coordinator_manager
                # workers 设置值（控制台持久化文件），供 UI 展示可调的 worker 数
                if mgr is not None:
                    coord["workers"] = type(mgr).workers_setting()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "schema_version": "amazon-us-console-v2",
                        "items": repository.list_batches(limit),
                        "coordinator": coord,
                    },
                )
                return
            batch_items_match = BATCH_ITEMS_PATH.fullmatch(path)
            if batch_items_match:
                batch_id = batch_items_match.group(1)
                query = parse_qs(parsed.query)
                status = query.get("status", [""])[0].strip() or None
                term = query.get("q", [""])[0].strip()[:100] or None
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
                if status and not re.fullmatch(r"[a-z_]+", status):
                    raise ValueError("invalid status")
                payload = repository.list_batch_items(
                    batch_id, status=status, query=term, limit=limit, offset=offset
                )
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "batch_not_found", "batch_id": batch_id})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            # ---- GET /api/batches/{id}/export: 下载批次结果 CSV ----
            batch_export_match = BATCH_EXPORT_PATH.fullmatch(path)
            if batch_export_match:
                batch_id = batch_export_match.group(1)
                csv_bytes = repository.export_batch(batch_id)
                if csv_bytes is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "batch_not_found", "batch_id": batch_id})
                    return
                # 以附件形式下发 CSV 文件流
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="batch_{batch_id[:8]}.csv"',
                )
                self.send_header("Content-Length", str(len(csv_bytes)))
                self.end_headers()
                self.wfile.write(csv_bytes)
                return
            batch_match = BATCH_PATH.fullmatch(path)
            if batch_match:
                batch_id = batch_match.group(1)
                payload = repository.load_batch(batch_id)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "batch_not_found", "batch_id": batch_id})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/operations":
                query = parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                self._send_json(
                    HTTPStatus.OK,
                    {"schema_version": "amazon-us-console-v2", "items": repository.list_operations(limit)},
                )
                return
            run_match = RUN_PATH.fullmatch(path)
            if run_match:
                run_id = run_match.group(1)
                payload = repository.load_run(run_id)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run_not_found", "run_id": run_id})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if path == "/api/items":
                query = parse_qs(parsed.query)
                status = query.get("status", [""])[0].strip() or None
                stage = query.get("stage", [""])[0].strip() or None
                term = query.get("q", [""])[0].strip()[:100] or None
                limit = int(query.get("limit", ["100"])[0])
                offset = int(query.get("offset", ["0"])[0])
                if status and not re.fullmatch(r"[a-z_]+", status):
                    raise ValueError("invalid status")
                if stage and stage not in {"product", "reviews", "complete"}:
                    raise ValueError("invalid stage")
                payload = repository.list_items(
                    status=status, stage=stage, query=term, limit=limit, offset=offset
                )
                self._send_json(HTTPStatus.OK, payload)
                return
            match = ITEM_PATH.fullmatch(path)
            if match:
                asin = match.group(1).upper()
                if not ASIN_RE.fullmatch(asin):
                    raise ValueError("invalid ASIN")
                payload = repository.load_detail(asin)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "asin_not_found", "asin": asin})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
        except Exception:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "database_unavailable"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "route_not_found"})
            return
        if not self._authorized(write=True):
            return
        try:
            # ---- POST /api/batches: 上传清单创建批次 ----
            if path == "/api/batches":
                repository = self._scoped_repository(parsed)
                rows, fields = self._read_multipart_manifest()
                # 评论开关：复选框勾选时发送（on/true/1 都算开），未发送 = 关
                collect_reviews = fields.get("collect_reviews", "on").strip().lower() in ("on", "true", "1")
                result = repository.create_batch(rows, collect_reviews=collect_reviews)
                self._send_json(HTTPStatus.CREATED, result)
                return

            # ---- POST /api/batches/json: Agent JSON 建批次（异步，立即返回批次ID） ----
            if path == "/api/batches/json":
                repository = self._scoped_repository(parsed)
                body = self._read_json_body(max_bytes=65536)
                result = repository.create_batch(
                    _rows_from_asin_payload(body),
                    idempotency_key=str(body.get("idempotency_key") or ""),
                    uploaded_by="agent",
                    # 评论开关：JSON 布尔字段，缺省默认开（true/false，容错字符串）
                    collect_reviews=str(body.get("collect_reviews", "true")).strip().lower()
                        not in ("false", "0", "no", "off"),
                )
                # 语义：提交即受理（异步采集，Agent 后续轮询进度接口）
                self._send_json(
                    HTTPStatus.CREATED if not result.get("idempotent_replay") else HTTPStatus.OK,
                    result,
                )
                return

            # ---- POST /api/coordinator/start: 启动协调器 ----
            if path == "/api/coordinator/start":
                mgr = self.server.coordinator_manager
                if mgr is None:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "coordinator_manager_not_configured"})
                    return
                status = mgr.start()
                self._send_json(HTTPStatus.OK, status)
                return

            # ---- POST /api/coordinator/restart: 调整 worker 数并重启协调器 ----
            if path == "/api/coordinator/restart":
                mgr = self.server.coordinator_manager
                if mgr is None:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "coordinator_manager_not_configured"})
                    return
                body = self._read_json_body()
                workers = int(body.get("workers") or 0)
                if not 1 <= workers <= 8:
                    raise ValueError("workers 必须在 1-8 之间")
                status = mgr.restart(workers)
                status["workers"] = workers
                self._send_json(HTTPStatus.OK, status)
                return

            # ---- POST /api/batches/{id}/stop: 停止批次 ----
            batch_match = BATCH_PATH.fullmatch(path)
            if batch_match and path.endswith('/stop'):
                batch_id = batch_match.group(1)
                repository = self._scoped_repository(parsed)
                result = repository.request_stop(batch_id)
                self._send_json(HTTPStatus.OK, result)
                return

            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_console_route"})
        except KeyError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc).split(":", 1)[0]})
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request", "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "detail": str(exc)})

    def _action_is_stop(self, parsed) -> bool:
        """判断 POST /api/batches/{id}/stop（URL 末尾带 /stop）。"""
        return parsed.path.endswith("/stop")

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_console_route"})

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/coordinator":
            if not self._authorized(write=True):
                return
            mgr = self.server.coordinator_manager
            if mgr is None:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "coordinator_manager_not_configured"})
                return
            result = mgr.stop()
            self._send_json(HTTPStatus.OK, result)
            return
        self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "read_only_console_route"})

    def _read_json_body(self, max_bytes: int = 65536) -> dict[str, Any]:
        """读取并解析 JSON 请求体（Agent 提交批次等接口用）。"""
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("空请求体")
        if length > max_bytes:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"无效 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return body

    def _read_multipart_manifest(self) -> tuple[list[dict[str, str]], dict[str, str]]:
        """解析 multipart/form-data，提取清单文件和普通表单字段。

        返回 (清单行, 表单字段字典)；表单字段用于接收上传选项
        （如 collect_reviews 复选框：未勾选时浏览器不发送该字段）。
        """
        length = int(self.headers.get("Content-Length", "0"))
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            raise ValueError("需要 multipart/form-data 上传")
        if length <= 0:
            raise ValueError("空请求体")
        raw = self.rfile.read(length)
        # 手动解析 multipart（无第三方库）
        boundary = None
        for part in content_type.split(";"):
            part = part.strip()
            if part.startswith("boundary="):
                boundary = part[len("boundary="):].strip('"')
                break
        if not boundary:
            raise ValueError("缺少 boundary 参数")
        sep = b"--" + boundary.encode()
        parts = raw.split(sep)
        file_data: bytes | None = None
        file_name = ""
        form_fields: dict[str, str] = {}
        for part in parts:
            if b"Content-Disposition" not in part:
                continue
            header_end = part.find(b"\r\n\r\n")
            if header_end < 0:
                continue
            disposition = part[:header_end]
            body = part[header_end + 4:].rstrip(b"\r\n")
            if b"filename=" in disposition:
                # 文件部分：记录内容与文件名（按扩展名分派解析器）
                file_data = body
                name_match = re.search(rb'filename="([^"]+)"', disposition)
                if name_match:
                    file_name = name_match.group(1).decode("utf-8", "replace")
            else:
                # 普通字段部分：name=value（如复选框 collect_reviews=on）
                name_match = re.search(rb'name="([^"]+)"', disposition)
                if name_match:
                    field = name_match.group(1).decode("utf-8", "replace")
                    form_fields[field] = body.decode("utf-8", "replace")
        if file_data is None:
            raise ValueError("未找到清单文件（字段名 manifest，支持 .csv / .xlsx）")
        if len(file_data) > 5 * 1024 * 1024:
            raise ValueError("文件超过 5MB 限制")
        # 按扩展名分派：Excel 走专用解析器，其余按 CSV 文本解析
        if file_name.lower().endswith(".xlsx"):
            rows = parse_manifest_excel(file_data)
        else:
            rows = parse_manifest_csv(file_data)
        return rows, form_fields

    def log_message(self, format: str, *args: Any) -> None:
        if self.server.access_log:
            super().log_message(format, *args)


class ConsoleServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        repository: Any,
        *,
        raw_html_dir: Path | None = None,
        api_key: str = "",
        read_key: str = "",
        access_log: bool = False,
        coordinator_manager: CoordinatorManager | None = None,
    ):
        if address[0] not in LOOPBACK_HOSTS:
            raise ValueError("console only allows loopback host")
        super().__init__(address, ConsoleHandler)
        self.repository = repository
        self.raw_html_dir = raw_html_dir
        self.api_key = api_key
        # 只读密钥：持有者只能查询，不能上传/启动/停止（只读 Agent 用）
        self.read_key = read_key
        self.access_log = access_log
        self.coordinator_manager = coordinator_manager


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    parser.add_argument("--tenant-id", help="optional initial tenant; the UI can switch among PostgreSQL tenants")
    parser.add_argument("--raw-html-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--api-key-env", default="AMAZON_COLLECTION_API_KEY")
    parser.add_argument("--require-api-key", action="store_true")
    # 只读密钥环境变量：配置后，只读 Agent 用它访问查询接口，写操作被拒
    parser.add_argument("--read-key-env", default="AMAZON_CONSOLE_READ_KEY")
    parser.add_argument("--access-log", action="store_true")
    args = parser.parse_args(argv)
    try:
        dsn = os.environ.get(args.dsn_env, "").strip()
        if not dsn:
            raise ValueError(f"PostgreSQL DSN environment variable is required: {args.dsn_env}")
        api_key = os.environ.get(args.api_key_env, "")
        if args.require_api_key and not api_key:
            raise ValueError(f"required API key environment variable is missing: {args.api_key_env}")
        read_key = os.environ.get(args.read_key_env, "")
        repository = PostgresConsoleRepository(dsn, args.tenant_id)
        coord_mgr = CoordinatorManager(dsn, args.tenant_id or "amazon_us_local")
        server = ConsoleServer(
            (args.host, args.port),
            repository,
            raw_html_dir=args.raw_html_dir,
            api_key=api_key,
            read_key=read_key,
            access_log=args.access_log,
            coordinator_manager=coord_mgr,
        )
        print(f"Amazon Collection Console: http://{args.host}:{server.server_port}")
        print(f"Tenants: PostgreSQL-visible | refresh: 5s | 协调器托管: 已启用")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            # console 退出时确保协调器子进程也终止
            coord_mgr.stop()
            server.server_close()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
