# -*- coding: utf-8 -*-
"""模拟 worker：不访问网络，用于隔离环境验证协调器编排。

行为由环境变量控制（全部可选）：
    MOCK_DELAY_SECONDS   每个任务的处理延迟（默认 0.2，模拟真实采集耗时）
    MOCK_FAIL_EVERY      每 N 个任务失败一个（0=从不失败，默认 0）
    MOCK_FAIL_CLASS      失败类别：fetch / system / blocked（默认 fetch）
    MOCK_MAX_ACTIONS     最多处理多少个任务后退出（默认不限，领不到就退出）
    MOCK_INSTANT_EXIT    =1 时不领任何任务立即退出（复现 CAPTCHA 熔断 worker
                          启动即退出的场景，用于验证协调器重启退避）

接口与 amazon_us_worker.py 的批次模式一致（--batch-id/--tenant-id 等），
协调器通过 --worker-script 指向本文件即可离线跑完整流程。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from batch_store import BatchStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模拟批次 worker（离线）")
    parser.add_argument("--backend", default="postgres")           # 兼容协调器传参
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--live", action="store_true")             # 兼容协调器传参
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--subject-type", default="own")
    parser.add_argument("--worker-id", default=f"mock-worker-{os.getpid()}")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    args = parser.parse_args(argv)

    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2

    # 行为参数
    delay = float(os.environ.get("MOCK_DELAY_SECONDS", "0.2"))
    fail_every = int(os.environ.get("MOCK_FAIL_EVERY", "0"))
    fail_class = os.environ.get("MOCK_FAIL_CLASS", "fetch")
    max_actions = int(os.environ.get("MOCK_MAX_ACTIONS", "0"))

    store = BatchStore(dsn, args.tenant_id, args.subject_type)
    run_id = args.run_id or f"mock-run-{uuid.uuid4().hex[:8]}"
    actions = 0

    if os.environ.get("MOCK_INSTANT_EXIT") == "1":
        # 不领任务、立即退出（复现 CAPTCHA 熔断门让 worker 启动即退出的场景）
        return 0

    while True:
        if max_actions and actions >= max_actions:
            break
        # 领取任务：批次停止/收尾后返回 None → 自然退出
        task = store.claim_task(args.worker_id, batch_id=args.batch_id, lease_seconds=120)
        if task is None:
            break
        actions += 1
        if delay > 0:
            time.sleep(delay)  # 模拟采集耗时
        evidence = {
            "run_id": run_id,
            "url": task["url"],
            "http_status": 200,
            "transfer_bytes": 2048,
            "source_type": "http_html",
            "content_hash": "b" * 64,
            "block_reason": None,
            "parser_version": "mock",
            "error_code": None,
            "context_json": {"mock": True},
        }
        # 按配置注入失败
        if fail_every and actions % fail_every == 0:
            fail_evidence = dict(evidence)
            if fail_class == "blocked":
                fail_evidence["block_reason"] = "captcha"
                fail_evidence["http_status"] = 503
            elif fail_class == "system":
                fail_evidence["error_code"] = "parse_error"
            else:
                fail_evidence["error_code"] = "fetch_error"
            store.save_failure(
                task=task,
                reason=f"{fail_class}_error",
                error=f"mock {fail_class} failure",
                evidence=fail_evidence,
                next_status="blocked" if fail_class == "blocked" else "failed",
            )
            continue
        # 成功路径：写商品快照 + 证据 + 成员终态
        store.save_product_result(
            task=task,
            evidence=evidence,
            product={
                "title": f"模拟商品 {task['asin']}",
                "price": "$19.99",
                "availability": "In Stock",
                "status": "product_done",
            },
            media=[],
            content_modules=[],
            review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
            next_status="succeeded",
            state_fields={"task_stage": "reviews"},
            reason="mock_success",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
