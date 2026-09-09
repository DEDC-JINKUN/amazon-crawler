# -*- coding: utf-8 -*-
"""Amazon Crawler MCP 服务器（stdio，零第三方依赖）。

把 8771 控制台 API + 熔断门文件操作包装成 MCP 工具，供 agent 会话直接
监督/操作爬虫批次，不必手写 SQL 或 PowerShell。

工具一览：
    overview            租户全景（进度/被拦/流量/近期 run）
    list_batches        批次列表 + 协调器心跳
    batch_status        单批次进度
    batch_items         批次成员明细（可按状态/关键词过滤）
    create_batch        用 ASIN 数组建批次（异步语义：返回批次ID，轮询进度）
    stop_batch          请求停止批次
    item_detail         单 ASIN 商品快照 + 采集状态
    captcha_gate_status 读熔断门状态
    clear_captcha_gate  清熔断门（唤醒睡眠中的 worker，最多 5 分钟生效）

环境变量：
    CRAWLER_CONSOLE_URL  控制台地址（默认 http://127.0.0.1:8771）
    CRAWLER_TENANT       租户（默认 amazon_us_main）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "state" / "captcha_gate.json"
PROTOCOL_VERSION = "2024-11-05"
CONSOLE_URL = os.environ.get("CRAWLER_CONSOLE_URL", "http://127.0.0.1:8771").rstrip("/")
TENANT = os.environ.get("CRAWLER_TENANT", "amazon_us_main")


# ---------------------------------------------------------------------------
# 控制台 HTTP 封装
# ---------------------------------------------------------------------------

def _http_json(method: str, path: str, body: dict | None = None, timeout: int = 30) -> dict:
    url = f"{CONSOLE_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8")
            return {"http_status": resp.status, "body": json.loads(payload) if payload else {}}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        return {"http_status": exc.code, "error": detail}
    except urllib.error.URLError as exc:
        return {"http_status": 0, "error": f"console_unreachable: {exc.reason}"}


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------

def tool_overview(args: dict) -> dict:
    resp = _http_json("GET", f"/api/overview?tenant={TENANT}")
    if resp.get("http_status") != 200:
        return resp
    body = resp["body"]
    compact = {
        "tenant": body.get("tenant_id"),
        "progress": body.get("progress"),
        "status_counts": body.get("status_counts"),
        "block_counts": body.get("block_counts"),
        "error_counts": body.get("error_counts"),
        "table_counts": body.get("table_counts"),
        "traffic_saved_raw_html_gb": round((body.get("traffic", {}).get("saved_raw_html_bytes") or 0) / 1e9, 2),
        "last_evidence_at": body.get("last_evidence_at"),
    }
    return {"http_status": 200, "body": compact, "recent_runs": body.get("recent_runs", [])[:5]}


def tool_list_batches(args: dict) -> dict:
    limit = int(args.get("limit", 10))
    return _http_json("GET", f"/api/batches?tenant={TENANT}&limit={limit}")


def tool_batch_status(args: dict) -> dict:
    batch_id = str(args.get("batch_id", "")).strip()
    if not batch_id:
        return {"error": "batch_id is required"}
    return _http_json("GET", f"/api/batches/{batch_id}?tenant={TENANT}")


def tool_batch_items(args: dict) -> dict:
    batch_id = str(args.get("batch_id", "")).strip()
    if not batch_id:
        return {"error": "batch_id is required"}
    params = [f"tenant={TENANT}", f"limit={int(args.get('limit', 20))}"]
    if args.get("status"):
        params.append(f"status={args['status']}")
    if args.get("q"):
        params.append(f"q={urllib.request.quote(str(args['q']))}")
    return _http_json("GET", f"/api/batches/{batch_id}/items?{'&'.join(params)}")


def tool_create_batch(args: dict) -> dict:
    asins = args.get("asins") or []
    if not isinstance(asins, list) or not asins:
        return {"error": "asins must be a non-empty array"}
    body = {
        "asins": [str(a).strip().upper() for a in asins],
        "collect_reviews": bool(args.get("collect_reviews", False)),
    }
    if args.get("idempotency_key"):
        body["idempotency_key"] = str(args["idempotency_key"])
    return _http_json("POST", f"/api/batches/json?tenant={TENANT}", body)


def tool_stop_batch(args: dict) -> dict:
    batch_id = str(args.get("batch_id", "")).strip()
    if not batch_id:
        return {"error": "batch_id is required"}
    return _http_json("POST", f"/api/batches/{batch_id}/stop?tenant={TENANT}", {})


def tool_item_detail(args: dict) -> dict:
    asin = str(args.get("asin", "")).strip().upper()
    if len(asin) != 10:
        return {"error": "asin must be 10 chars"}
    return _http_json("GET", f"/api/items/{asin}?tenant={TENANT}")


def _read_gate() -> dict:
    if not GATE_PATH.exists():
        return {"exists": False, "note": "门文件不存在=无熔断记录"}
    try:
        gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"exists": True, "error": str(exc)}
    paused_until = gate.get("paused_until")
    remaining_min = None
    if paused_until:
        try:
            until = datetime.fromisoformat(paused_until)
            remaining_min = round((until - datetime.now(timezone.utc)).total_seconds() / 60, 1)
        except ValueError:
            pass
    return {
        "exists": True,
        "date_utc": gate.get("date"),
        "blocked_today": gate.get("count"),
        "consecutive_failures": gate.get("consecutive"),
        "paused_until_utc": paused_until,
        "pause_remaining_minutes": remaining_min,
    }


def tool_captcha_gate_status(args: dict) -> dict:
    return _read_gate()


def tool_clear_captcha_gate(args: dict) -> dict:
    before = _read_gate()
    GATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    gate = {
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "count": 0,
        "consecutive": 0,
        "paused_until": None,
    }
    GATE_PATH.write_text(json.dumps(gate, indent=2), encoding="utf-8")
    return {
        "cleared": True,
        "before": before,
        "after": _read_gate(),
        "note": "睡眠中的 worker 每 300s 重读门文件，最多 5 分钟内醒来续跑",
    }


# ---------------------------------------------------------------------------
# MCP 工具注册表
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "overview",
        "description": "爬虫租户全景：批次进度、被拦/失败计数、数据表规模、流量、近期 run。日常监督首选。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_batches",
        "description": "批次列表 + 协调器在线状态/心跳。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10, "description": "返回条数 1-100"}},
        },
    },
    {
        "name": "batch_status",
        "description": "单批次进度（成功/被拦/失败/待跑计数）。",
        "inputSchema": {
            "type": "object",
            "properties": {"batch_id": {"type": "string", "description": "批次 UUID"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "batch_items",
        "description": "批次成员明细，可按 status（succeeded/blocked/failed/pending/running）与关键词过滤。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string"},
                "status": {"type": "string", "description": "成员状态过滤"},
                "q": {"type": "string", "description": "ASIN/关键词模糊搜索"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["batch_id"],
        },
    },
    {
        "name": "create_batch",
        "description": "用 ASIN 数组建采集批次（异步：立即返回批次ID，由协调器自动拉起 worker，用 batch_status 轮询）。单批上限 5800。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asins": {"type": "array", "items": {"type": "string"}, "description": "10 位大写 ASIN 数组"},
                "idempotency_key": {"type": "string", "description": "幂等键：同键 24h 重试返回同一批次"},
                "collect_reviews": {"type": "boolean", "default": False, "description": "是否采评论（默认关，商品页完成即收尾）"},
            },
            "required": ["asins"],
        },
    },
    {
        "name": "stop_batch",
        "description": "请求停止批次（已终态批次会报错）。",
        "inputSchema": {
            "type": "object",
            "properties": {"batch_id": {"type": "string"}},
            "required": ["batch_id"],
        },
    },
    {
        "name": "item_detail",
        "description": "单 ASIN 详情：商品快照（价格/评分/评论数/BSR）+ 采集状态 + 媒体/内容计数。",
        "inputSchema": {
            "type": "object",
            "properties": {"asin": {"type": "string", "description": "10 位 ASIN"}},
            "required": ["asin"],
        },
    },
    {
        "name": "captcha_gate_status",
        "description": "读 CAPTCHA 熔断门：当日被拦数、连续失败数、暂停剩余分钟。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_captcha_gate",
        "description": "清熔断门并唤醒睡眠中的 worker（≤5 分钟生效）。用于换节点/代理后立即续跑，省 6 小时等待。慎用：直连被拦率高时频繁清门=反复撞墙。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_FUNCS = {
    "overview": tool_overview,
    "list_batches": tool_list_batches,
    "batch_status": tool_batch_status,
    "batch_items": tool_batch_items,
    "create_batch": tool_create_batch,
    "stop_batch": tool_stop_batch,
    "item_detail": tool_item_detail,
    "captcha_gate_status": tool_captcha_gate_status,
    "clear_captcha_gate": tool_clear_captcha_gate,
}


# ---------------------------------------------------------------------------
# JSON-RPC / MCP 协议层
# ---------------------------------------------------------------------------

SERVER_INFO = {"name": "amazon-crawler", "version": "1.0.0"}


def _result(req_id: object, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
    sys.stdout.flush()


def _error(req_id: object, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}) + "\n"
    )
    sys.stdout.flush()


def _handle_request(msg: dict) -> None:
    method = msg.get("method", "")
    req_id = msg.get("id")

    if method == "initialize":
        _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    elif method == "ping":
        _result(req_id, {})
    elif method == "tools/list":
        _result(req_id, {"tools": TOOLS})
    elif method == "tools/call":
        name = (msg.get("params") or {}).get("name", "")
        args = (msg.get("params") or {}).get("arguments") or {}
        func = TOOL_FUNCS.get(name)
        if func is None:
            _result(req_id, {
                "content": [{"type": "text", "text": f"unknown tool: {name}"}],
                "isError": True,
            })
            return
        try:
            payload = func(args)
            text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            _result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:  # noqa: BLE001
            _result(req_id, {
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    else:
        # 未知请求方法
        if req_id is not None:
            _error(req_id, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        # notification（无 id）不回包
        if msg.get("id") is None and not str(msg.get("method", "")).startswith("tools/"):
            if msg.get("method") == "notifications/initialized":
                continue
            if msg.get("method") == "initialize":
                _handle_request(msg)
            continue
        _handle_request(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
