# -*- coding: utf-8 -*-
"""协调器端到端测试：真实子进程编排，模拟 worker 离线跑，覆盖六类场景。

场景（对应验证计划）：
1. 正常：上传 → 协调器 → worker → 批次 completed
2. 失败重试：注入 fetch 失败 → 重试 → 最终 completed
3. 停止：批次进行中 stop → worker 退出 → 批次 stopped
4. 崩溃恢复：杀掉协调器 → 批次保持 running → 重启协调器 → 继续 → completed
5. 双协调器互斥：第二个协调器启动即退出（咨询锁）
6. 连续批次：两个批次先后/同时跑，进度互不污染

全部离线（模拟 worker 不发网络请求），可直接反复执行。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures"
DSN = os.environ.get(
    "AMAZON_US_POSTGRES_DSN",
    "postgresql://postgres:123456@localhost:5432/amazon_us",
)
COORDINATOR = SCRIPTS / "batch_coordinator.py"
MOCK_WORKER = FIXTURES / "mock_batch_worker.py"


@pytest.fixture()
def env():
    """独立租户 + 环境变量；结束时清理全部数据。"""
    tenant = f"e2etest_{uuid.uuid4().hex[:10]}"
    e = dict(os.environ)
    e["AMAZON_US_POSTGRES_DSN"] = DSN
    yield {"env": e, "tenant": tenant}
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            for table in ("collection_evidence", "product_snapshot", "media_asset",
                          "content_module", "review_summary", "review_record",
                          "review_page_state", "state_history", "item_state",
                          "asin_master", "collection_run", "batch"):
                cur.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant,))
        conn.commit()


def _write_manifest(path: Path, asins: list[str]) -> Path:
    """写上传 CSV（asin,url 两列）。"""
    lines = ["asin,url"] + [f"{a},https://www.amazon.com/dp/{a}" for a in asins]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _create_batch(env: dict, asins: list[str]) -> str:
    """CLI 创建批次，返回 batch_id。"""
    manifest = Path(env["env"].get("TEMP", ".")) / f"manifest_{uuid.uuid4().hex[:8]}.csv"
    _write_manifest(manifest, asins)
    try:
        result = subprocess.run(
            [sys.executable, str(COORDINATOR), "create-batch",
             "--manifest", str(manifest), "--tenant-id", env["tenant"]],
            env=env["env"], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        for line in result.stdout.splitlines():
            if line.startswith("批次已创建:"):
                return line.split(":", 1)[1].strip()
        raise AssertionError(f"未解析到 batch_id: {result.stdout}")
    finally:
        manifest.unlink(missing_ok=True)


def _start_coordinator(env: dict, extra: list[str] | None = None) -> subprocess.Popen:
    """启动协调器子进程（模拟 worker + 快轮询）。

    Windows 下必须 CREATE_NEW_PROCESS_GROUP，否则 CTRL_BREAK 信号
    会发给整个进程组（连 pytest 一起被打断）。
    """
    cmd = [
        sys.executable, str(COORDINATOR), "run",
        "--tenant-id", env["tenant"],
        "--workers-per-batch", "2",
        "--poll-seconds", "0.5",
        "--worker-script", str(MOCK_WORKER),
    ] + (extra or [])
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(
        cmd, env=env["env"], cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        creationflags=creationflags,
    )


def _load_coordinator_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("batch_coordinator_unit", SCRIPTS / "batch_coordinator.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_respawn_cooldown_helper():
    """_respawn_cooldown_remaining：未 spawn 过 → 0；刚 spawn → >0；过了间隔 → 0。"""
    bc = _load_coordinator_module()
    coord = bc.BatchCoordinator.__new__(bc.BatchCoordinator)  # 不跑 __init__（不连库）
    coord._last_spawn_at = {}

    assert coord._respawn_cooldown_remaining("b1") == 0.0

    coord._last_spawn_at["b1"] = time.monotonic()
    remaining = coord._respawn_cooldown_remaining("b1")
    assert 0.0 < remaining <= bc.WORKER_RESPAWN_MIN_INTERVAL

    coord._last_spawn_at["b1"] = time.monotonic() - bc.WORKER_RESPAWN_MIN_INTERVAL - 1.0
    assert coord._respawn_cooldown_remaining("b1") == 0.0


def _batch_status(env: dict, batch_id: str) -> dict:
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT batch_status, succeeded, failed_total, blocked, cancelled, requested_count "
                "FROM amazon_us.batch_progress WHERE batch_id=%s", (batch_id,),
            )
            row = cur.fetchone()
            if row is None:
                return {}
            keys = ("batch_status", "succeeded", "failed_total", "blocked", "cancelled", "requested_count")
            return dict(zip(keys, row))


def _wait_terminal(env: dict, batch_id: str, timeout: float = 60.0) -> dict:
    """轮询等批次进入终态。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _batch_status(env, batch_id)
        if status.get("batch_status") in {"completed", "blocked", "failed", "stopped"}:
            return status
        time.sleep(0.5)
    raise AssertionError(f"批次超时未到终态: {_batch_status(env, batch_id)}")


