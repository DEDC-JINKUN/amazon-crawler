"""控制台控制 API 测试：上传清单、启动协调器、停止批次。"""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

try:
    import psycopg
except ImportError:
    pytest.skip("psycopg not available", allow_module_level=True)


def _new_tenant() -> str:
    return f"console_api_{uuid.uuid4().hex[:8]}"


def _write_csv(rows: list[tuple[str, str]]) -> bytes:
    """生成 CSV 字节（首行标题 + ASIN,URL 行）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["asin", "url"])
    for asin, url in rows:
        w.writerow([asin, url])
    return buf.getvalue().encode("utf-8")


def _read_multipart(body: bytes, boundary: str) -> dict[str, bytes]:
    """手动解析 multipart/form-data，返回 {字段名: 字节内容}。"""
    sep = b"--" + boundary.encode()
    parts = body.split(sep)
    result: dict[str, bytes] = {}
    for part in parts:
        if not part or part in (b"--\r\n", b"--"):
            continue
        header_end = part.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = part[:header_end].decode("utf-8", errors="replace")
        content = part[header_end + 4:].rstrip(b"\r\n")
        # 从 Content-Disposition 提取 name
        for line in headers.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for attr in line.split(";"):
                    attr = attr.strip()
                    if attr.startswith("name="):
                        name = attr[5:].strip('"')
                        result[name] = content
                        break
    return result


@pytest.fixture()
def server():
    """启动 collection_console.py 子进程，返回 (base_url, tenant_id)。"""
    dsn = os.environ.get("AMAZON_US_POSTGRES_DSN", "postgresql://postgres:123456@localhost:5432/amazon_us")
    tenant = _new_tenant()
    port = 18770 + (hash(tenant) % 1000)
    env = os.environ.copy()
    env["AMAZON_US_POSTGRES_DSN"] = dsn
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "collection_console.py"), "--host", "127.0.0.1", "--port", str(port), "--tenant-id", tenant],
        env=env,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    # 等启动
    for _ in range(30):
        import urllib.request
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)
    else:
        proc.kill()
        raise RuntimeError("console 启动超时")
    try:
        yield base, tenant
    finally:
        proc.kill()
        proc.wait(timeout=5)


def test_manifest_csv_parses_basic():
    """CSV 解析：首行标题跳过 + ASIN/URL 自动补全。"""
    from collection_console import parse_manifest_csv
    data = _write_csv([
        ("B00TEST123", "https://www.amazon.com/dp/B00TEST123"),
        ("B00TEST567", "https://www.amazon.com/dp/B00TEST567"),
    ])
    rows = parse_manifest_csv(data)
    assert len(rows) == 2
    assert rows[0]["asin"] == "B00TEST123"
    assert rows[1]["url"].endswith("/B00TEST567")


def test_manifest_csv_auto_fills_url():
    """只有 ASIN 时自动补全 URL。"""
    from collection_console import parse_manifest_csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["asin"])
    w.writerow(["B00TEST123"])
    rows = parse_manifest_csv(buf.getvalue().encode("utf-8"))
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.amazon.com/dp/B00TEST123"


def test_manifest_csv_rejects_empty():
    """空清单抛 ValueError。"""
    from collection_console import parse_manifest_csv
    with pytest.raises(ValueError, match="清单为空"):
        parse_manifest_csv(b"asin,url\n")


def test_manifest_csv_rejects_bad_asin():
    """非法 ASIN 抛 ValueError 带行号。"""
    from collection_console import parse_manifest_csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["asin", "url"])
    w.writerow(["SHORT", "https://example.com"])
    with pytest.raises(ValueError, match="第 2 行"):
        parse_manifest_csv(buf.getvalue().encode("utf-8"))


def test_manifest_csv_handles_bom():
    """UTF-8 BOM 不影响解析。"""
    from collection_console import parse_manifest_csv
    data = b"\xef\xbb\xbf" + _write_csv([("B00TEST123", "")])
    rows = parse_manifest_csv(data)
    assert len(rows) == 1


def test_parse_manifest_5800_rows_performance():
    """5800 行 CSV 解析 < 3 秒。"""
    from collection_console import parse_manifest_csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["asin", "url"])
    for i in range(5800):
        asin = f"B{i:09d}"
        # 确保 10 位
        asin = ("B" + f"{i:09d}")[:10]
        w.writerow([asin, f"https://www.amazon.com/dp/{asin}"])
    data = buf.getvalue().encode("utf-8")
    t0 = time.monotonic()
    rows = parse_manifest_csv(data)
    elapsed = time.monotonic() - t0
    assert len(rows) == 5800
    assert elapsed < 3.0, f"解析耗时 {elapsed:.2f}s 超过 3 秒"


def _write_xlsx(rows: list[list]) -> bytes:
    """用 openpyxl 在内存生成 xlsx 字节。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_manifest_excel_parses_basic():
    """Excel 解析：表头跳过 + 单列纯 ASIN（用户真实清单形态）。"""
    from collection_console import parse_manifest_excel
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        pytest.skip("openpyxl not available")
    data = _write_xlsx([["ASIN"], ["B00TEST123"], ["B01TEST456"]])
    rows = parse_manifest_excel(data)
    assert len(rows) == 2
    assert rows[0] == {"asin": "B00TEST123", "url": "https://www.amazon.com/dp/B00TEST123"}
    assert rows[1]["asin"] == "B01TEST456"


