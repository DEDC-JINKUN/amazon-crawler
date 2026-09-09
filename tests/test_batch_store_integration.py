# -*- coding: utf-8 -*-
"""批次模型存储层集成测试：连真实 PostgreSQL，覆盖核心生命周期。

场景覆盖（对应验证计划）：
1. 正常：创建批次（含去重）→ 领取 → 成功保存 → 收尾 completed
2. 失败重试：失败保存（三分类）→ 重新领取 → 成功
3. 停止：请求停止 → 领取返回 None → 收尾 stopped / blocked
4. 崩溃恢复：租约过期 → 回收 → 重新领取
5. 容量：槽位防超卖 → 释放 → 再租
6. 连续批次：同 ASIN 两个批次，各自独立计进度（互不污染）

隔离方式：独立 tenant_id，测试结束清理本租户全部数据。
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get(
    "AMAZON_US_POSTGRES_DSN",
    "postgresql://postgres:123456@localhost:5432/amazon_us",
)


def load_batch_store():
    """按项目惯例动态加载 scripts/batch_store.py。"""
    spec = importlib.util.spec_from_file_location(
        "batch_store", ROOT / "scripts" / "batch_store.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# 模块级单例：pytest.raises 需要捕获同一个类对象，
# 每次重新加载会产生不同的 BatchStoreError 类导致捕获失败
_BATCH_STORE = load_batch_store()


@pytest.fixture()
def store():
    """每个测试独立租户，互不可见；结束时清理。"""
    tenant = f"batchtest_{uuid.uuid4().hex[:12]}"
    s = _BATCH_STORE.BatchStore(DSN, tenant, "own")
    yield s
    # 清理：本租户的批次（级联删成员）+ 旧表数据
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM amazon_us.collection_evidence WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.product_snapshot WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.media_asset WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.content_module WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.review_summary WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.review_record WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.review_page_state WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.state_history WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.item_state WHERE tenant_id=%s", (tenant,)
            )
            cur.execute(
                "DELETE FROM amazon_us.asin_master WHERE tenant_id=%s", (tenant,)
            )
            cur.execute("DELETE FROM amazon_us.batch WHERE tenant_id=%s", (tenant,))
        conn.commit()


def _rows(*asins):
    """构造上传清单行。"""
    return [
        {"asin": a, "url": f"https://www.amazon.com/dp/{a}"} for a in asins
    ]


def _evidence(run_id, url):
    """构造最小证据。"""
    return {
        "run_id": run_id, "url": url, "http_status": 200,
        "transfer_bytes": 1234, "source_type": "http_html",
        "content_hash": "a" * 64, "block_reason": None,
        "parser_version": "test", "error_code": None, "context_json": {},
    }


# ---------------------------------------------------------------------
# 1. 正常链路
# ---------------------------------------------------------------------
def test_create_batch_dedup_and_claim(store):
    """创建批次：同批 ASIN 去重；领取拿到租约。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST01", "B000TEST02"))
    assert result["requested_count"] == 2
    assert result["skipped_duplicates"] == 1
    batch_id = result["batch_id"]

    assert store.start_batch(batch_id) is True
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None
    assert task["asin"] in {"B000TEST01", "B000TEST02"}
    assert task["lease_token"]
    assert task["status"] == "running"