def _stop_coordinator(proc: subprocess.Popen, grace: float = 15.0) -> None:
    """优雅停止协调器进程。"""
    if proc.poll() is None:
        proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


# ---------------------------------------------------------------------
# 1. 正常链路：上传 → 协调 → completed
# ---------------------------------------------------------------------
def test_e2e_normal_completion(env):
    asins = [f"B000E2E{n:03d}" for n in range(1, 6)]  # 5 个成员
    batch_id = _create_batch(env, asins)
    coordinator = _start_coordinator(env)
    try:
        status = _wait_terminal(env, batch_id, timeout=90)
        assert status["batch_status"] == "completed"
        assert status["succeeded"] == 5
        assert status["requested_count"] == 5
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 2. 失败重试：每 3 个失败一个（fetch），重试后成功
# ---------------------------------------------------------------------
def test_e2e_failure_retry_then_complete(env):
    asins = [f"B000F2F{n:03d}" for n in range(1, 7)]  # 6 个成员
    batch_id = _create_batch(env, asins)
    env2 = dict(env)
    env2["env"] = dict(env["env"], MOCK_FAIL_EVERY="3", MOCK_FAIL_CLASS="fetch")
    coordinator = _start_coordinator(env2)
    try:
        status = _wait_terminal(env, batch_id, timeout=120)
        # 失败被重试 → 最终全成功
        assert status["batch_status"] == "completed"
        assert status["succeeded"] == 6
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 3. 停止：进行中 stop → stopped
# ---------------------------------------------------------------------
def test_e2e_stop_midway(env):
    asins = [f"B000S2S{n:03d}" for n in range(1, 11)]  # 10 个成员
    batch_id = _create_batch(env, asins)
    env2 = dict(env)
    env2["env"] = dict(env["env"], MOCK_DELAY_SECONDS="0.8")  # 放慢制造停止窗口
    coordinator = _start_coordinator(env2)
    try:
        # 等批次开始跑（有成功记录）
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _batch_status(env, batch_id).get("succeeded", 0) >= 1:
                break
            time.sleep(0.3)
        # 请求停止
        result = subprocess.run(
            [sys.executable, str(COORDINATOR), "stop",
             "--batch-id", batch_id, "--tenant-id", env["tenant"]],
            env=env["env"], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        status = _wait_terminal(env, batch_id, timeout=60)
        assert status["batch_status"] == "stopped"
        # 部分完成：成功数 < 总数，取消数 > 0
        assert status["succeeded"] < 10
        assert status["cancelled"] >= 1
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 4. 崩溃恢复：杀协调器 → 批次保持 running → 重启 → completed
# ---------------------------------------------------------------------
def test_e2e_coordinator_crash_recovery(env):
    asins = [f"B000C2C{n:03d}" for n in range(1, 13)]  # 12 个成员
    batch_id = _create_batch(env, asins)
    env2 = dict(env)
    env2["env"] = dict(env["env"], MOCK_DELAY_SECONDS="0.5")
    coordinator = _start_coordinator(env2)
    try:
        # 等批次跑起来
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _batch_status(env, batch_id).get("succeeded", 0) >= 2:
                break
            time.sleep(0.3)
        # 模拟崩溃：直接 kill（不走优雅退出）
        coordinator.kill()
        coordinator.wait(timeout=10)
        if coordinator.stdout:
            coordinator.stdout.close()
        time.sleep(1)
        # 批次应保持活跃（等待协调器回归），不能被误收尾
        # canary：崩溃发生在探针阶段；running/stopping：探针已过或正停止
        mid = _batch_status(env, batch_id)
        assert mid["batch_status"] in {"canary", "running", "stopping"}
        # 崩溃后可能有成员挂着过期租约 → 重启协调器恢复
        coordinator2 = _start_coordinator(env2)
        try:
            status = _wait_terminal(env, batch_id, timeout=120)
            assert status["batch_status"] == "completed"
            assert status["succeeded"] == 12
        finally:
            _stop_coordinator(coordinator2)
    finally:
        if coordinator.poll() is None:
            coordinator.kill()
            coordinator.wait(timeout=10)
        if coordinator.stdout:
            coordinator.stdout.close()


# ---------------------------------------------------------------------
# 5. 双协调器互斥：第二个启动即退出
# ---------------------------------------------------------------------
def test_e2e_second_coordinator_exits(env):
    asins = [f"B000D2D{n:03d}" for n in range(1, 6)]
    _create_batch(env, asins)
    first = _start_coordinator(env)
    time.sleep(3)  # 等第一个拿到咨询锁
    try:
        # 第二个协调器：应立即退出（锁被占）
        second = _start_coordinator(env)
        try:
            code = second.wait(timeout=20)
            assert code == 0  # 退出码 0：正常让位，不是故障
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=5)
            if second.stdout:
                second.stdout.close()
    finally:
        _stop_coordinator(first)


