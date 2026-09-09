# -*- coding: utf-8 -*-
"""统一生命周期协调器：容量、启动、停止、清理、最终状态的唯一负责人。

设计规则（对应架构调整的三条原则）：
1. 单一写者：批次终态、run 终态只有协调器能写；worker 只写成员结果。
2. 协调器自身用 PostgreSQL 咨询锁保证全局唯一（连接断开锁自动释放，
   崩溃后新协调器可以直接接管，无需人工解锁）。
3. 恢复是启动流程的一部分：每次启动先统一回收（过期租约/过期槽/
   孤儿 run），再开工作——幂等启动天然覆盖崩溃恢复。

生命周期：
    启动 → 抢协调器锁 → 恢复清理
         → 循环：管理批次（启动 worker / 监控 / 收尾）
         → 退出信号 → 停 worker → 释放资源（批次保持原状，重启后继续）

用法：
    python batch_coordinator.py run                          # 常驻协调
    python batch_coordinator.py create-batch --manifest x.csv # 上传清单
    python batch_coordinator.py stop --batch-id <uuid>        # 停止批次
    python batch_coordinator.py status [---batch-id <uuid>]   # 查看进度
"""
from __future__ import annotations

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from batch_store import BatchStore, BatchStoreError  # noqa: E402

WORKER_SCRIPT = ROOT / "scripts" / "amazon_us_worker.py"
# 协调器咨询锁 key（int64，任意固定值；一个部署一个）
COORDINATOR_LOCK_KEY = 82024001
# 停止批次时等待 worker 优雅退出的宽限期（秒）
STOP_GRACE_SECONDS = 30
# 同一批次 worker 崩溃重启的次数上限（防死循环）
MAX_WORKER_RESTARTS = 20
# worker 退出后重新 spawn 的最小间隔（秒）：防止快速崩溃循环在几十秒内
# 烧光 MAX_WORKER_RESTARTS 上限（例如熔断门让 worker 立即退出时曾 50 秒重启 20 次）
WORKER_RESPAWN_MIN_INTERVAL = 30.0
# 同批次多 worker 错峰上线间隔（秒）：4 worker 同秒启动=同指纹集群爆发，
# 亚马逊按指纹集群标记 → 全员被拦烧光轮换预算（2026-09-09 12:16 实测 3/4
# worker 零成功）；错峰 75s 让亚马逊看到的是不同时刻上线的独立用户
WORKER_START_STAGGER_SECONDS = 75.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str) -> None:
    print(f"[{_now()}] {message}", flush=True)