def test_success_path_finalize_completed(store):
    """全部成功 → 批次 completed，进度只认本批次。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    for _ in range(2):
        task = store.claim_task("worker-a", batch_id=batch_id)
        assert task is not None
        ok = store.save_product_result(
            task=task,
            evidence=_evidence(run_id, task["url"]),
            product={"title": "测试商品", "price": "$9.99", "status": "product_done"},
            media=[],
            content_modules=[],
            review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
            next_status="succeeded",
            state_fields={"task_stage": "reviews"},
            reason="product_and_reviews_done",
        )
        assert ok is True
    # 没任务可领了
    assert store.claim_task("worker-a", batch_id=batch_id) is None
    summary = store.finalize_batch(batch_id)
    assert summary["batch_status"] == "completed"
    assert summary["succeeded"] == 2
    assert summary["evidence_count"] == 2  # 证据与成功数一致


def test_success_path_with_dirty_numeric_fields(store):
    """真实 parser 的脏数字字段（空串/空格/带逗号/货币符号）不能炸整数列。

    回归背景：parser 的 _count_from_text 拿不到计数时返回空字符串，
    worker 原样放进 state_fields，_finish_item 曾直接透传给
    batch_item.reported_rating_count（integer 列）触发 PostgreSQL 报错，
    导致真实商品采集成功却保存失败。
    """
    result = store.create_batch(_rows("B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None
    ok = store.save_product_result(
        task=task,
        evidence=_evidence(f"run-{uuid.uuid4().hex[:8]}", task["url"]),
        # product 里的计数字段也全是脏值（历史版本会炸 product_snapshot）
        product={
            "title": "无计数商品", "price": "$19.99", "status": "product_done",
            "reported_rating_count": "", "reported_review_count": "  ",
            "rating": None,
        },
        media=[],
        content_modules=[],
        review_summary={
            "status": "exhausted",
            "reported_rating_count": "",       # 空字符串
            "reported_review_count": "  ",     # 空格
            "fetched_count": "1,234",          # 带逗号
            "pages_fetched": " 5 ",            # 带空格
        },
        next_status="succeeded",
        state_fields={
            "task_stage": "product",
            "reported_rating_count": "",       # 空字符串 → 应转 None
            "reported_review_count": "N/A",    # 非数字 → 应转 None
            "next_review_page": "",            # 空字符串 → 应转 None
            "review_page_limit": " 10 ",       # 带空格数字 → 应转 10
        },
        reason="product_done",
    )
    assert ok is True
    # 库里整数列要么是干净整数要么是 NULL，不能是字符串
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT reported_rating_count, reported_review_count,
                          next_review_page, review_page_limit
                   FROM amazon_us.batch_item WHERE batch_id=%s AND asin=%s""",
                (batch_id, task["asin"]),
            )
            rating_count, review_count, next_page, page_limit = cur.fetchone()
    assert rating_count is None      # "" → None
    assert review_count is None      # "N/A" → None
    assert next_page is None         # "" → None
    assert page_limit == 10          # " 10 " → 10
    summary = store.finalize_batch(batch_id)
    assert summary["batch_status"] == "completed"
    assert summary["succeeded"] == 1