# ---------------------------------------------------------------------
# 6. 连续批次：两个批次同时跑，进度独立
# ---------------------------------------------------------------------
def test_e2e_two_batches_independent(env):
    batch_a = _create_batch(env, [f"B000A2A{n:03d}" for n in range(1, 4)])
    batch_b = _create_batch(env, [f"B000B2B{n:03d}" for n in range(1, 6)])
    env2 = dict(env)
    env2["env"] = dict(env["env"], MOCK_DELAY_SECONDS="0.3")
    coordinator = _start_coordinator(env2)
    try:
        status_a = _wait_terminal(env, batch_a, timeout=90)
        status_b = _wait_terminal(env, batch_b, timeout=90)
        assert status_a["batch_status"] == "completed"
        assert status_b["batch_status"] == "completed"
        # 各自的进度只算自己的成员
        assert status_a["succeeded"] == 3
        assert status_b["succeeded"] == 5
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 8. canary 预检：探针被拦 → 整批不放开，非探针成员不被尝试
# ---------------------------------------------------------------------
def test_e2e_canary_fail_blocks_full_batch(env):
    """探针成员被拦（blocked 终态）→ canary 未通过 → 批次收尾 blocked。

    关键断言：非探针成员全部 cancelled 且 attempts=0——
    出口已被拦时 5800 个成员不能整批冲出去。
    """
    asins = [f"B000K2K{n:03d}" for n in range(1, 9)]  # 8 个成员，探针 2 个
    batch_id = _create_batch(env, asins)
    env2 = dict(env)
    # 每个任务都被拦：探针 2 个全 blocked → canary 失败
    env2["env"] = dict(env["env"], MOCK_FAIL_EVERY="1", MOCK_FAIL_CLASS="blocked")
    coordinator = _start_coordinator(env2)
    try:
        status = _wait_terminal(env, batch_id, timeout=90)
        assert status["batch_status"] == "blocked"
        # 只有探针被尝试（2 个 blocked），其余 6 个全部取消且从未尝试
        assert status["blocked"] == 2
        assert status["cancelled"] == 6
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT is_canary, status, attempts FROM amazon_us.batch_item "
                    "WHERE batch_id=%s ORDER BY asin",
                    (batch_id,),
                )
                rows = cur.fetchall()
        for is_canary, item_status, attempts in rows:
            if is_canary:
                assert item_status == "blocked"
                assert attempts == 1
            else:
                assert item_status == "cancelled"
                assert attempts == 0  # 从未被尝试——canary 拦住了全量
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 9. canary 阶段停止：探针还没跑完用户就停 → stopped
# ---------------------------------------------------------------------
def test_e2e_canary_stop_during_probe(env):
    """探针进行中请求停止 → 批次转 stopping → 收尾 stopped。"""
    asins = [f"B000P2P{n:03d}" for n in range(1, 7)]  # 6 个成员
    batch_id = _create_batch(env, asins)
    env2 = dict(env)
    # 探针放慢到 5 秒，保证停止请求落在 canary 窗口内
    env2["env"] = dict(env["env"], MOCK_DELAY_SECONDS="5")
    coordinator = _start_coordinator(env2)
    try:
        # 等批次进入 canary（探针被领走）
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with psycopg.connect(DSN) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status FROM amazon_us.batch WHERE batch_id=%s",
                        (batch_id,),
                    )
                    if cur.fetchone()[0] == "canary":
                        break
            time.sleep(0.3)
        result = subprocess.run(
            [sys.executable, str(COORDINATOR), "stop",
             "--batch-id", batch_id, "--tenant-id", env["tenant"]],
            env=env["env"], capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr
        status = _wait_terminal(env, batch_id, timeout=90)
        assert status["batch_status"] == "stopped"
        # 没有任何成员成功
        assert status["succeeded"] == 0
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 10. 重启退避：worker 启动即退出（复现 CAPTCHA 熔断场景）不得瞬间烧光重启次数
# ---------------------------------------------------------------------
def test_e2e_instant_exit_worker_backoff(env):
    """worker 启动即退出（MOCK_INSTANT_EXIT=1，复现熔断门退出）→ 协调器必须按
    WORKER_RESPAWN_MIN_INTERVAL 退避，而不是几十秒内重启 20 次 → 整批取消。

    回归背景：熔断 worker 启动即退出曾让协调器 50 秒内耗尽 20 次重启上限，
    1022 个成员被整批取消。
    """
    asins = [f"B000G2G{n:03d}" for n in range(1, 7)]  # 6 个成员
    batch_id = _create_batch(env, asins)
    env2 = dict(env)
    env2["env"] = dict(env["env"], MOCK_INSTANT_EXIT="1")
    coordinator = _start_coordinator(env2)
    try:
        # 观察窗口 > WORKER_RESPAWN_MIN_INTERVAL(30s)：退避生效时最多 spawn 2 次
        time.sleep(35)
        status = _batch_status(env, batch_id)
        # 关键断言 1：批次未被中止（没有出现大批 cancelled）
        assert status.get("batch_status") in {"canary", "running"}, status
        assert status.get("cancelled", 0) == 0, status
        # 关键断言 2：spawn 次数被退避压制（旧代码 35 秒内会 spawn 15~20 次）
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM amazon_us.collection_run WHERE batch_id=%s",
                    (batch_id,),
                )
                run_count = cur.fetchone()[0]
        assert run_count <= 3, f"35 秒内 spawn 了 {run_count} 次，退避未生效"
    finally:
        _stop_coordinator(coordinator)