class BatchCoordinator:
    """批次生命周期的唯一管理者。"""

    def __init__(
        self,
        dsn: str,
        tenant_id: str,
        subject_type: str = "own",
        *,
        workers_per_batch: int = 2,
        poll_seconds: float = 2.0,
        dsn_env: str = "AMAZON_US_POSTGRES_DSN",
        worker_script: Path | None = None,
        worker_config: Path | None = None,
    ) -> None:
        self.dsn = dsn
        self.tenant_id = tenant_id
        self.subject_type = subject_type
        self.workers_per_batch = max(1, int(workers_per_batch))
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.dsn_env = dsn_env
        # worker 脚本可替换（隔离测试用模拟 worker，不访问网络）
        self.worker_script = Path(worker_script) if worker_script else WORKER_SCRIPT
        # worker 运行配置（限速/jitter/会话休息等）；不传则 worker 用其默认配置
        self.worker_config = Path(worker_config) if worker_config else None
        self.store = BatchStore(dsn, tenant_id, subject_type)
        self.coordinator_id = uuid.uuid4()
        # 活跃 worker 子进程：{proc: {"batch_id":..., "run_id":..., "restarts":...}}
        self.workers: dict[subprocess.Popen, dict[str, Any]] = {}
        # 每批次的累计启动次数（防崩溃死循环；协调器重启后归零，可接受）
        self._spawn_counts: dict[str, int] = {}
        # 每批次最近一次 spawn 时间（重启退避用）
        self._last_spawn_at: dict[str, float] = {}
        self._stop_requested = False  # 协调器自身的退出标志
        self._lock_conn = None        # 持有咨询锁的连接

    # ------------------------------------------------------------------
    # 协调器单例：咨询锁
    # ------------------------------------------------------------------
    def acquire_lock(self) -> bool:
        """抢全局唯一锁；抢不到说明已有协调器在跑，直接退出。"""
        conn = psycopg.connect(self.dsn)
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (COORDINATOR_LOCK_KEY,))
            ok = bool(cur.fetchone()[0])
        if not ok:
            conn.close()
            return False
        self._lock_conn = conn  # 连接存活期间持锁；进程崩溃连接断开锁自动释放
        return True

    # ------------------------------------------------------------------
    # 启动恢复：幂等清理
    # ------------------------------------------------------------------
    def recover(self) -> dict[str, int]:
        """启动时统一恢复：过期租约、过期槽、孤儿协调器标记。"""
        result = {
            "items_reclaimed": self.store.reclaim_expired_items(),
            "slots_reclaimed": self.store.reclaim_expired_slots(),
        }
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                # 上一个协调器没写 stopped → 标记 crashed（心跳超时的）
                cur.execute(
                    """
                    UPDATE amazon_us.coordinator_state
                    SET status='crashed'
                    WHERE status='running'
                    """
                )
                result["coordinators_marked_crashed"] = cur.rowcount
                # 孤儿 run：running 状态但对应 worker 进程早已不在
                # （简单判定：超过 1 小时没更新且无活租约）
                cur.execute(
                    """
                    UPDATE amazon_us.collection_run
                    SET status='interrupted',
                        termination_reason='coordinator_recovery',
                        finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE status IN ('starting','running')
                      AND updated_at < CURRENT_TIMESTAMP - INTERVAL '1 hour'
                    """
                )
                result["runs_interrupted"] = cur.rowcount
            conn.commit()
        return result

    def heartbeat(self) -> None:
        """更新协调器心跳（页面判断协调器活着的依据）。"""
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amazon_us.coordinator_state
                      (coordinator_id, host, pid, advisory_lock_key, status, heartbeat_at)
                    VALUES (%s, %s, %s, %s, 'running', CURRENT_TIMESTAMP)
                    ON CONFLICT (coordinator_id) DO UPDATE SET
                      heartbeat_at=CURRENT_TIMESTAMP, status='running'
                    """,
                    (self.coordinator_id, os.environ.get("COMPUTERNAME", "unknown"),
                     os.getpid(), COORDINATOR_LOCK_KEY),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # worker 子进程管理
    # ------------------------------------------------------------------
    def spawn_worker(self, batch_id: str, restart_count: int) -> subprocess.Popen:
        """启动一个 worker 子进程服务指定批次。"""
        worker_id = f"batch-worker-{batch_id[:8]}-{os.getpid()}-{restart_count}"
        run_id = f"run-{batch_id[:8]}-{uuid.uuid4().hex[:6]}"
        env = dict(os.environ)
        env[self.dsn_env] = self.dsn
        cmd = [
            sys.executable, str(self.worker_script),
            "--backend", "postgres",
            "--batch-id", batch_id,
            "--live",
            "--tenant-id", self.tenant_id,
            "--subject-type", self.subject_type,
            "--worker-id", worker_id,
            "--run-id", run_id,
        ]
        if self.worker_config:
            cmd.extend(["--config", str(self.worker_config)])
        proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
        self.workers[proc] = {
            "batch_id": batch_id, "run_id": run_id,
            "worker_id": worker_id, "restarts": restart_count,
        }
        self._spawn_counts[batch_id] = self._spawn_counts.get(batch_id, 0) + 1
        self._last_spawn_at[batch_id] = time.monotonic()
        # 记录 collection_run（带 batch_id）
        self._write_run_start(batch_id, run_id, worker_id, proc.pid)
        _log(f"worker 启动: batch={batch_id[:8]} pid={proc.pid} run={run_id} 重启次数={restart_count}")
        return proc

    def _write_run_start(self, batch_id: str, run_id: str, worker_id: str, pid: int) -> None:
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO amazon_us.collection_run
                      (tenant_id, run_id, command, requested_actions, status,
                       worker_id, controller_pid, batch_id)
                    VALUES (%s, %s, %s, 1, 'running', %s, %s, %s)
                    ON CONFLICT (tenant_id, run_id) DO UPDATE SET
                      status='running', updated_at=CURRENT_TIMESTAMP
                    """,
                    (self.tenant_id, run_id, "batch-worker", worker_id, pid, batch_id),
                )
            conn.commit()

    def _write_run_finish(self, run_id: str, exit_code: int) -> None:
        # Windows 强杀（TerminateProcess）的退出码是无符号 DWORD（如 4294967295），
        # 直接写 Postgres integer 列会 NumericValueOutOfRange 崩掉协调器——钳到 int32
        exit_code = max(-2147483648, min(2147483647, int(exit_code)))
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE amazon_us.collection_run
                    SET status=CASE WHEN %s=0 THEN 'completed' ELSE 'failed' END,
                        controller_exit_code=%s, worker_exit_code=%s,
                        termination_reason='worker_exited',
                        finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=%s AND run_id=%s
                    """,
                    (exit_code, exit_code, exit_code, self.tenant_id, run_id),
                )
            conn.commit()

    def reap_workers(self) -> None:
        """收割已退出的 worker：写 run 终态 + 清理进程表。"""
        for proc in list(self.workers):
            code = proc.poll()
            if code is None:
                continue
            info = self.workers.pop(proc)
            self._write_run_finish(info["run_id"], code)
            _log(f"worker 退出: batch={info['batch_id'][:8]} run={info['run_id']} code={code}")

    def stop_batch_workers(self, batch_id: str) -> None:
        """停止指定批次的所有 worker：先等宽限期，超时强杀。

        杀完后立即释放这些 worker 持有的成员租约——
        Windows 下进程被杀时收尾代码不会执行，不主动释放的话
        批次收尾要干等整个租约期。
        """
        targets = [(p, i) for p, i in self.workers.items() if i["batch_id"] == batch_id]
        if not targets:
            return
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        for proc, _ in targets:
            if proc.poll() is None:
                proc.terminate()  # worker 循环里 claim 失败即退出，通常很快
        killed_ids: list[str] = []
        for proc, info in targets:
            remaining = deadline - time.monotonic()
            try:
                proc.wait(timeout=max(0.1, remaining))
            except subprocess.TimeoutExpired:
                proc.kill()
                _log(f"worker 强杀: batch={batch_id[:8]} pid={proc.pid}")
            self.workers.pop(proc, None)
            self._write_run_finish(info["run_id"], proc.returncode if proc.returncode is not None else -1)
            killed_ids.append(str(info["worker_id"]))
        # 进程已死，立即还租约（否则 finalize 见活租约会拒绝收尾）
        released = self.store.release_worker_leases(batch_id, killed_ids)
        if released:
            _log(f"已释放 {released} 个被杀 worker 持有的成员租约")

    # ------------------------------------------------------------------
    # 批次管理主逻辑
    # ------------------------------------------------------------------
    def _active_batches(self) -> list[dict[str, Any]]:
        """查询需要管理的批次（pending/canary/running/stopping）。"""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT batch_id, status, stop_requested, requested_count
                    FROM amazon_us.batch
                    WHERE tenant_id=%s AND marketplace='US'
                      AND status IN ('pending','canary','running','stopping')
                    ORDER BY created_at
                    """,
                    (self.tenant_id,),
                )
                return [dict(r) for r in cur.fetchall()]

    def _claimable_count(self, batch_id: str) -> int:
        """批次内当前可立即领取的成员数（pending 或 到期可重试的 failed）。"""
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM amazon_us.batch_item
                    WHERE batch_id=%s
                      AND (status='pending'
                           OR (status='failed' AND attempts<max_attempts
                               AND (next_retry_at IS NULL OR next_retry_at<=CURRENT_TIMESTAMP)))
                      AND (lease_expires_at IS NULL OR lease_expires_at<=CURRENT_TIMESTAMP)
                    """,
                    (batch_id,),
                )
                return int(cur.fetchone()[0])

    def _alive_worker_count(self, batch_id: str) -> int:
        return sum(1 for i in self.workers.values() if i["batch_id"] == batch_id)

    def _respawn_cooldown_remaining(self, batch_id: str) -> float:
        """距该批次最近一次 spawn 的剩余退避秒数（0 表示可立即 spawn）。"""
        last = self._last_spawn_at.get(batch_id)
        if last is None:
            return 0.0
        return max(0.0, WORKER_RESPAWN_MIN_INTERVAL - (time.monotonic() - last))

    def manage_once(self) -> None:
        """一轮批次管理：推进生命周期 + 补充 worker + 尝试收尾。"""
        for batch in self._active_batches():
            batch_id = str(batch["batch_id"])
            status = batch["status"]
            # 先看全局可用槽位——没槽时所有批次都不要 spawn 新 worker
            free_slots = self.store.count_free_slots() if hasattr(self.store, "count_free_slots") else 999
            # 1) pending → 启动（有容量槽才 spawn，否则等下一轮）
            if status == "pending":
                if free_slots <= 0:
                    _log(f"无可用容量槽，批次 {batch_id[:8]} 等下一轮")
                    continue
                self.store.start_batch(batch_id)
                _log(f"批次启动（canary 预检）: {batch_id[:8]}（{batch['requested_count']} 个成员）")
                self.spawn_worker(batch_id, 0)
                continue
            # 2) canary → 评估探针：通过转 running 放开全量；失败整批收尾
            if status == "canary":
                verdict = self.store.evaluate_canary(batch_id)
                if verdict is None:
                    # 探针还有成员没跑完：保持 worker 存活（死了且有活干才补）
                    alive = self._alive_worker_count(batch_id)
                    if alive == 0 and self._claimable_count(batch_id) > 0:
                        spawned = self._spawn_counts.get(batch_id, 0)
                        if spawned < MAX_WORKER_RESTARTS and free_slots > 0:
                            if self._respawn_cooldown_remaining(batch_id) > 0:
                                continue
                            self.spawn_worker(batch_id, spawned)
                    continue
                if verdict.get("canary_passed"):
                    # 探针全部成功：批次已转 running，下一轮按 running 补足 worker
                    _log(f"canary 通过，放开全量: {batch_id[:8]}（探针 {verdict.get('canary_total')} 个全成功）")
                    continue
                # 探针有终态失败：整批不放开，停 worker 后收尾（残余成员取消）
                _log(f"canary 未通过（{verdict.get('canary_succeeded')}/{verdict.get('canary_total')}），"
                     f"批次收尾: {batch_id[:8]}")
                if self._alive_worker_count(batch_id) > 0:
                    self.stop_batch_workers(batch_id)
                    continue
                # force=True：批次未被用户停止但全量不能放开，残余 pending 一律取消，
                # 终态按探针失败构成写 blocked/failed（不写 stopped）
                summary = self.store.finalize_batch(batch_id, force=True)
                if summary:
                    _log(f"批次收尾: {batch_id[:8]} → {summary['batch_status']}")
                continue
            # 3) stopping → 停 worker → 收尾
            if status == "stopping":
                if self._alive_worker_count(batch_id) > 0:
                    self.stop_batch_workers(batch_id)
                    continue
                summary = self.store.finalize_batch(batch_id)
                if summary:
                    _log(f"批次收尾: {batch_id[:8]} → {summary['batch_status']}")
                continue
            # 3) running → 补 worker / 尝试收尾
            alive = self._alive_worker_count(batch_id)
            if alive == 0:
                # 没有活 worker：有可领任务才重启（防止空转）
                if self._claimable_count(batch_id) > 0:
                    spawned = self._spawn_counts.get(batch_id, 0)
                    if spawned < MAX_WORKER_RESTARTS:
                        if free_slots <= 0:
                            _log(f"无可用容量槽，批次 {batch_id[:8]} 等下一轮")
                            continue
                        if self._respawn_cooldown_remaining(batch_id) > 0:
                            continue
                        self.spawn_worker(batch_id, spawned)
                    else:
                        _log(f"批次启动次数耗尽（{spawned}），转停止: {batch_id[:8]}")
                        self.store.request_stop(batch_id)
                        continue
                else:
                    # 没有可领任务：尝试收尾（有未到期重试时 finalize 返回 None）
                    summary = self.store.finalize_batch(batch_id)
                    if summary:
                        _log(f"批次收尾: {batch_id[:8]} → {summary['batch_status']}")
            elif alive < self.workers_per_batch and self._claimable_count(batch_id) > alive:
                # 有活 worker 但不满额且还有富余任务 → 补一个
                if free_slots <= 0:
                    continue
                # 错峰上线：批次已有活 worker 时，距上次 spawn 需间隔 WORKER_START_STAGGER_SECONDS
                if alive > 0 and (time.monotonic() - self._last_spawn_at.get(batch_id, 0.0)) < WORKER_START_STAGGER_SECONDS:
                    continue
                self.spawn_worker(batch_id, self._spawn_counts.get(batch_id, 0))

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run_forever(self) -> int:
        """常驻主循环：心跳 → 回收 → 管理，收到退出信号后优雅收尾。"""
        if not self.acquire_lock():
            _log("已有协调器在运行（咨询锁被占用），本进程退出。")
            return 0
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)
        recovered = self.recover()
        _log(f"协调器启动: id={str(self.coordinator_id)[:8]} 恢复={recovered}")
        try:
            while not self._stop_requested:
                self.heartbeat()
                self.store.reclaim_expired_items()
                self.store.reclaim_expired_slots()
                self.reap_workers()
                self.manage_once()
                time.sleep(self.poll_seconds)
        finally:
            self._shutdown()
        return 0

    def _handle_signal(self, signum: int, frame: Any) -> None:
        _log(f"收到退出信号 {signum}，开始优雅收尾……")
        self._stop_requested = True

    def _shutdown(self) -> None:
        """协调器退出：停掉所有 worker，标记状态，释放锁。

        注意：批次不 finalize（保持 running），下次启动继续管——
        协调器退出不等于用户要求停止采集。
        """
        # 按批次分组：杀完 worker 后按批次释放成员租约
        by_batch: dict[str, list[tuple[subprocess.Popen, dict[str, Any]]]] = {}
        for proc, info in list(self.workers.items()):
            by_batch.setdefault(str(info["batch_id"]), []).append((proc, info))
        for batch_id, group in by_batch.items():
            killed_ids: list[str] = []
            for proc, info in group:
                try:
                    proc.terminate()
                    proc.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                self._write_run_finish(info["run_id"], proc.returncode if proc.returncode is not None else -1)
                killed_ids.append(str(info["worker_id"]))
            # 进程已死，立即还租约（下次协调器启动就能接着领，不用等租约过期）
            try:
                self.store.release_worker_leases(batch_id, killed_ids)
            except Exception as exc:  # 退出路径不因清理失败崩溃
                _log(f"释放租约失败（忽略，等过期回收）: {exc}")
        self.workers.clear()
        with psycopg.connect(self.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE amazon_us.coordinator_state
                    SET status='stopped', heartbeat_at=CURRENT_TIMESTAMP
                    WHERE coordinator_id=%s
                    """,
                    (self.coordinator_id,),
                )
            conn.commit()
        if self._lock_conn is not None:
            self._lock_conn.close()  # 连接关闭 → 咨询锁自动释放
        _log("协调器已退出。")