# ---------------------------------------------------------------------
# 2. 失败与三分类
# ---------------------------------------------------------------------
def test_failure_classification_and_retry(store):
    """fetch 类失败可重试；blocked 终态分类正确。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"

    # 第一个成员：网络失败（fetch 分类）
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert store.save_failure(
        task=task, reason="fetch_error", error="proxy timeout",
        evidence=_evidence(run_id, task["url"]),
        next_status="failed",
    ) is True
    # 该成员 attempts 计数 +1（查库验证，因为重新领取可能先拿到另一个成员）
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempts FROM amazon_us.batch_item WHERE batch_id=%s AND asin=%s",
                (batch_id, task["asin"]),
            )
            assert cur.fetchone()[0] == 1
    # 重新领取（可重试的 failed 也会被领到）
    remaining = []
    while True:
        t = store.claim_task("worker-a", batch_id=batch_id)
        if t is None:
            break
        remaining.append(t)
    assert len(remaining) == 2  # 另一个 pending + 刚失败的 retryable

    # 把剩余成员都置为 blocked（模拟被拦）
    for t in remaining:
        ev = _evidence(run_id, t["url"])
        ev["block_reason"] = "captcha"
        assert store.save_failure(
            task=t, reason="captcha", error="captcha",
            evidence=ev, next_status="blocked",
        ) is True
    # blocked 成员不再可领
    assert store.claim_task("worker-a", batch_id=batch_id) is None
    summary = store.finalize_batch(batch_id)
    # 第一个成员是 fetch 失败（attempts=1 < max=3 仍可重试，收尾时停止语义）
    # 剩余失败里没有 system → 终态按停止/被拦处理
    assert summary["batch_status"] in {"blocked", "stopped"}
    assert summary["blocked"] >= 1


def test_system_failure_marks_batch_failed(store):
    """system 类失败 → 批次 failed，主导类别 system。"""
    result = store.create_batch(_rows("B000TEST01"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    task = store.claim_task("worker-a", batch_id=batch_id)
    # 把 attempts 拉满：不可重试的 system 失败
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE amazon_us.batch_item SET attempts=3 WHERE batch_id=%s",
                (batch_id,),
            )
        conn.commit()
    assert store.save_failure(
        task=task, reason="parse_error", error="title missing",
        next_status="failed", terminal=True,
    ) is True
    summary = store.finalize_batch(batch_id)
    assert summary["batch_status"] == "failed"
    assert summary["final_failure_class"] == "system"
    assert summary["failed_system"] == 1


# ---------------------------------------------------------------------
# 3. 停止
# ---------------------------------------------------------------------
def test_stop_request_blocks_claim(store):
    """停止请求后：领取返回 None；活租约等 worker 写完，崩溃后收尾 stopped。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None

    assert store.request_stop(batch_id) is True
    # 停止后领不到新任务（worker 自然退出）
    assert store.claim_task("worker-a", batch_id=batch_id) is None
    # 活租约还在时不能收尾（worker 可能正在写结果）
    assert store.finalize_batch(batch_id) is None
    # 模拟 worker 崩溃：租约过期 → 回收 → 残余成员转 cancelled
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE amazon_us.batch_item SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' "
                "WHERE batch_id=%s AND status='running'",
                (batch_id,),
            )
        conn.commit()
    assert store.reclaim_expired_items() >= 1
    summary = store.finalize_batch(batch_id)
    assert summary["batch_status"] == "stopped"
    assert summary["cancelled"] + summary["pending"] == 2


# ---------------------------------------------------------------------
# 4. 崩溃恢复：租约过期回收
# ---------------------------------------------------------------------
def test_expired_lease_reclaimed(store):
    """worker 崩溃（租约未释放）→ 过期回收 → 重新领取。"""
    result = store.create_batch(_rows("B000TEST01"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    task = store.claim_task("worker-a", batch_id=batch_id, lease_seconds=1)
    assert task is not None

    # 模拟租约过期（直接把过期时间改到过去）
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE amazon_us.batch_item SET lease_expires_at=CURRENT_TIMESTAMP-INTERVAL '1 second' "
                "WHERE batch_id=%s AND asin=%s",
                (batch_id, task["asin"]),
            )
        conn.commit()

    # 旧持有者写入被拒（租约已过期）
    assert store.save_failure(
        task=task, reason="whatever", error="late write",
        next_status="failed",
    ) is False
    # 回收后新 worker 可领取
    assert store.reclaim_expired_items() >= 1
    task2 = store.claim_task("worker-b", batch_id=batch_id)
    assert task2 is not None
    assert task2["asin"] == task["asin"]


# ---------------------------------------------------------------------
# 5. 容量槽：防超卖
# ---------------------------------------------------------------------
def test_slot_no_oversell(store):
    """2 个槽：第 3 个租不到；释放后可再租。"""
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO amazon_us.egress_endpoint (egress_id, total_slots, enabled) "
                "VALUES ('test-egress', 2, true) ON CONFLICT DO NOTHING"
            )
            cur.execute(
                "INSERT INTO amazon_us.egress_slot (egress_id, slot_index) VALUES "
                "('test-egress',0),('test-egress',1) ON CONFLICT DO NOTHING"
            )
        conn.commit()
    try:
        s1 = store.acquire_slot("worker-a", egress_id="test-egress")
        s2 = store.acquire_slot("worker-b", egress_id="test-egress")
        assert s1 is not None and s2 is not None
        assert (s1["slot_index"], s2["slot_index"]) != (s1["slot_index"], s1["slot_index"])
        # 第 3 个租不到（容量已满，不等待）
        assert store.acquire_slot("worker-c", egress_id="test-egress") is None
        # 释放后可再租
        assert store.release_slot(s1["egress_id"], s1["slot_index"], "worker-a") is True
        s3 = store.acquire_slot("worker-c", egress_id="test-egress")
        assert s3 is not None
        # 心跳续期
        assert store.renew_slot(s3["egress_id"], s3["slot_index"], "worker-c") is True
        # 清理
        store.release_slot(s2["egress_id"], s2["slot_index"], "worker-b")
        store.release_slot(s3["egress_id"], s3["slot_index"], "worker-c")
    finally:
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM amazon_us.egress_slot WHERE egress_id='test-egress'")
                cur.execute("DELETE FROM amazon_us.egress_endpoint WHERE egress_id='test-egress'")
            conn.commit()