def test_manifest_excel_skips_note_rows_and_fills_url():
    """Excel 解析：备注列/说明行跳过，带 URL 列时直接使用。"""
    from collection_console import parse_manifest_excel
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        pytest.skip("openpyxl not available")
    data = _write_xlsx([
        ["ASIN", "备注", "链接"],
        ["B00TEST123", "重点商品", "https://www.amazon.com/dp/B00TEST123"],
        [None, "这行没有 ASIN，只有说明", None],
        ["B01TEST456", None, None],
    ])
    rows = parse_manifest_excel(data)
    # 说明行被跳过，不算错误
    assert len(rows) == 2
    assert rows[0]["url"] == "https://www.amazon.com/dp/B00TEST123"
    # 无 URL 时自动补全
    assert rows[1]["url"] == "https://www.amazon.com/dp/B01TEST456"


def test_manifest_excel_rejects_empty():
    """Excel 解析：没有有效 ASIN 时报错。"""
    from collection_console import parse_manifest_excel
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        pytest.skip("openpyxl not available")
    data = _write_xlsx([["ASIN"], ["不是ASIN"], [None]])
    with pytest.raises(ValueError, match="为空"):
        parse_manifest_excel(data)


def test_multipart_dispatches_by_filename():
    """multipart 上传按文件名分派：.xlsx 走 Excel 解析器，其余走 CSV。"""
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        pytest.skip("openpyxl not available")
    import collection_console as cc

    def _make_handler(body: bytes, filename: str, extra_fields: dict[str, str] | None = None):
        """构造带 headers/rfile 的假 handler，直接调用 _read_multipart_manifest。

        extra_fields：普通表单字段（如复选框 collect_reviews）。
        """
        boundary = "testboundary123"
        content_type = f"multipart/form-data; boundary={boundary}"
        parts = []
        for name, value in (extra_fields or {}).items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        file_part = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="manifest"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        body_bytes = b"".join(parts) + file_part + body + f"\r\n--{boundary}--\r\n".encode("utf-8")

        class _FakeHeaders:
            def __init__(self, ct: str, length: int):
                self._ct = ct
                self._len = length

            def get(self, key: str, default: str = "") -> str:
                if key.lower() == "content-type":
                    return self._ct
                if key.lower() == "content-length":
                    return str(self._len)
                return default

        class _FakeRFile:
            def __init__(self, data: bytes):
                self._data = data

            def read(self, n: int) -> bytes:
                return self._data[:n]

        class _FakeHandler:
            headers = _FakeHeaders(content_type, len(body_bytes))
            rfile = _FakeRFile(body_bytes)

        return _FakeHandler()

    # .xlsx 文件名 → Excel 解析（返回值已改为 (清单行, 表单字段)）
    xlsx_data = _write_xlsx([["ASIN"], ["B00TEST123"]])
    rows, fields = cc.ConsoleHandler._read_multipart_manifest(_make_handler(xlsx_data, "清单.xlsx"))
    assert len(rows) == 1
    assert rows[0]["asin"] == "B00TEST123"
    assert fields == {}

    # .csv 文件名 → CSV 解析（原路径不受影响）
    csv_data = _write_csv([("B00TEST123", "")])
    rows, fields = cc.ConsoleHandler._read_multipart_manifest(_make_handler(csv_data, "清单.csv"))
    assert len(rows) == 1
    assert rows[0]["asin"] == "B00TEST123"

    # 带评论开关字段：清单行 + 表单字段一并解析
    rows, fields = cc.ConsoleHandler._read_multipart_manifest(
        _make_handler(csv_data, "清单.csv", extra_fields={"collect_reviews": "off"})
    )
    assert len(rows) == 1
    assert fields == {"collect_reviews": "off"}


