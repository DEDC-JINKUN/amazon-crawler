# -*- coding: utf-8 -*-
"""冒烟测试：console 批次前端接口完整验证（用完即删）。

验证：
1. /api/batches 返回 items + coordinator 字段
2. /api/batches/{id}/items 返回 reported_review_count 字段
3. 静态页面包含新面板元素
"""
import json
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
DSN = "postgresql://postgres:123456@localhost:5432/amazon_us"
TENANT = f"smoke_{uuid.uuid4().hex[:8]}"
PORT = 18998
ASINS = [f"B00000FE{n:02d}" for n in range(1, 4)]


def main() -> int:
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO amazon_us.batch
                  (tenant_id, marketplace, manifest_hash, requested_count, status, uploaded_by)
                VALUES (%s, 'US', %s, %s, 'running', 'smoke')
                RETURNING batch_id
                """,
                (TENANT, f"fe-{uuid.uuid4().hex}", len(ASINS)),
            )
            batch_id = str(cur.fetchone()[0])
            for asin in ASINS:
                cur.execute(
                    """
                    INSERT INTO amazon_us.batch_item
                      (batch_id, tenant_id, marketplace, asin, url, status,
                       reported_review_count, fetched_review_count, review_pages_fetched)
                    VALUES (%s, %s, 'US', %s, %s, 'succeeded', %s, %s, %s)
                    """,
                    (batch_id, TENANT, asin, f"https://www.amazon.com/dp/{asin}",
                     120, 100, 2),
                )
        conn.commit()
    print(f"测试批次: {batch_id}")

    env_cmd = f"$env:AMAZON_US_POSTGRES_DSN='{DSN}'"
    proc = subprocess.Popen(
        ["powershell", "-Command",
         f"{env_cmd}; python scripts/collection_console.py --port {PORT}"],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    failures = []
    try:
        base = f"http://127.0.0.1:{PORT}"
        for _ in range(30):
            time.sleep(0.5)
            try:
                urllib.request.urlopen(f"{base}/healthz", timeout=2).read()
                break
            except Exception:
                if proc.poll() is not None:
                    print("console 启动失败")
                    return 1

        # 1. 批次列表 + coordinator 字段
        data = json.loads(urllib.request.urlopen(
            f"{base}/api/batches?tenant={TENANT}", timeout=10).read())
        assert "coordinator" in data, "响应应含 coordinator 字段"
        assert len(data["items"]) == 1, "应有 1 个批次"
        assert data["items"][0]["succeeded"] == 3, "成功数应为 3"
        print("[通过] /api/batches 含 coordinator + 进度数据")

        # 2. 成员列表含 reported_review_count
        data = json.loads(urllib.request.urlopen(
            f"{base}/api/batches/{batch_id}/items?tenant={TENANT}", timeout=10).read())
        assert data["total"] == 3
        assert data["items"][0]["reported_review_count"] == 120, "应返回评论总数"
        print("[通过] /api/batches/{id}/items 含 reported_review_count")

        # 3. 静态页面含新面板
        page = urllib.request.urlopen(f"{base}/", timeout=10).read().decode("utf-8")
        for element in ("collectionBatchRows", "batchItemsPanel", "coordinatorBadge", "采集批次", "批次成员"):
            assert element in page, f"页面缺少 {element}"
        appjs = urllib.request.urlopen(f"{base}/app.js", timeout=10).read().decode("utf-8")
        for element in ("renderCollectionBatches", "selectBatch", "loadBatchItems", "renderCoordinatorBadge"):
            assert element in appjs, f"app.js 缺少 {element}"
        css = urllib.request.urlopen(f"{base}/styles.css", timeout=10).read().decode("utf-8")
        for element in ("mini-track", "coordinator-badge.online", "status-badge.completed"):
            assert element in css, f"styles.css 缺少 {element}"
        print("[通过] 页面/JS/CSS 均含批次模型元素")

        return 1 if failures else 0
    except AssertionError as exc:
        print(f"[失败] {exc}")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        with psycopg.connect(DSN) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM amazon_us.batch WHERE tenant_id=%s", (TENANT,))
            conn.commit()
        print("已清理测试数据")


if __name__ == "__main__":
    raise SystemExit(main())