# ---------------------------------------------------------------------
# 6. 连续批次：同 ASIN 不互相污染
# ---------------------------------------------------------------------
def test_overlapping_batches_independent(store):
    """同一 ASIN 连续两个批次：第一个成功不影响第二个从头采集。"""
    # 批次 1：B000TEST01 成功
    r1 = store.create_batch(_rows("B000TEST01"))
    store.start_batch(r1["batch_id"])
    task = store.claim_task("worker-a", batch_id=r1["batch_id"])
    store.save_product_result(
        task=task,
        evidence=_evidence("run-1", task["url"]),
        product={"title": "第一次", "status": "product_done"},
        media=[], content_modules=[],
        review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
        next_status="succeeded",
        state_fields={"task_stage": "reviews"},
        reason="done",
    )
    store.finalize_batch(r1["batch_id"])

    # 批次 2：同一 ASIN 重新上传，从 pending 开始
    r2 = store.create_batch(_rows("B000TEST01"))
    store.start_batch(r2["batch_id"])
    task2 = store.claim_task("worker-a", batch_id=r2["batch_id"])
    assert task2 is not None
    assert task2["status"] == "running"
    assert task2["attempts"] == 0
    # 两个批次进度各自独立
    s1 = store.batch_summary(r1["batch_id"])
    s2 = store.batch_summary(r2["batch_id"])
    assert s1["succeeded"] == 1
    assert s2["succeeded"] == 0
    assert s2["pending"] + s2["running"] == 1


def test_duplicate_active_manifest_rejected(store):
    """完全相同清单在活跃期间重复上传 → 拒绝；终态后重跑允许。"""
    r1 = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    store.start_batch(r1["batch_id"])
    with pytest.raises(_BATCH_STORE.BatchStoreError):
        store.create_batch(_rows("B000TEST02", "B000TEST01"))  # 行序不同也算相同
    # 批次结束后重跑允许：先停止并收尾成终态
    store.request_stop(r1["batch_id"])
    assert store.finalize_batch(r1["batch_id"])["batch_status"] == "stopped"
    r2 = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    assert r2["batch_id"] != r1["batch_id"]


# ---------------------------------------------------------------------
# 7. canary 预检：探针通过才放开全量
# ---------------------------------------------------------------------
def _batch_status(batch_id):
    """直查批次状态。"""
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM amazon_us.batch WHERE batch_id=%s", (batch_id,))
            return cur.fetchone()[0]