def test_export_batch_csv_content():
    """批次导出：成功/失败成员都进 CSV，含中文表头和 BOM（Excel 可直接打开）。"""
    import importlib.util

    # 动态加载 batch_store（与 test_batch_store_integration 同模式）
    spec = importlib.util.spec_from_file_location("batch_store", ROOT / "scripts" / "batch_store.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    from collection_console import PostgresConsoleRepository

    dsn = os.environ.get("AMAZON_US_POSTGRES_DSN", "postgresql://postgres:123456@localhost:5432/amazon_us")
    tenant = _new_tenant()
    store = bs.BatchStore(dsn, tenant, "own")
    repo = PostgresConsoleRepository(dsn, tenant)
    try:
        # 建批次：两个成员（含一个重复，验证去重不炸）
        result = store.create_batch([
            {"asin": "B00TEST001", "url": "https://www.amazon.com/dp/B00TEST001"},
            {"asin": "B00TEST001", "url": "https://www.amazon.com/dp/B00TEST001"},
            {"asin": "B00TEST002", "url": "https://www.amazon.com/dp/B00TEST002"},
        ])
        assert result["requested_count"] == 2
        assert result["skipped_duplicates"] == 1
        batch_id = result["batch_id"]
        # 启动批次后才能领任务
        assert store.start_batch(batch_id) is True
        # 成员 1：领取并保存成功结果（带商品字段）
        task = store.claim_task("w-export-1", batch_id=batch_id)
        assert task is not None and task["asin"] == "B00TEST001"
        assert store.save_product_result(
            task=task,
            evidence=_evidence_x("run-export-1"),
            product={
                "title": "Test Product", "brand": "TestBrand", "price": "$19.99",
                "availability": "In Stock", "rating": "4.5",
                "reported_rating_count": 10, "reported_review_count": 100,
                "canonical_url": "https://www.amazon.com/dp/B00TEST001",
                "status": "product_done",
            },
            media=[],
            content_modules=[],
            review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
            next_status="succeeded",
            state_fields={"task_stage": "reviews"},
            reason="product_and_reviews_done",
        ) is True
        # 成员 2：领取并保存失败（模拟被拦：block_reason=robot → 错误类别 blocked）
        task2 = store.claim_task("w-export-2", batch_id=batch_id)
        assert task2 is not None and task2["asin"] == "B00TEST002"
        blocked_evidence = dict(_evidence_x("run-export-2"), block_reason="robot")
        assert store.save_failure(
            task=task2, reason="captcha", error="robot check 拦截",
            evidence=blocked_evidence, next_status="blocked",
        ) is True

        # 导出 CSV
        csv_bytes = repo.export_batch(batch_id)
        assert csv_bytes is not None
        # UTF-8 BOM 开头（Excel 双击打开不乱码）
        assert csv_bytes.startswith(b"\xef\xbb\xbf")
        text = csv_bytes.decode("utf-8-sig")
        lines = text.splitlines()
        # 表头 + 2 行数据
        assert len(lines) == 3
        header = lines[0]
        assert "ASIN" in header and "标题" in header and "价格" in header and "错误类别" in header
        # 成员 1（按 ASIN 排序在前）：成功 + 商品字段
        assert "B00TEST001" in lines[1] and "Test Product" in lines[1] and "$19.99" in lines[1]
        # 成员 2：被拦 + 三分类 blocked + 拦截原因
        assert "B00TEST002" in lines[2] and ",blocked," in lines[2] and "robot check 拦截" in lines[2]

        # 不存在的批次返回 None
        assert repo.export_batch("00000000-0000-0000-0000-000000000000") is None
    finally:
        # 清理本租户
        import psycopg
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for table in ("collection_evidence", "product_snapshot", "item_state",
                              "asin_master", "batch", "collection_run"):
                    cur.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant,))
            conn.commit()