# ----------------------------------------------------------------------
# CLI 子命令
# ----------------------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2
    coordinator = BatchCoordinator(
        dsn, args.tenant_id, args.subject_type,
        workers_per_batch=args.workers_per_batch,
        poll_seconds=args.poll_seconds,
        dsn_env=args.dsn_env,
        worker_script=getattr(args, "worker_script", None),
        worker_config=getattr(args, "worker_config", None),
    )
    return coordinator.run_forever()


def _read_manifest_rows(path: Path) -> list[dict[str, str]]:
    """读上传 CSV：必须含 asin 列；url 列缺省时自动构造。"""
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for raw in csv.DictReader(handle):
            asin = (raw.get("asin") or "").strip().upper()
            if not asin:
                continue
            url = (raw.get("url") or "").strip() or f"https://www.amazon.com/dp/{asin}"
            rows.append({"asin": asin, "url": url})
    if not rows:
        raise ValueError("清单里没有有效的 asin 行")
    return rows


def cmd_create_batch(args: argparse.Namespace) -> int:
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2
    store = BatchStore(dsn, args.tenant_id, args.subject_type)
    try:
        result = store.create_batch(_read_manifest_rows(args.manifest), uploaded_by="cli")
    except (BatchStoreError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    print(f"批次已创建: {result['batch_id']}")
    print(f"成员数: {result['requested_count']}（清单内去重 {result['skipped_duplicates']} 个）")
    print(f"启动协调器后自动开始采集: python scripts/batch_coordinator.py run")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2
    store = BatchStore(dsn, args.tenant_id, args.subject_type)
    if not store.request_stop(args.batch_id):
        print(f"错误: 批次不存在或已是终态: {args.batch_id}", file=sys.stderr)
        return 2
    print(f"已请求停止批次 {args.batch_id}（协调器会等 worker 收尾后写终态）")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    dsn = os.environ.get(args.dsn_env, "").strip()
    if not dsn:
        print(f"错误: 需要环境变量 {args.dsn_env}", file=sys.stderr)
        return 2
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if args.batch_id:
                cur.execute(
                    "SELECT * FROM amazon_us.batch_progress WHERE batch_id=%s",
                    (args.batch_id,),
                )
                rows = [dict(r) for r in cur.fetchall()]
            else:
                cur.execute(
                    """
                    SELECT * FROM amazon_us.batch_progress
                    WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 20
                    """,
                    (args.tenant_id,),
                )
                rows = [dict(r) for r in cur.fetchall()]
            # 协调器心跳
            cur.execute(
                """
                SELECT status, host, pid, heartbeat_at FROM amazon_us.coordinator_state
                WHERE status='running' AND heartbeat_at > CURRENT_TIMESTAMP - INTERVAL '30 seconds'
                """
            )
            coordinator = cur.fetchone()
    for row in rows:
        print(
            f"批次 {row['batch_id']} | {row['batch_status']}"
            f" | 成功 {row['succeeded']}/{row['requested_count']}"
            f" | 被拦 {row['blocked']} 网络 {row['failed_fetch']} 系统 {row['failed_system']}"
            f" | 取消 {row['cancelled']} | 待跑 {row['pending']}"
        )
    if coordinator:
        print(f"协调器在线: {coordinator['host']} pid={coordinator['pid']} 心跳={coordinator['heartbeat_at']}")
    else:
        print("协调器不在线（最近 30 秒无心跳）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="常驻协调器")
    run_parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    run_parser.add_argument("--tenant-id", default="amazon_us_local")
    run_parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    run_parser.add_argument("--workers-per-batch", type=int, default=2, help="每批次并发 worker 数（默认 2）")
    run_parser.add_argument("--poll-seconds", type=float, default=2.0, help="监控轮询间隔秒数")
    run_parser.add_argument("--worker-script", type=Path, help="替换 worker 脚本（隔离测试用模拟 worker）")
    run_parser.add_argument("--worker-config", type=Path, help="worker 运行配置 toml（限速/jitter/会话休息等）")
    run_parser.set_defaults(func=cmd_run)

    create_parser = sub.add_parser("create-batch", help="上传清单创建批次")
    create_parser.add_argument("--manifest", type=Path, required=True)
    create_parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    create_parser.add_argument("--tenant-id", default="amazon_us_local")
    create_parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    create_parser.set_defaults(func=cmd_create_batch)

    stop_parser = sub.add_parser("stop", help="请求停止批次")
    stop_parser.add_argument("--batch-id", required=True)
    stop_parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    stop_parser.add_argument("--tenant-id", default="amazon_us_local")
    stop_parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    stop_parser.set_defaults(func=cmd_stop)

    status_parser = sub.add_parser("status", help="查看批次进度与协调器状态")
    status_parser.add_argument("--batch-id")
    status_parser.add_argument("--dsn-env", default="AMAZON_US_POSTGRES_DSN")
    status_parser.add_argument("--tenant-id", default="amazon_us_local")
    status_parser.add_argument("--subject-type", choices=("own", "competitor", "candidate"), default="own")
    status_parser.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