def test_canary_marks_probes_and_blocks_full_claim(store):
    """启动后进 canary：只有探针成员可领，非探针领不到。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03", "B000TEST04"))
    batch_id = result["batch_id"]
    assert store.start_batch(batch_id) is True
    assert _batch_status(batch_id) == "canary"
    # 探针 = 按 ASIN 排序前 2 个
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT asin FROM amazon_us.batch_item WHERE batch_id=%s AND is_canary ORDER BY asin",
                (batch_id,),
            )
            probes = [r[0] for r in cur.fetchall()]
    assert probes == ["B000TEST01", "B000TEST02"]
    # canary 阶段只领得到探针成员：领 2 个后第 3 个返回 None
    claimed = []
    for _ in range(3):
        t = store.claim_task("worker-a", batch_id=batch_id)
        if t is None:
            break
        claimed.append(t["asin"])
    assert claimed == ["B000TEST01", "B000TEST02"]


def test_canary_evaluate_waits_until_probes_done(store):
    """探针有 pending/running → 返回 None 继续等，批次保持 canary。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    # 一个探针都没跑
    assert store.evaluate_canary(batch_id) is None
    # 领走一个（running）
    store.claim_task("worker-a", batch_id=batch_id)
    assert store.evaluate_canary(batch_id) is None
    assert _batch_status(batch_id) == "canary"


def test_canary_pass_opens_full_batch(store):
    """探针全成功 → passed=True，批次转 running，非探针可领。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 跑完两个探针（全成功）
    for _ in range(2):
        task = store.claim_task("worker-a", batch_id=batch_id)
        assert task is not None
        assert store.save_product_result(
            task=task,
            evidence=_evidence(run_id, task["url"]),
            product={"title": "探针商品", "price": "$1.00", "status": "product_done"},
            media=[], content_modules=[],
            review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
            next_status="succeeded",
            state_fields={"task_stage": "reviews"},
            reason="done",
        ) is True
    verdict = store.evaluate_canary(batch_id)
    assert verdict is not None and verdict["canary_passed"] is True
    assert _batch_status(batch_id) == "running"
    # 放开后非探针成员可领
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None and task["asin"] == "B000TEST03"


def test_canary_fail_keeps_batch_and_finalize_cancels_rest(store):
    """探针终态失败 → passed=False 批次保持 canary；收尾取消残余成员。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 探针 1：成功
    t1 = store.claim_task("worker-a", batch_id=batch_id)
    store.save_product_result(
        task=t1,
        evidence=_evidence(run_id, t1["url"]),
        product={"title": "探针商品", "status": "product_done"},
        media=[], content_modules=[],
        review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
        next_status="succeeded",
        state_fields={"task_stage": "reviews"},
        reason="done",
    )
    # 探针 2：被拦终态（不可重试：attempts 用满）
    t2 = store.claim_task("worker-a", batch_id=batch_id)
    ev = _evidence(run_id, t2["url"])
    ev["block_reason"] = "robot"
    store.save_failure(
        task=t2, reason="robot_check", error="robot", evidence=ev,
        next_status="blocked",
    )
    # 探针 2 是 blocked 终态 → 评估给失败结论，批次保持 canary
    verdict = store.evaluate_canary(batch_id)
    assert verdict is not None and verdict["canary_passed"] is False
    assert _batch_status(batch_id) == "canary"
    # 收尾（force：canary 失败专用，未被用户停止也取消残余）
    summary = store.finalize_batch(batch_id, force=True)
    assert summary["batch_status"] == "blocked"
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM amazon_us.batch_item WHERE batch_id=%s AND asin='B000TEST03'",
                (batch_id,),
            )
            assert cur.fetchone()[0] == "cancelled"


