# -*- coding: utf-8 -*-
"""手动端到端验证脚本：建批次 → 协调器 → 等完成 → 打印结果。"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg

ROOT = Path(r"c:\Users\Administrator\Desktop\amazon-crawler-main")
DSN = "postgresql://postgres:123456@localhost:5432/amazon_us"
COORDINATOR = ROOT / "scripts" / "batch_coordinator.py"
MOCK_WORKER = ROOT / "tests" / "fixtures" / "mock_batch_worker.py"

tenant = f"manual_{uuid.uuid4().hex[:8]}"
env = dict(os.environ, AMAZON_US_POSTGRES_DSN=DSN)

# 1. 写清单
manifest = ROOT / f"_tmp_manifest_{uuid.uuid4().hex[:6]}.csv"
manifest.write_text("\n".join(
    ["asin,url"] + [f"B000M2N{n:03d},https://www.amazon.com/dp/B000M2N{n:03d}" for n in range(1, 7)]
), encoding="utf-8")

try:
    # 2. 建批次
    r = subprocess.run(
        [sys.executable, str(COORDINATOR), "create-batch",
         "--manifest", str(manifest), "--tenant-id", tenant],
        env=env, capture_output=True, text=True, timeout=60, cwd=str(ROOT),
    )
    print(r.stdout.strip())
    assert r.returncode == 0, r.stderr
    batch_id = [l for l in r.stdout.splitlines() if l.startswith("批次已创建")][0].split(":")[1].strip()

    # 3. 起协调器（后台，日志写文件避免管道阻塞）
    log = ROOT / f"_tmp_coord_{uuid.uuid4().hex[:6]}.log"
    coord = subprocess.Popen(
        [sys.executable, str(COORDINATOR), "run",
         "--tenant-id", tenant, "--workers-per-batch", "2",
         "--poll-seconds", "0.5", "--worker-script", str(MOCK_WORKER)],
        env=env, cwd=str(ROOT),
        stdout=open(log, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
    )
    print(f"协调器 pid={coord.pid}，日志={log.name}")

    # 4. 等终态
    deadline = time.monotonic() + 90
    final = None
    while time.monotonic() < deadline:
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT batch_status, succeeded, failed_total, requested_count "
                    "FROM amazon_us.batch_progress WHERE batch_id=%s", (batch_id,))
                row = cur.fetchone()
        if row and row[0] in ("completed", "blocked", "failed", "stopped"):
            final = row
            break
        time.sleep(1)
    print("终态:", final)

    # 5. 停协调器
    coord.terminate()
    try:
        coord.wait(timeout=15)
    except subprocess.TimeoutExpired:
        coord.kill()
    print("协调器退出码:", coord.returncode)
    print("---- 协调器日志（最后 15 行）----")
    print("\n".join(log.read_text(encoding="utf-8").splitlines()[-15:]))
    log.unlink(missing_ok=True)
finally:
    manifest.unlink(missing_ok=True)
    # 清理租户
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            for table in ("collection_evidence", "product_snapshot", "media_asset",
                          "content_module", "review_summary", "review_record",
                          "review_page_state", "state_history", "item_state",
                          "asin_master", "collection_run", "batch"):
                cur.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant,))
        conn.commit()
    print("清理完成")