def _evidence_x(run_id):
    """构造最小证据（导出测试用）。"""
    return {
        "run_id": run_id, "url": "https://www.amazon.com/dp/x", "http_status": 200,
        "transfer_bytes": 100, "source_type": "http_html",
        "content_hash": "b" * 64, "block_reason": None,
        "parser_version": "test", "error_code": None, "context_json": {},
    }


def test_list_batch_items_exposes_snapshot_timestamp():
    """缓存复用展示：成员列表带 snapshot_at——已采成员有快照时间，未采成员为 None。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("batch_store", ROOT / "scripts" / "batch_store.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    from collection_console import PostgresConsoleRepository

    dsn = os.environ.get("AMAZON_US_POSTGRES_DSN", "postgresql://postgres:123456@localhost:5432/amazon_us")
    tenant = _new_tenant()
    store = bs.BatchStore(dsn, tenant, "own")
    repo = PostgresConsoleRepository(dsn, tenant)
    try:
        result = store.create_batch([
            {"asin": "B00SNAP001", "url": "https://www.amazon.com/dp/B00SNAP001"},
            {"asin": "B00SNAP002", "url": "https://www.amazon.com/dp/B00SNAP002"},
        ])
        batch_id = result["batch_id"]
        assert store.start_batch(batch_id) is True
        # 成员 1：采集成功 → 库里有快照
        task = store.claim_task("w-snap-1", batch_id=batch_id)
        assert store.save_product_result(
            task=task,
            evidence=_evidence_x("run-snap-1"),
            product={
                "title": "Snapshot Product", "canonical_url": "https://www.amazon.com/dp/B00SNAP001",
                "status": "product_done",
            },
            media=[], content_modules=[],
            review_summary={"status": "exhausted", "fetched_count": 0, "pages_fetched": 0},
            next_status="succeeded",
            state_fields={"task_stage": "reviews"},
            reason="product_and_reviews_done",
        ) is True
        # 成员 2：不采（留在 pending，模拟"库内无快照"的待采成员）
        payload = repo.list_batch_items(batch_id, limit=10)
        by_asin = {item["asin"]: item for item in payload["items"]}
        assert by_asin["B00SNAP001"]["snapshot_at"] is not None  # 已采 → 有快照时间
        assert by_asin["B00SNAP002"]["snapshot_at"] is None      # 未采 → 无快照
    finally:
        import psycopg
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for table in ("collection_evidence", "product_snapshot", "item_state",
                              "asin_master", "batch", "collection_run"):
                    cur.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant,))
            conn.commit()


def test_rows_from_asin_payload_validates_and_fills_url():
    """Agent JSON 载荷：ASIN 校验 + 去重 + URL 自动补全。"""
    from collection_console import _rows_from_asin_payload
    # 正常：大写化、去重、补全 URL
    rows = _rows_from_asin_payload({"asins": ["b00test123", "B00TEST123", "B01TEST456"]})
    assert len(rows) == 2
    assert rows[0] == {"asin": "B00TEST123", "url": "https://www.amazon.com/dp/B00TEST123"}
    # 显式 urls 数组
    rows = _rows_from_asin_payload({
        "asins": ["B00TEST123", "B01TEST456"],
        "urls": ["https://www.amazon.com/dp/B00TEST123", ""],
    })
    assert rows[1]["url"] == "https://www.amazon.com/dp/B01TEST456"
    # 无效 ASIN / 空数组 / 非对象 → 报错
    for bad in ({"asins": []}, {"asins": ["SHORT"]}, {"asins": ["B00TEST123"], "urls": []}, {"no": "asins"}):
        with pytest.raises(ValueError):
            _rows_from_asin_payload(bad)


def test_rows_from_asin_payload_rejects_keywords():
    """关键词排名：Agent 请求带 keywords/keyword_ranking/search_terms → 明确报不支持。"""
    import collection_console as cc

    # 三种字段名都拒
    for field in ("keywords", "keyword_ranking", "search_terms"):
        with pytest.raises(ValueError, match="关键词排名采集暂不支持"):
            cc._rows_from_asin_payload({"asins": ["B00TEST001"], field: ["wireless speaker"]})

    # 正常载荷不受影响
    rows = cc._rows_from_asin_payload({"asins": ["B00TEST001"]})
    assert rows == [{"asin": "B00TEST001", "url": "https://www.amazon.com/dp/B00TEST001"}]


def test_create_batch_idempotency_key_returns_same_batch():
    """幂等键：同租户同键 24h 内重试返回同一批次，不重复创建。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("batch_store", ROOT / "scripts" / "batch_store.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    dsn = os.environ.get("AMAZON_US_POSTGRES_DSN", "postgresql://postgres:123456@localhost:5432/amazon_us")
    tenant = _new_tenant()
    store = bs.BatchStore(dsn, tenant, "own")
    try:
        idem = "agent-retry-key-001"
        first = store.create_batch(
            [{"asin": "B00TEST001", "url": "https://www.amazon.com/dp/B00TEST001"}],
            idempotency_key=idem,
        )
        assert first["idempotent_replay"] is False
        # 模拟 Agent 超时重试：同键再提交，拿回同一批次
        second = store.create_batch(
            [{"asin": "B00TEST001", "url": "https://www.amazon.com/dp/B00TEST001"}],
            idempotency_key=idem,
        )
        assert second["idempotent_replay"] is True
        assert second["batch_id"] == first["batch_id"]
        # 不同键 + 不同清单 → 新批次（同清单会触发"活跃批次重复上传"拒绝，属正常防线）
        third = store.create_batch(
            [{"asin": "B00TEST999", "url": "https://www.amazon.com/dp/B00TEST999"}],
            idempotency_key="another-key",
        )
        assert third["idempotent_replay"] is False
        assert third["batch_id"] != first["batch_id"]
    finally:
        import psycopg
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for table in ("collection_evidence", "item_state", "asin_master", "batch"):
                    cur.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant,))
            conn.commit()