def test_canary_stop_request_transitions_to_stopping(store):
    """canary 阶段用户停止 → 批次转 stopping 且领不到新任务。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    assert store.request_stop(batch_id) is True
    assert _batch_status(batch_id) == "stopping"
    # 停止后 worker 领不到任务（含探针）
    assert store.claim_task("worker-a", batch_id=batch_id) is None
    summary = store.finalize_batch(batch_id)
    assert summary["batch_status"] == "stopped"


def _defer_captcha(store, task, run_id, retry_seconds=120):
    """模拟代理轮换模式的延迟重试写法：被拦 → failed + 可重试 + next_retry 在未来。"""
    from datetime import datetime, timedelta, timezone
    ev = _evidence(run_id, task["url"])
    ev["block_reason"] = "captcha"
    next_retry = (datetime.now(timezone.utc) + timedelta(seconds=retry_seconds)).replace(microsecond=0).isoformat()
    assert store.save_failure(
        task=task, reason="captcha", error="captcha", evidence=ev,
        next_status="pending",
        state_fields={"task_stage": "product", "resume_status": "pending",
                      "next_retry_at": next_retry, "block_reason": "captcha"},
        increment_attempts=True,
    ) is True


def test_canary_deferred_retry_keeps_batch_alive(store):
    """回归（2026-09-09 连杀两批 937 条事故）：探针被拦走延迟重试
    （failed + attempts<max + next_retry_at 在未来）时，evaluate_canary
    必须返回 None 继续等。旧判定把"等待重试"排除在 retryable 之外，
    探针在 120s 重试等待窗口内既非 pending/running 也非 retryable →
    被误判全部终态 → canary 假失败 → 整批被 force 取消。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 两个探针都被 captcha 拦，走延迟重试
    for _ in range(2):
        task = store.claim_task("worker-a", batch_id=batch_id)
        assert task is not None
        _defer_captcha(store, task, run_id)
    # 死亡窗口：两个探针都在等重试（next_retry 在未来）→ 必须继续等
    assert store.evaluate_canary(batch_id) is None
    assert _batch_status(batch_id) == "canary"
    # 批次未被取消：非探针成员未被 force 收尾
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM amazon_us.batch_item WHERE batch_id=%s AND asin='B000TEST03'",
                (batch_id,),
            )
            assert cur.fetchone()[0] == "pending"


def test_finalize_waits_for_deferred_retries(store):
    """回归：批次尾段只剩等待重试的 failed（无 pending、无活租约）时，
    finalize_batch 不得收尾——重试到期后协调器会重拉 worker 跑完剩余
    attempts。旧逻辑只检查 pending，failed+可重试被无视 → 批次尾段
    被 prematurely 判成终态。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 两个探针全成功 → canary 通过转 running
    for _ in range(2):
        task = store.claim_task("worker-a", batch_id=batch_id)
        assert task is not None
        assert store.save_product_result(
            task=task,
            evidence=_evidence(run_id, task["url"]),
            product={"title": "商品", "price": "$1.00", "status": "product_done"},
            media=[], content_modules=[],
            review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
            next_status="succeeded",
            state_fields={"task_stage": "reviews"},
            reason="done",
        ) is True
    verdict = store.evaluate_canary(batch_id)
    assert verdict is not None and verdict["canary_passed"] is True
    # 最后一个成员被拦走延迟重试（attempts=1<3，next_retry 在未来）
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None and task["asin"] == "B000TEST03"
    _defer_captcha(store, task, run_id)
    # 无 pending、无活租约，但有等待重试的 failed → 不收尾
    assert store.finalize_batch(batch_id) is None
    assert _batch_status(batch_id) == "running"
    # 模拟 120s 过去：重试到期可再领 → 第二次被拦（attempts=2<3 仍可重试）→ 仍不收尾
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE amazon_us.batch_item SET next_retry_at=CURRENT_TIMESTAMP - INTERVAL '1 second' "
                "WHERE batch_id=%s AND asin='B000TEST03'",
                (batch_id,),
            )
        conn.commit()
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None and task["asin"] == "B000TEST03"
    _defer_captcha(store, task, run_id)
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempts, max_attempts FROM amazon_us.batch_item "
                "WHERE batch_id=%s AND asin='B000TEST03'",
                (batch_id,),
            )
            attempts, max_attempts = cur.fetchone()
    assert attempts == 2 and attempts < max_attempts
    assert store.finalize_batch(batch_id) is None
    # attempts 用满（终态 blocked）后才可收尾：2 成功 + 1 blocked → blocked
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE amazon_us.batch_item SET attempts=max_attempts, status='blocked' "
                "WHERE batch_id=%s AND asin='B000TEST03'",
                (batch_id,),
            )
        conn.commit()
    summary = store.finalize_batch(batch_id)
    assert summary["batch_status"] == "blocked"


# ---------------------------------------------------------------------
# 8. 变体跳转：专门终态，不算失败也不算原商品成功
# ---------------------------------------------------------------------
def test_variant_redirect_terminal_state(store):
    """变体跳转 → variant 终态：记录实际 ASIN、attempts 置满、不可重领。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    task = store.claim_task("worker-a", batch_id=batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    assert store.save_failure(
        task=task, reason="variant_redirect", error="variant_redirect",
        evidence=_evidence(run_id, task["url"]),
        terminal=False,                    # 变体不走 terminal（terminal 会强制 failed）
        variant_asin="B0VARIENT1",
    ) is True
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, variant_asin, attempts, error_class "
                "FROM amazon_us.batch_item WHERE batch_id=%s AND asin=%s",
                (batch_id, task["asin"]),
            )
            status, variant_asin, attempts, error_class = cur.fetchone()
    assert status == "variant"             # 专门终态，不是 failed
    assert variant_asin == "B0VARIENT1"    # 记录实际跳转到的 ASIN
    assert attempts >= task["max_attempts"]  # 置满：重试结果相同，不允许重领
    assert error_class is None             # 不算失败：无失败分类
    # 变体成员不可再领取：再领只能领到另一个 pending 成员，领完就 None
    other = store.claim_task("worker-a", batch_id=batch_id)
    assert other is not None and other["asin"] != task["asin"]
    assert store.claim_task("worker-a", batch_id=batch_id) is None