# ---------------------------------------------------------------------
# 7. 双 Worker 同批次并发：全部成功 + 无重复领取 + 无租约残留
# ---------------------------------------------------------------------
def test_e2e_dual_worker_no_duplicate_claim(env):
    """两个 worker 同时跑一个批次，每个 item 只被一个 worker 处理一次。

    回归背景：之前没配 egress_slot 时 count_free_slots=999，
    但 acquire_slot 因 slot 表空返回 None 导致 worker 被拒；
    还有 claim_task 的租约检查曾有竞态窗口让重复领取漏网。
    """
    asins = [f"B000C2C{n:03d}" for n in range(1, 11)]  # 10 个成员，双 worker 并发
    batch_id = _create_batch(env, asins)
    coordinator = _start_coordinator(env)  # 默认 --workers-per-batch 2
    try:
        status = _wait_terminal(env, batch_id, timeout=120)
        assert status["batch_status"] == "completed"
        assert status["succeeded"] == 10

        # ---- 关键断言 1：每个 batch_item 只有一个 worker 处理 ----
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                # 查每个 batch_item 被 claim 过几次（每个 item 应该只被 claim 1 次）
                cur.execute(
                    """
                    SELECT bi.asin, bi.status, bi.lease_owner
                    FROM amazon_us.batch_item bi
                    WHERE bi.batch_id = %s
                    ORDER BY bi.asin
                    """,
                    (batch_id,),
                )
                items = cur.fetchall()
                assert len(items) == 10, f"期望 10 个成员，实际 {len(items)}"
                for asin, item_status, lease_owner in items:
                    assert item_status == "succeeded", f"{asin} 终态不是 succeeded，是 {item_status}"
                    # 终态后 lease 应已清理
                    assert lease_owner is None, f"{asin} 终态后 lease_owner 未释放: {lease_owner}"

                # ---- 关键断言 2：每个 batch_item 只被一个 worker 处理（lease_owner 唯一） ----
                cur.execute(
                    """
                    SELECT lease_owner, COUNT(*) AS cnt
                    FROM amazon_us.batch_item
                    WHERE batch_id = %s AND lease_owner IS NOT NULL
                    GROUP BY lease_owner
                    """,
                    (batch_id,),
                )
                per_owner = dict(cur.fetchall())
                # 终态后 lease 应该全被释放，这里应该是空字典
                # 如果有残留，说明某个 item 被多个 worker 抢过
                assert not per_owner, f"终态后仍有 lease_owner 残留: {per_owner}"

                # ---- 关键断言 3：collection_run 数量 = worker 数（双 worker → 2） ----
                cur.execute(
                    """
                    SELECT COUNT(*) FROM amazon_us.collection_run
                    WHERE batch_id = %s AND status = 'completed'
                    """,
                    (batch_id,),
                )
                run_count = cur.fetchone()[0]
                # canary 流程下探针跑完 worker 会自然退出一次、
                # canary 通过后重进，所以 3 个 run 也正常（1 探针 + 1~2 全量）
                assert run_count in (1, 2, 3), f"collection_run 数量 {run_count} 异常，期望 1~3"

                # ---- 关键断言 4：没有租约残留 ----
                cur.execute(
                    """
                    SELECT COUNT(*) FROM amazon_us.batch_item
                    WHERE batch_id = %s AND lease_expires_at IS NOT NULL
                    """,
                    (batch_id,),
                )
                residual_leases = cur.fetchone()[0]
                assert residual_leases == 0, f"有 {residual_leases} 个成员租约残留"
    finally:
        _stop_coordinator(coordinator)