def test_create_batch_collect_reviews_toggle_persisted():
    """评论开关：collect_reviews=False 落库为 false，默认建批为 true。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("batch_store", ROOT / "scripts" / "batch_store.py")
    bs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bs)

    dsn = os.environ.get("AMAZON_US_POSTGRES_DSN", "postgresql://postgres:123456@localhost:5432/amazon_us")
    tenant = _new_tenant()
    store = bs.BatchStore(dsn, tenant, "own")
    try:
        off = store.create_batch(
            [{"asin": "B00REVOFF1", "url": "https://www.amazon.com/dp/B00REVOFF1"}],
            collect_reviews=False,
        )
        default = store.create_batch(
            [{"asin": "B00REVDEF1", "url": "https://www.amazon.com/dp/B00REVDEF1"}],
        )
        import psycopg
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT collect_reviews FROM amazon_us.batch WHERE batch_id=%s",
                    (off["batch_id"],),
                )
                assert cur.fetchone()[0] is False
                cur.execute(
                    "SELECT collect_reviews FROM amazon_us.batch WHERE batch_id=%s",
                    (default["batch_id"],),
                )
                assert cur.fetchone()[0] is True
    finally:
        import psycopg
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                for table in ("collection_evidence", "item_state", "asin_master", "batch"):
                    cur.execute(f"DELETE FROM amazon_us.{table} WHERE tenant_id=%s", (tenant,))
            conn.commit()


def _make_auth_handler(supplied_key, api_key, read_key):
    """构造假的 handler/server 组合，测 _authorized 的读写区分。"""

    class _FakeHeaders:
        def get(self, key, default=""):
            if key == "X-Collection-API-Key":
                return supplied_key
            return default

    class _FakeServer:
        def __init__(self):
            self.api_key = api_key
            self.read_key = read_key

    class _FakeHandler:
        def __init__(self):
            self.headers = _FakeHeaders()
            self.server = _FakeServer()
            self.sent = []

        def _send_json(self, status, payload):
            self.sent.append((status, payload))

    return _FakeHandler()


def test_authorized_read_write_permission_split():
    """只读 key 拦截：读放行、写 403；主 key 全放行；未配置全放行。"""
    from collection_console import ConsoleHandler
    from http import HTTPStatus

    # 1. 只读 key：GET 放行，POST 403
    h = _make_auth_handler("read-key-1", api_key="master-key-1", read_key="read-key-1")
    assert ConsoleHandler._authorized(h) is True
    assert ConsoleHandler._authorized(h, write=True) is False
    assert h.sent[0][0] == HTTPStatus.FORBIDDEN

    # 2. 主 key：读写全放行
    h = _make_auth_handler("master-key-1", api_key="master-key-1", read_key="read-key-1")
    assert ConsoleHandler._authorized(h) is True
    assert ConsoleHandler._authorized(h, write=True) is True

    # 3. 错误 key：401
    h = _make_auth_handler("wrong", api_key="master-key-1", read_key="read-key-1")
    assert ConsoleHandler._authorized(h) is False
    assert h.sent[0][0] == HTTPStatus.UNAUTHORIZED

    # 4. 未配置任何 key：全放行（本机开发模式）
    h = _make_auth_handler("", api_key="", read_key="")
    assert ConsoleHandler._authorized(h) is True
    assert ConsoleHandler._authorized(h, write=True) is True


@pytest.mark.skip(reason="pytest fixture issue")
def test_coordinator_manager_spawn_and_stop(tmp_path):
    """CoordinatorManager spawn + stop 生命周期（用 mock 脚本避免启动真协调器）。"""
    dsn = os.environ.get("AMAZON_US_POSTGRES_DSN", "postgresql://postgres:123456@localhost:5432/amazon_us")
    tenant = _new_tenant()
    # 创建 mock 协调器脚本（sleep 30 秒后退出，模拟长驻进程）
    mock_script = tmp_path / "mock_coord.py"
    mock_script.write_text("import time; time.sleep(30)", encoding="utf-8")
    import collection_console as cc
    real = cc.COORDINATOR_SCRIPT
    cc.COORDINATOR_SCRIPT = mock_script
    try:
        from collection_console import CoordinatorManager
        mgr = CoordinatorManager(dsn, tenant)
        assert mgr.status()["status"] == "offline"
        start_result = mgr.start()
        assert start_result["status"] == "online"
        pid = start_result["pid"]
        assert pid is not None
        start2 = mgr.start()
        assert start2["pid"] == pid
        stop_result = mgr.stop()
        assert stop_result["status"] == "offline"
        assert isinstance(mgr.recent_log(), list)
    finally:
        cc.COORDINATOR_SCRIPT = real