def test_variant_does_not_break_completion(store):
    """变体 + 成功混合 → 批次 completed：变体不算失败，完成度按 成功+变体 计。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 成员 1：变体跳转
    t1 = store.claim_task("worker-a", batch_id=batch_id)
    store.save_failure(
        task=t1, reason="variant_redirect", error="variant_redirect",
        evidence=_evidence(run_id, t1["url"]), variant_asin="B0VARIENT1",
    )
    # 成员 2：正常成功
    t2 = store.claim_task("worker-a", batch_id=batch_id)
    store.save_product_result(
        task=t2,
        evidence=_evidence(run_id, t2["url"]),
        product={"title": "正常商品", "price": "$5.00", "status": "product_done"},
        media=[], content_modules=[],
        review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
        next_status="succeeded",
        state_fields={"task_stage": "reviews"},
        reason="done",
    )
    summary = store.finalize_batch(batch_id)
    # 变体不算失败 → 批次整体 completed（而不是 failed）
    assert summary["batch_status"] == "completed"
    assert summary["succeeded"] == 1
    assert summary["variant"] == 1


def test_variant_canary_probe_counts_as_effective(store):
    """canary 探针成员是变体跳转 → 评估照样通过（出口是通的）。"""
    result = store.create_batch(_rows("B000TEST01", "B000TEST02", "B000TEST03"))
    batch_id = result["batch_id"]
    store.start_batch(batch_id)
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    # 两个探针：一个变体、一个成功
    for _ in range(2):
        task = store.claim_task("worker-a", batch_id=batch_id)
        assert task is not None
        if task["asin"] == "B000TEST01":
            store.save_failure(
                task=task, reason="variant_redirect", error="variant_redirect",
                evidence=_evidence(run_id, task["url"]), variant_asin="B0VARIENT1",
            )
        else:
            store.save_product_result(
                task=task,
                evidence=_evidence(run_id, task["url"]),
                product={"title": "探针商品", "status": "product_done"},
                media=[], content_modules=[],
                review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
                next_status="succeeded",
                state_fields={"task_stage": "reviews"},
                reason="done",
            )
    verdict = store.evaluate_canary(batch_id)
    # 变体探针 = 出口有效（页面正常抓到并解析），canary 照样通过
    assert verdict is not None and verdict["canary_passed"] is True
    assert _batch_status(batch_id) == "running"
    # 放开后非探针成员可领
    task = store.claim_task("worker-a", batch_id=batch_id)
    assert task is not None and task["asin"] == "B000TEST03"

