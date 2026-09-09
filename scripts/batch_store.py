"""批次模型存储层：任务队列与并发协调的核心。

设计要点（对应架构调整）：
1. 进度只认本批次：任务挂在 batch_item 上，证据带 batch_id，批次进度
   由 batch_progress 视图统一推导，页面不再自己拼多张表。
2. 容量做成数据库槽位租约：一行一个槽，物理上不可能超卖；双 worker
   分别租槽，互不挤占启动。
3. 失败三分类：blocked=被拦(验证码) / fetch=网络代理 / system=解析代码，
   批次终态按剩余失败构成决定，用户能看懂该等冷却还是修系统。
4. 对 worker 保持鸭子类型兼容：claim_task / save_failure /
   save_product_result / save_review_result 与旧 PostgresWorkerStorage
   同名同参，amazon_us_worker.py 无需改循环逻辑。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
import uuid
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row


# 旧 item_state 七态 → 新 batch_item 六态的映射
# （worker 循环传的还是旧状态名，这里统一转换）
ITEM_STATUS_MAP = {
    "pending": "pending",          # 延后重试 → failed + next_retry_at，见 _normalize_status
    "running": "running",
    "product_done": "pending",     # 商品已存，待评论阶段
    "reviews_pending": "pending",  # 评论还有下一页
    "succeeded": "succeeded",
    "blocked": "blocked",
    "failed": "failed",
    "variant": "variant",          # 变体跳转终态：页面是另一个 ASIN
}

# canary 探针成员数：批次启动先跑这几个成员，全部成功才放开全量，
# 避免 5800 个 ASIN 在出口已被拦的情况下整批冲出去
CANARY_SIZE = 2

# batch_item 状态 → item_state 缓存态（旧 console 兼容）
# variant → failed：旧表没有变体态，映射成最接近的"没拿到数据"终态
CACHE_STATUS_MAP = {
    "pending": "pending",
    "running": "running",
    "succeeded": "succeeded",
    "blocked": "blocked",
    "failed": "failed",
    "cancelled": "pending",
    "variant": "failed",
}

# 失败原因 → 三分类的判定规则（按顺序匹配）
FETCH_REASON_KEYWORDS = ("fetch_error", "review_fetch_error", "adapter", "timeout", "proxy", "connection")


class BatchStoreError(RuntimeError):
    """批次存储层基础异常。"""


class BatchStore:
    """批次成员模型存储：领取、保存、回收、容量租约、批次收尾。"""

    def __init__(
        self,
        dsn: str,
        tenant_id: str,
        subject_type: str = "own",
        *,
        connect: Callable[[], Any] | None = None,
        default_lease_seconds: int = 300,
        default_slot_seconds: int = 120,
    ) -> None:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id 不能为空")
        if subject_type not in {"own", "competitor", "candidate"}:
            raise ValueError("subject_type 必须是 own/competitor/candidate")
        self.dsn = dsn
        self.tenant_id = tenant_id.strip()
        self.subject_type = subject_type
        self.default_lease_seconds = int(default_lease_seconds)
        self.default_slot_seconds = int(default_slot_seconds)
        self._connect_factory = connect or self._connect

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _connect(self):
        """每个操作独立连接，事务边界清晰，崩溃不留半截状态。"""
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _as_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return dict(row) if isinstance(row, Mapping) else dict(row)

    @staticmethod
    def _jsonb(value: Any) -> Any:
        from psycopg.types.json import Jsonb
        return Jsonb(value)

    @staticmethod
    def _nullable_int(value: Any) -> int | None:
        """脏数据防御：空格、非数字字符串、NaN 一律当 None。"""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            # 带逗号的数字（如 "1,234"）和货币符号（如 "$12.50"）
            cleaned = stripped.replace(",", "").replace("$", "").replace("¥", "")
            try:
                return int(float(cleaned))
            except (ValueError, TypeError):
                return None
        try:
            result = int(value)
            return result if result == result else None  # NaN 检查（float('nan') 不等于自己）
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _int(value: Any, default: int = 0) -> int:
        return BatchStore._nullable_int(value) if BatchStore._nullable_int(value) is not None else default

    @staticmethod
    def _normalize_status(next_status: str, task_stage: str) -> str:
        """把 worker 传来的旧状态名转换为 batch_item 新状态。"""
        mapped = ITEM_STATUS_MAP.get(next_status, next_status)
        # pending + reviews 阶段保持 task_stage；pending 且带 next_retry_at
        # 的情况由调用方转成 failed（延后重试语义）
        if mapped == "pending" and next_status == "pending":
            return "failed"  # 旧代码 save_failure(next_status='pending') = 延迟重试
        if mapped == "pending" and task_stage == "reviews":
            return "pending"
        return mapped

    @staticmethod
    def _classify_error(reason: str, block_reason: str | None) -> str:
        """失败三分类：blocked / fetch / system。

        规则：有 block_reason（验证码/机器人）→ blocked；
        网络类关键词 → fetch；其余（解析/代码/数据库）→ system。
        """
        if block_reason:
            return "blocked"
        text = (reason or "").lower()
        if any(word in text for word in FETCH_REASON_KEYWORDS):
            return "fetch"
        return "system"

    # ------------------------------------------------------------------
    # 1. 批次创建：上传清单 → batch + batch_item
    # ------------------------------------------------------------------
    def create_batch(
        self,
        rows: Iterable[Mapping[str, Any]],
        *,
        uploaded_by: str = "console",
        idempotency_key: str = "",
        collect_reviews: bool = True,
    ) -> dict[str, Any]:
        """一次上传 = 一个批次；同批次 ASIN 自动去重（UNIQUE 冲突跳过）。

        返回 {batch_id, requested_count, skipped_duplicates, idempotent_replay}。
        同时 upsert asin_master / item_state 保持旧表可见（缓存用途）。
        传 idempotency_key 时：24 小时内同租户同键直接返回已有批次
        （idempotent_replay=True），防止 Agent 超时重试重复建批次。
        collect_reviews=False：批次不采评论，商品完成即收尾。
        """
        # 先转成列表（避免生成器被消费后无法统计去重数）
        all_rows = [dict(row) for row in rows]
        # 在内存里按 ASIN 去重（数据库唯一约束兜底）
        seen: set[str] = set()
        unique_rows: list[dict[str, Any]] = []
        for row in all_rows:
            asin = str(row.get("asin", "")).strip().upper()
            url = str(row.get("url", "")).strip()
            if not asin or not url:
                raise ValueError("清单行必须包含 asin 和 url")
            if asin in seen:
                continue
            seen.add(asin)
            unique_rows.append({"asin": asin, "url": url})
        if not unique_rows:
            raise ValueError("清单为空：没有有效的 asin/url 行")

        # 幂等键：规范化（去空白 + 限长），空串表示不用幂等
        idem_key = (idempotency_key or "").strip()[:200]
        if idem_key:
            with self._connect_factory() as conn:
                with conn.cursor() as cursor:
                    # 24 小时窗口内同租户同键 → 返回已有批次，不重复创建
                    cursor.execute(
                        """
                        SELECT batch_id, requested_count
                        FROM amazon_us.batch
                        WHERE tenant_id=%s AND idempotency_key=%s
                          AND created_at > now() - interval '24 hours'
                        ORDER BY created_at DESC LIMIT 1
                        """,
                        (self.tenant_id, idem_key),
                    )
                    hit = self._as_dict(cursor.fetchone())
            if hit:
                return {
                    "batch_id": str(hit["batch_id"]),
                    "requested_count": hit["requested_count"],
                    "skipped_duplicates": len(all_rows) - len(unique_rows),
                    "idempotent_replay": True,
                }

        # 清单哈希：ASIN 排序后拼接，同一份清单（无论行序）哈希一致
        import hashlib
        canonical = "\n".join(sorted(f"{r['asin']}|{r['url']}" for r in unique_rows))
        manifest_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        batch_id = uuid.uuid4()
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    # 活跃批次中相同清单 → 拒绝，防止误重复上传
                    cursor.execute(
                        """
                        SELECT batch_id, status FROM amazon_us.batch
                        WHERE tenant_id=%s AND marketplace='US'
                          AND manifest_hash=%s
                          AND status IN ('pending','provisioning','canary','running','stopping')
                        """,
                        (self.tenant_id, manifest_hash),
                    )
                    active = self._as_dict(cursor.fetchone())
                    if active:
                        raise BatchStoreError(
                            f"相同清单已在批次 {active['batch_id']}（状态 {active['status']}）中运行"
                        )
                    # 建批次（带幂等键时一并写入；评论开关一并落库）
                    if idem_key:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.batch
                              (batch_id, tenant_id, marketplace, manifest_hash,
                               requested_count, status, uploaded_by, idempotency_key, collect_reviews)
                            VALUES (%s,%s,'US',%s,%s,'pending',%s,%s,%s)
                            """,
                            (batch_id, self.tenant_id, manifest_hash, len(unique_rows), uploaded_by, idem_key, bool(collect_reviews)),
                        )
                    else:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.batch
                              (batch_id, tenant_id, marketplace, manifest_hash,
                               requested_count, status, uploaded_by, collect_reviews)
                            VALUES (%s,%s,'US',%s,%s,'pending',%s,%s)
                            """,
                            (batch_id, self.tenant_id, manifest_hash, len(unique_rows), uploaded_by, bool(collect_reviews)),
                        )
                    # 建成员 + 同步旧表（asin_master 外键需要）
                    for row in unique_rows:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.asin_master
                              (tenant_id, marketplace, asin, subject_type, source_type, source_url)
                            VALUES (%s,'US',%s,%s,'batch_upload',%s)
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type) DO UPDATE
                              SET updated_at=CURRENT_TIMESTAMP
                            """,
                            (self.tenant_id, row["asin"], self.subject_type, row["url"]),
                        )
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.item_state
                              (tenant_id, marketplace, asin, subject_type, url, status)
                            VALUES (%s,'US',%s,%s,%s,'pending')
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type) DO UPDATE
                              SET url=EXCLUDED.url, updated_at=CURRENT_TIMESTAMP
                            """,
                            (self.tenant_id, row["asin"], self.subject_type, row["url"]),
                        )
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.batch_item
                              (batch_id, tenant_id, marketplace, asin, url, status)
                            VALUES (%s,%s,'US',%s,%s,'pending')
                            ON CONFLICT (batch_id, asin) DO NOTHING
                            """,
                            (batch_id, self.tenant_id, row["asin"], row["url"]),
                        )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "batch_id": str(batch_id),
            "requested_count": len(unique_rows),
            "skipped_duplicates": len(all_rows) - len(unique_rows),
            "idempotent_replay": False,
        }

    # ------------------------------------------------------------------
    # 2. 任务领取：从 batch_item 领（兼容 worker 的 claim_task 接口）
    # ------------------------------------------------------------------
    def claim_task(
        self,
        worker_id: str,
        *,
        lease_seconds: int | None = None,
        task_stage: str | None = None,
        batch_id: str | None = None,
    ) -> dict[str, Any] | None:
        """原子领取一个批次成员：SKIP LOCKED + 租约 token。

        关键点：JOIN batch 且要求批次 running 且未请求停止——
        worker 领不到任务自然退出，停止传播不需要额外信号通道。
        """
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id 不能为空")
        seconds = int(lease_seconds if lease_seconds is not None else self.default_lease_seconds)
        if seconds <= 0:
            raise ValueError("lease_seconds 必须为正")
        stage = task_stage.strip() if task_stage else None
        if stage not in {None, "product", "reviews"}:
            raise ValueError("task_stage 必须是 product 或 reviews")
        token = uuid.uuid4().hex
        batch_filter = "AND b.batch_id=%s" if batch_id else ""
        stage_clause = "AND i.task_stage=%s" if stage else ""
        # canary 阶段只领探针成员（批次状态 canary 时）；
        # running 阶段领全部剩余成员（探针此时已是终态，不受影响）
        canary_clause = (
            "AND (b.status!='canary' OR i.is_canary)"
        )
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    sql = f"""
                        WITH candidate AS (
                          SELECT i.batch_id, i.asin
                          FROM amazon_us.batch_item i
                          JOIN amazon_us.batch b ON b.batch_id=i.batch_id
                          WHERE b.tenant_id=%s AND b.marketplace='US'
                            AND b.status IN ('running','canary') AND b.stop_requested=false
                            AND i.status IN ('pending','failed')
                            AND (i.lease_expires_at IS NULL OR i.lease_expires_at<=CURRENT_TIMESTAMP)
                            AND (i.status='pending'
                                 OR (i.attempts < i.max_attempts
                                     AND (i.next_retry_at IS NULL OR i.next_retry_at<=CURRENT_TIMESTAMP)))
                            {batch_filter}
                            {stage_clause}
                            {canary_clause}
                          ORDER BY i.batch_id, i.updated_at, i.asin
                          FOR UPDATE OF i SKIP LOCKED
                          LIMIT 1
                        ), claimed AS (
                          UPDATE amazon_us.batch_item i
                          SET status='running', resume_status=i.status,
                              lease_token=%s, lease_owner=%s,
                              lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),
                              updated_at=CURRENT_TIMESTAMP
                          FROM candidate c
                          WHERE i.batch_id=c.batch_id AND i.asin=c.asin
                          RETURNING i.*
                        ), history AS (
                          INSERT INTO amazon_us.state_history
                            (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
                          SELECT tenant_id, 'US', asin, %s, resume_status, status, 'batch_lease_claimed'
                          FROM claimed
                        )
                        -- 带出批次的评论开关：worker 据此决定商品完成后是否进评论阶段
                        SELECT c.*, b.collect_reviews
                        FROM claimed c
                        JOIN amazon_us.batch b ON b.batch_id=c.batch_id
                        """
                    params: list[Any] = [self.tenant_id]
                    if batch_id:
                        params.append(batch_id)
                    if stage:
                        params.append(stage)
                    params.extend([token, worker_id.strip(), seconds, self.subject_type])
                    cursor.execute(sql, tuple(params))
                    task = self._as_dict(cursor.fetchone())
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        if task is not None:
            # 兼容 worker 循环读取的字段名（与旧 claim_task 返回一致）
            task["job_id"] = None
        return task

    # ------------------------------------------------------------------
    # 3. 租约校验与释放的公共片段
    # ------------------------------------------------------------------
    def _lock_leased_item(self, cursor, task: Mapping[str, Any]) -> dict[str, Any] | None:
        """锁住仍持有有效租约的成员行；租约丢失返回 None（worker 应放弃）。"""
        cursor.execute(
            """
            SELECT status FROM amazon_us.batch_item
            WHERE batch_id=%s AND asin=%s
              AND lease_token=%s AND lease_owner=%s
              AND lease_expires_at > CURRENT_TIMESTAMP
            FOR UPDATE
            """,
            (task["batch_id"], str(task.get("asin", "")).strip().upper(),
             task.get("lease_token", ""), task.get("lease_owner", "")),
        )
        return self._as_dict(cursor.fetchone())

    def _sync_item_state_cache(
        self,
        cursor,
        asin: str,
        url: str,
        new_status: str,
        state_fields: Mapping[str, Any],
    ) -> None:
        """把 batch_item 的结果同步到旧 item_state（作为 ASIN 最新状态缓存）。

        旧 console 继续可用；新进度一律读 batch_progress 视图。
        """
        cache_status = CACHE_STATUS_MAP.get(new_status, "pending")
        assignments = ["status=%s", "updated_at=CURRENT_TIMESTAMP"]
        params: list[Any] = [cache_status]
        # 白名单内的续采字段同步（列名两表一致）
        cache_fields = {
            "task_stage", "resume_status", "next_review_url", "next_review_page",
            "next_retry_at", "review_page_limit", "reported_rating_count",
            "reported_review_count", "reported_count_source", "fetched_review_count",
            "review_pages_fetched", "block_reason", "last_error",
        }
        for name, value in state_fields.items():
            if name in cache_fields:
                assignments.append(f"{name}=COALESCE(%s, {name})")
                # 整数列与 _finish_item 同一套防御（空字符串/脏数据转 None）
                int_columns = {
                    "next_review_page", "review_page_limit", "reported_rating_count",
                    "reported_review_count", "fetched_review_count", "review_pages_fetched",
                }
                params.append(BatchStore._nullable_int(value) if name in int_columns else value)
        params.extend([self.tenant_id, asin, self.subject_type])
        cursor.execute(
            f"""
            UPDATE amazon_us.item_state SET {', '.join(assignments)}
            WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s
            """,
            params,
        )

    def _write_evidence(self, cursor, task: Mapping[str, Any], evidence: Mapping[str, Any]) -> None:
        """写采集证据，强制带上 batch_id —— 只认本批次证据的落地点。"""
        cursor.execute(
            """
            INSERT INTO amazon_us.collection_evidence
              (tenant_id, marketplace, asin, subject_type, run_id, batch_id, url,
               http_status, transfer_bytes, retrieved_at, source_type, content_hash,
               raw_html_path, block_reason, parser_version, error_code, context_json)
            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,CURRENT_TIMESTAMP),
                    %s,%s,%s,%s,%s,%s,%s)
            """,
            (
                self.tenant_id, str(task.get("asin", "")).strip().upper(), self.subject_type,
                evidence.get("run_id"), task.get("batch_id"), evidence.get("url"),
                self._nullable_int(evidence.get("http_status")),
                self._nullable_int(evidence.get("transfer_bytes")), evidence.get("retrieved_at"),
                evidence.get("source_type"), evidence.get("content_hash"),
                evidence.get("raw_html_path"), evidence.get("block_reason"),
                evidence.get("parser_version"), evidence.get("error_code"),
                self._jsonb(evidence.get("context_json") or {}),
            ),
        )

    # ------------------------------------------------------------------
    # 4. 保存商品结果（兼容 worker 的 save_product_result 接口）
    # ------------------------------------------------------------------
    def save_product_result(
        self,
        *,
        task: Mapping[str, Any],
        evidence: Mapping[str, Any],
        product: Mapping[str, Any],
        media: Iterable[Mapping[str, Any]],
        content_modules: Iterable[Mapping[str, Any]],
        review_summary: Mapping[str, Any],
        next_status: str,
        state_fields: Mapping[str, Any],
        reason: str,
    ) -> bool:
        """一个事务内：证据 + 商品快照 + 媒体/模块/评论汇总 + 成员状态 + 旧表缓存。

        商品数据仍写 product_snapshot 等 ASIN 维度表（它是商品数据不是任务数据）。
        """
        asin = str(task.get("asin", "")).strip().upper()
        new_status = self._normalize_status(next_status, str(state_fields.get("task_stage") or task.get("task_stage") or "product"))
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    current = self._lock_leased_item(cursor, task)
                    if current is None:
                        conn.rollback()
                        return False  # 租约已丢（被回收/过期），放弃本次写入
                    self._write_evidence(cursor, task, evidence)
                    # 商品快照（保持 ASIN 维度，追加式）
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.product_snapshot
                          (tenant_id, marketplace, asin, subject_type, canonical_url, availability,
                           title, brand, rating, reported_rating_count, reported_review_count,
                           review_count, review_count_source, price, bullets, product_description,
                           specs, buy_box, top_reviews, review_link, review_section_anchor,
                           aplus_present, bsr_rank, bsr_category, bsr_entries,
                           collected_at, status)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                COALESCE(%s,CURRENT_TIMESTAMP),%s)
                        """,
                        (
                            self.tenant_id, asin, self.subject_type, product.get("canonical_url"),
                            product.get("availability"), product.get("title"), product.get("brand"),
                            product.get("rating"), self._nullable_int(product.get("reported_rating_count")),
                            self._nullable_int(product.get("reported_review_count")),
                            product.get("review_count"), product.get("review_count_source"),
                            product.get("price"), self._jsonb(product.get("bullets") or []),
                            product.get("product_description"), self._jsonb(product.get("specs") or {}),
                            self._jsonb(product.get("buy_box") or {}),
                            self._jsonb(product.get("top_reviews") or []),
                            product.get("review_link"), product.get("review_section_anchor"),
                            bool(product.get("aplus_present")),
                            # BSR 类目销售排名：主排名/主类目/全部条目
                            self._nullable_int(product.get("bsr_rank")),
                            product.get("bsr_category"),
                            self._jsonb(product.get("bsr_entries") or []),
                            product.get("collected_at"),
                            product.get("status") or "product_done",
                        ),
                    )
                    # 媒体与内容模块：全删全插（快照语义）
                    cursor.execute(
                        "DELETE FROM amazon_us.media_asset WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                        (self.tenant_id, asin, self.subject_type),
                    )
                    for item in media:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.media_asset
                              (tenant_id, marketplace, asin, subject_type, placement, entry_type,
                               thumbnail_url, display_url, asset_url, poster_url, ordinal, is_primary,
                               width, height, alt_text, variant_asin, load_status, failure_reason, unique_key)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type, unique_key) DO UPDATE SET
                              asset_url=EXCLUDED.asset_url, load_status=EXCLUDED.load_status,
                              failure_reason=EXCLUDED.failure_reason
                            """,
                            (self.tenant_id, asin, self.subject_type, item.get("placement"),
                             item.get("entry_type"), item.get("thumbnail_url"), item.get("display_url"),
                             item.get("asset_url"), item.get("poster_url"),
                             self._nullable_int(item.get("ordinal")), bool(item.get("is_primary")),
                             item.get("width"), item.get("height"), item.get("alt_text"),
                             item.get("variant_asin"), item.get("load_status"),
                             item.get("failure_reason"), item.get("unique_key")),
                        )
                    cursor.execute(
                        "DELETE FROM amazon_us.content_module WHERE tenant_id=%s AND marketplace='US' AND asin=%s AND subject_type=%s",
                        (self.tenant_id, asin, self.subject_type),
                    )
                    for item in content_modules:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.content_module
                              (tenant_id, marketplace, asin, subject_type, module_type, position,
                               order_index, text, image_url, link_url, status, unique_key)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type, unique_key) DO UPDATE SET
                              text=EXCLUDED.text, image_url=EXCLUDED.image_url, status=EXCLUDED.status
                            """,
                            (self.tenant_id, asin, self.subject_type, item.get("module_type"),
                             self._int(item.get("position"), 0), self._nullable_int(item.get("order_index")),
                             item.get("text"), item.get("image_url"), item.get("link_url"),
                             item.get("status"), item.get("unique_key")),
                        )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.review_summary
                          (tenant_id, marketplace, asin, subject_type, reported_rating_count,
                           reported_review_count, reported_count_source, fetched_count, pages_fetched,
                           next_page, status, updated_at)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (tenant_id, marketplace, asin, subject_type) DO UPDATE SET
                          reported_rating_count=EXCLUDED.reported_rating_count,
                          reported_review_count=EXCLUDED.reported_review_count,
                          reported_count_source=EXCLUDED.reported_count_source,
                          fetched_count=EXCLUDED.fetched_count, pages_fetched=EXCLUDED.pages_fetched,
                          next_page=EXCLUDED.next_page, status=EXCLUDED.status, updated_at=CURRENT_TIMESTAMP
                        """,
                        (self.tenant_id, asin, self.subject_type,
                         self._nullable_int(review_summary.get("reported_rating_count")),
                         self._nullable_int(review_summary.get("reported_review_count")),
                         review_summary.get("reported_count_source"),
                         self._int(review_summary.get("fetched_count"), 0),
                         self._int(review_summary.get("pages_fetched"), 0),
                         review_summary.get("next_page"), review_summary.get("status")),
                    )
                    # 更新 batch_item 成员状态并释放租约
                    self._finish_item(cursor, task, new_status, state_fields, reason, current["status"])
                    # 同步旧表缓存
                    self._sync_item_state_cache(cursor, asin, str(task.get("url", "")), new_status, state_fields)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    def _finish_item(
        self,
        cursor,
        task: Mapping[str, Any],
        new_status: str,
        state_fields: Mapping[str, Any],
        reason: str,
        from_status: str,
    ) -> None:
        """收尾成员：写终态/中间态、释放租约、记历史。"""
        asin = str(task.get("asin", "")).strip().upper()
        terminal = new_status in {"succeeded", "blocked", "failed", "cancelled", "variant"}
        # 列名 → (SQL 片段, 参数)：同列后写覆盖先写，避免
        # "multiple assignments to same column"——成功收尾自动置 last_error=NULL，
        # 而 state_fields 又带 last_error='login_wall'（评论缺失标记）时，以 state_fields 为准
        sets: dict[str, tuple[str, Any]] = {}
        sets["status"] = ("status=%s", new_status)
        sets["lease_token"] = ("lease_token=NULL", None)
        sets["lease_owner"] = ("lease_owner=NULL", None)
        sets["lease_expires_at"] = ("lease_expires_at=NULL", None)
        sets["updated_at"] = ("updated_at=CURRENT_TIMESTAMP", None)
        error_class: str | None = None
        if terminal:
            sets["finalized_at"] = ("finalized_at=CURRENT_TIMESTAMP", None)
            # 成功清掉错误痕迹；失败记录分类（variant 不算失败：分类置空）
            if new_status in {"succeeded", "variant"}:
                sets["error_class"] = ("error_class=NULL", None)
                sets["block_reason"] = ("block_reason=NULL", None)
                if new_status == "succeeded":
                    sets["last_error"] = ("last_error=NULL", None)
            else:
                error_class = self._classify_error(
                    reason, state_fields.get("block_reason") or (task.get("block_reason") if isinstance(task, Mapping) else None)
                )
                sets["error_class"] = ("error_class=%s", error_class)
        # 白名单字段写入
        allowed = {
            "task_stage", "resume_status", "next_review_url", "next_review_page",
            "next_retry_at", "review_page_limit", "reported_rating_count",
            "reported_review_count", "reported_count_source", "fetched_review_count",
            "review_pages_fetched", "block_reason", "last_error", "variant_asin",
        }
        # 整数列：透传前必须过 _nullable_int，空字符串/脏数据转 None
        int_columns = {
            "next_review_page", "review_page_limit", "reported_rating_count",
            "reported_review_count", "fetched_review_count", "review_pages_fetched",
        }
        for name, value in state_fields.items():
            if name in allowed:
                # task_stage 归一：batch_item 只有 product/reviews 两阶段，
                # worker 的 'complete'（全部完成）归入 reviews（终态由 status 表达）
                if name == "task_stage" and str(value).lower() == "complete":
                    value = "reviews"
                sets[name] = (f"{name}=%s", BatchStore._nullable_int(value) if name in int_columns else value)
        # 按序展开：占位符与参数一一对齐
        assignments = [fragment for fragment, _ in sets.values()]
        params = [value for fragment, value in sets.values() if fragment.endswith("=%s")]
        params.extend([task["batch_id"], asin])
        cursor.execute(
            f"""
            UPDATE amazon_us.batch_item SET {', '.join(assignments)}
            WHERE batch_id=%s AND asin=%s
            """,
            params,
        )
        cursor.execute(
            """
            INSERT INTO amazon_us.state_history
              (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
            VALUES (%s,'US',%s,%s,%s,%s,%s)
            """,
            (self.tenant_id, asin, self.subject_type, from_status, new_status, reason),
        )

    # ------------------------------------------------------------------
    # 5. 保存失败（兼容 worker 的 save_failure 接口）
    # ------------------------------------------------------------------
    def save_failure(
        self,
        *,
        task: Mapping[str, Any],
        reason: str,
        error: str,
        evidence: Mapping[str, Any] | None = None,
        next_status: str = "failed",
        state_fields: Mapping[str, Any] | None = None,
        increment_attempts: bool = True,
        terminal: bool = False,
        variant_asin: str | None = None,
    ) -> bool:
        """保存失败/延后重试：带三分类，释放租约。

        variant_asin 非空 = 变体跳转：页面有效但返回的是另一个 ASIN，
        成员进 variant 终态（不是失败也不是原商品成功），
        attempts 直接置满（重试结果相同，没有意义）。
        """
        asin = str(task.get("asin", "")).strip().upper()
        fields = dict(state_fields or {})
        new_status = self._normalize_status(next_status, str(fields.get("task_stage") or task.get("task_stage") or "product"))
        if terminal:
            new_status = "failed"
        if variant_asin:
            # 变体跳转终态：不走 terminal 的 failed 映射
            new_status = "variant"
            fields.setdefault("variant_asin", variant_asin)
            fields.setdefault("last_error", f"{error}: 实际页面 ASIN={variant_asin}")
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    current = self._lock_leased_item(cursor, task)
                    if current is None:
                        conn.rollback()
                        return False
                    if evidence is not None:
                        self._write_evidence(cursor, task, evidence)
                    # 记录错误分类（blocked / fetch / system）
                    fields.setdefault("last_error", error)
                    if new_status in {"failed", "blocked"}:
                        fields.setdefault("block_reason", fields.get("block_reason"))
                    self._finish_item(cursor, task, new_status, fields, reason, current["status"])
                    # attempts 计数（延后重试不计数；terminal/变体直接置满防重复领取）
                    if increment_attempts or terminal or variant_asin:
                        cursor.execute(
                            """
                            UPDATE amazon_us.batch_item
                            SET attempts=CASE WHEN %s THEN max_attempts ELSE attempts+1 END
                            WHERE batch_id=%s AND asin=%s
                            """,
                            (terminal or bool(variant_asin), task["batch_id"], asin),
                        )
                    self._sync_item_state_cache(cursor, asin, str(task.get("url", "")), new_status, fields)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    # ------------------------------------------------------------------
    # 6. 保存评论页结果（兼容 worker 的 save_review_result 接口）
    # ------------------------------------------------------------------
    def save_review_result(
        self,
        *,
        task: Mapping[str, Any],
        evidence: Mapping[str, Any],
        page: Mapping[str, Any],
        records: Iterable[Mapping[str, Any]],
        summary: Mapping[str, Any],
        next_status: str,
        state_fields: Mapping[str, Any],
        reason: str,
        increment_attempts: bool = False,
    ) -> bool:
        """保存一页评论 + 记录 + 汇总 + 成员断点续采状态。"""
        asin = str(task.get("asin", "")).strip().upper()
        new_status = self._normalize_status(next_status, str(state_fields.get("task_stage") or "reviews"))
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    current = self._lock_leased_item(cursor, task)
                    if current is None:
                        conn.rollback()
                        return False
                    self._write_evidence(cursor, task, evidence)
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.review_page_state
                          (tenant_id, marketplace, asin, subject_type, page, url, status, next_url, fetched_at)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (tenant_id, marketplace, asin, subject_type, page) DO UPDATE SET
                          url=EXCLUDED.url, status=EXCLUDED.status, next_url=EXCLUDED.next_url,
                          fetched_at=CURRENT_TIMESTAMP
                        """,
                        (self.tenant_id, asin, self.subject_type,
                         self._int(page.get("page"), 1), page.get("url"),
                         page.get("status"), page.get("next_url")),
                    )
                    for record in records:
                        cursor.execute(
                            """
                            INSERT INTO amazon_us.review_record
                              (tenant_id, marketplace, asin, subject_type, review_id, rating, title, body,
                               review_url, review_date, locale, verified, body_truncated, review_images,
                               page, unique_key)
                            VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (tenant_id, marketplace, asin, subject_type, review_id) DO UPDATE SET
                              rating=EXCLUDED.rating, title=EXCLUDED.title, body=EXCLUDED.body,
                              review_url=EXCLUDED.review_url, review_date=EXCLUDED.review_date,
                              review_images=EXCLUDED.review_images, page=EXCLUDED.page
                            """,
                            (self.tenant_id, asin, self.subject_type, record.get("review_id"),
                             record.get("rating"), record.get("title"), record.get("body"),
                             record.get("review_url"), record.get("review_date"), record.get("locale"),
                             bool(record.get("verified")), bool(record.get("body_truncated")),
                             self._jsonb(record.get("review_images") or []),
                             self._nullable_int(record.get("page")), record.get("unique_key")),
                        )
                    cursor.execute(
                        """
                        INSERT INTO amazon_us.review_summary
                          (tenant_id, marketplace, asin, subject_type, reported_rating_count,
                           reported_review_count, reported_count_source, fetched_count, pages_fetched,
                           next_page, status, updated_at)
                        VALUES (%s,'US',%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
                        ON CONFLICT (tenant_id, marketplace, asin, subject_type) DO UPDATE SET
                          fetched_count=EXCLUDED.fetched_count, pages_fetched=EXCLUDED.pages_fetched,
                          next_page=EXCLUDED.next_page, status=EXCLUDED.status, updated_at=CURRENT_TIMESTAMP
                        """,
                        (self.tenant_id, asin, self.subject_type,
                         self._nullable_int(summary.get("reported_rating_count")),
                         self._nullable_int(summary.get("reported_review_count")),
                         summary.get("reported_count_source"),
                         self._int(summary.get("fetched_count"), 0),
                         self._int(summary.get("pages_fetched"), 0),
                         summary.get("next_page"), summary.get("status")),
                    )
                    self._finish_item(cursor, task, new_status, state_fields, reason, current["status"])
                    if increment_attempts:
                        cursor.execute(
                            "UPDATE amazon_us.batch_item SET attempts=attempts+1 WHERE batch_id=%s AND asin=%s",
                            (task["batch_id"], asin),
                        )
                    self._sync_item_state_cache(cursor, asin, str(task.get("url", "")), new_status, state_fields)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True

    # ------------------------------------------------------------------
    # 7. 过期回收：成员租约 + 容量槽
    # ------------------------------------------------------------------
    def reclaim_expired_items(self) -> int:
        """回收过期成员租约：running → 回退到 resume_status（可重新领取）。"""
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH expired AS (
                          SELECT batch_id, asin, resume_status
                          FROM amazon_us.batch_item
                          WHERE status='running' AND lease_expires_at IS NOT NULL
                            AND lease_expires_at<=CURRENT_TIMESTAMP
                          FOR UPDATE SKIP LOCKED
                        ), changed AS (
                          UPDATE amazon_us.batch_item i
                          SET status=COALESCE(NULLIF(e.resume_status,''),'pending'),
                              lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL,
                              updated_at=CURRENT_TIMESTAMP
                          FROM expired e
                          WHERE i.batch_id=e.batch_id AND i.asin=e.asin
                          RETURNING i.batch_id, i.asin, e.resume_status AS from_status, i.status
                        )
                        INSERT INTO amazon_us.state_history
                          (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
                        SELECT %s,'US',asin,%s,'running',status,'batch_lease_expired'
                        FROM changed
                        """,
                        (self.tenant_id, self.subject_type),
                    )
                    count = cursor.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return count

    def release_worker_leases(self, batch_id: str, worker_ids: list[str]) -> int:
        """协调器杀掉 worker 后，立即释放它们持有的成员租约。

        Windows 下 terminate() 是直接杀进程，worker 的收尾代码不会执行，
        不主动释放的话，批次收尾要干等整个租约期（默认 10 分钟）。
        释放后成员回退到 resume_status，停止流程里随后会被取消，
        协调器退出流程里则等待下次继续。
        """
        if not worker_ids:
            return 0
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH released AS (
                            UPDATE amazon_us.batch_item
                            SET status=COALESCE(NULLIF(resume_status,''),'pending'),
                                lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE batch_id=%s AND status='running'
                              AND lease_owner = ANY(%s)
                            RETURNING asin,
                                      COALESCE(NULLIF(resume_status,''),'pending') AS to_status
                        )
                        INSERT INTO amazon_us.state_history
                          (tenant_id, marketplace, asin, subject_type, from_status, to_status, reason)
                        SELECT %s,'US',asin,%s,'running',to_status,'worker_killed'
                        FROM released
                        """,
                        (batch_id, list(worker_ids), self.tenant_id, self.subject_type),
                    )
                    count = cursor.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return count

    def reclaim_expired_slots(self) -> int:
        """回收过期容量槽：租约到期的槽置为空闲（崩溃的 worker 不会再回来）。"""
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE amazon_us.egress_slot
                        SET worker_id=NULL, batch_id=NULL, lease_expires_at=NULL, heartbeat_at=NULL
                        WHERE lease_expires_at IS NOT NULL AND lease_expires_at<=CURRENT_TIMESTAMP
                        """,
                    )
                    count = cursor.rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return count

    # ------------------------------------------------------------------
    # 8. 容量槽租约：acquire / renew / release
    # ------------------------------------------------------------------
    def acquire_slot(
        self,
        worker_id: str,
        batch_id: str | None = None,
        *,
        egress_id: str | None = None,
        slot_seconds: int | None = None,
        timeout_seconds: float = 0.0,
    ) -> dict[str, Any] | None:
        """租一个容量槽；没有空闲槽时可选等待。

        一行一槽的物理约束保证容量永不超卖；SKIP LOCKED 保证
        两个 worker 同时租槽互不阻塞、绝不租到同一个。
        """
        import time as _time
        seconds = int(slot_seconds if slot_seconds is not None else self.default_slot_seconds)
        deadline = _time.monotonic() + max(0.0, timeout_seconds) if timeout_seconds > 0 else None
        # egress_id 为空时不过滤出口（None 参数会让 PG 无法推断类型，所以动态拼 SQL）
        egress_clause = "AND e.egress_id=%s" if egress_id else ""
        while True:
            with self._connect_factory() as conn:
                try:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            f"""
                            WITH candidate AS (
                              SELECT s.egress_id, s.slot_index
                              FROM amazon_us.egress_slot s
                              JOIN amazon_us.egress_endpoint e ON e.egress_id=s.egress_id
                              WHERE e.enabled
                                AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=CURRENT_TIMESTAMP)
                              {egress_clause}
                              ORDER BY s.egress_id, s.slot_index
                              FOR UPDATE OF s SKIP LOCKED
                              LIMIT 1
                            )
                            UPDATE amazon_us.egress_slot s
                            SET worker_id=%s, batch_id=%s,
                                lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),
                                heartbeat_at=CURRENT_TIMESTAMP
                            FROM candidate c
                            WHERE s.egress_id=c.egress_id AND s.slot_index=c.slot_index
                            RETURNING s.egress_id, s.slot_index, s.lease_expires_at
                            """,
                            tuple(
                                ([egress_id] if egress_id else [])
                                + [worker_id, batch_id, seconds]
                            ),
                        )
                        slot = self._as_dict(cursor.fetchone())
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            if slot is not None:
                return slot
            if deadline is None or _time.monotonic() >= deadline:
                return None
            _time.sleep(min(2.0, max(0.2, deadline - _time.monotonic())))

    def renew_slot(self, egress_id: str, slot_index: int, worker_id: str, *, slot_seconds: int | None = None) -> bool:
        """容量槽心跳续期；续不上（已被回收）返回 False。"""
        seconds = int(slot_seconds if slot_seconds is not None else self.default_slot_seconds)
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE amazon_us.egress_slot
                        SET lease_expires_at=CURRENT_TIMESTAMP+(%s*INTERVAL '1 second'),
                            heartbeat_at=CURRENT_TIMESTAMP
                        WHERE egress_id=%s AND slot_index=%s AND worker_id=%s
                          AND (lease_expires_at IS NULL OR lease_expires_at>CURRENT_TIMESTAMP)
                        """,
                        (seconds, egress_id, slot_index, worker_id),
                    )
                    ok = cursor.rowcount > 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ok

    def release_slot(self, egress_id: str, slot_index: int, worker_id: str) -> bool:
        """主动释放容量槽（worker 优雅退出时调用）。"""
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE amazon_us.egress_slot
                        SET worker_id=NULL, batch_id=NULL, lease_expires_at=NULL, heartbeat_at=NULL
                        WHERE egress_id=%s AND slot_index=%s AND worker_id=%s
                        """,
                        (egress_id, slot_index, worker_id),
                    )
                    ok = cursor.rowcount > 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ok

    def count_free_slots(self) -> int:
        """当前可用的容量槽数量（协调器用来判断要不要 spawn worker）。

        如果 egress_endpoint 没有启用的出口，或启用了但 slot 总数为 0——
        说明系统没配置容量，返回一个大的默认值表示"不限量"，
        让没配容量的环境也能正常跑。
        """
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    # 先看有没有启用的出口 + slot 总数——两个都要有才是"配了容量"
                    cursor.execute(
                        """
                        SELECT
                          (SELECT COUNT(*) FROM amazon_us.egress_endpoint WHERE enabled) AS endpoint_count,
                          (SELECT COUNT(*) FROM amazon_us.egress_slot) AS slot_count
                        """
                    )
                    row = self._as_dict(cursor.fetchone())
                    endpoint_count = int(row["endpoint_count"]) if row else 0
                    slot_count = int(row["slot_count"]) if row else 0
                    if endpoint_count == 0 or slot_count == 0:
                        return 999
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS free_count
                        FROM amazon_us.egress_slot s
                        JOIN amazon_us.egress_endpoint e ON e.egress_id=s.egress_id
                        WHERE e.enabled
                          AND (s.lease_expires_at IS NULL OR s.lease_expires_at<=CURRENT_TIMESTAMP)
                        """
                    )
                    row = self._as_dict(cursor.fetchone())
                    count = int(row["free_count"]) if row else 0
            except Exception:
                conn.rollback()
                raise
        return count

    # ------------------------------------------------------------------
    # 9. 批次生命周期：状态推进 / 停止 / 收尾
    # ------------------------------------------------------------------
    def start_batch(self, batch_id: str) -> bool:
        """批次启动：pending → canary（先跑探针成员）。

        标记前 CANARY_SIZE 个成员为探针（is_canary=true），批次进入 canary 状态；
        此后 claim_task 只领探针成员，evaluate_canary 通过后批次转 running 放开全量。
        """
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    # 标记前 CANARY_SIZE 个成员为探针（按 ASIN 排序保证确定性）
                    cursor.execute(
                        """
                        WITH ranked AS (
                          SELECT asin FROM amazon_us.batch_item
                          WHERE batch_id=%s
                          ORDER BY asin
                          LIMIT %s
                        )
                        UPDATE amazon_us.batch_item i
                        SET is_canary=true, updated_at=CURRENT_TIMESTAMP
                        FROM ranked r WHERE i.batch_id=%s AND i.asin=r.asin
                        """,
                        (batch_id, CANARY_SIZE, batch_id),
                    )
                    cursor.execute(
                        """
                        UPDATE amazon_us.batch
                        SET status='canary', started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                            updated_at=CURRENT_TIMESTAMP
                        WHERE batch_id=%s AND status IN ('pending','provisioning')
                        """,
                        (batch_id,),
                    )
                    ok = cursor.rowcount > 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ok

    def evaluate_canary(self, batch_id: str) -> dict[str, Any] | None:
        """评估 canary 探针结果（协调器每轮调用）。

        返回 None：探针还有未完成成员（pending/running/可重试 failed），继续等。
        返回 {"canary_passed": True}：探针全部成功，批次已转 running（放开全量）。
        返回 {"canary_passed": False, ...}：探针有终态失败，批次保持 canary，
        由调用方调 finalize_batch 收尾（残余成员取消，避免全量冲出去）。
        """
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        "SELECT status FROM amazon_us.batch WHERE batch_id=%s FOR UPDATE",
                        (batch_id,),
                    )
                    batch = self._as_dict(cursor.fetchone())
                    if batch is None or batch["status"] != "canary":
                        conn.rollback()
                        return None
                    # 探针成员状态汇总（total 含全部探针，含终态失败）
                    cursor.execute(
                        """
                        SELECT COUNT(*) AS total,
                               COUNT(*) FILTER (WHERE status='succeeded') AS succeeded,
                               COUNT(*) FILTER (WHERE status='variant') AS variant,
                               COUNT(*) FILTER (WHERE status='pending') AS pending,
                               COUNT(*) FILTER (WHERE status='running') AS running,
                               COUNT(*) FILTER (WHERE status='failed' AND attempts<max_attempts) AS retryable
                        FROM amazon_us.batch_item
                        WHERE batch_id=%s AND is_canary
                        """,
                        (batch_id,),
                    )
                    s = self._as_dict(cursor.fetchone()) or {}
                    # 还有未完成/可重试的探针 → 继续等。
                    # 可重试的判定不能带 next_retry_at<=now：代理轮换模式把被拦探针
                    # 写成 failed+next_retry_at=未来（延迟重试语义），若只认"重试已到期"，
                    # 等待重试的探针既不算 pending/running 也不算 retryable → 被误判
                    # 全部终态 → canary 假失败 → 整批被取消（2026-09-09 连杀两批 937 条的事故）
                    if int(s.get("pending") or 0) > 0 or int(s.get("running") or 0) > 0 \
                            or int(s.get("retryable") or 0) > 0:
                        conn.rollback()
                        return None
                    # 探针全部到终态：有效采集（成功+变体）全占才放开——
                    # 变体跳转说明出口是通的（页面正常抓到并解析），不代表被拦
                    effective = int(s.get("succeeded") or 0) + int(s.get("variant") or 0)
                    passed = int(s.get("total") or 0) > 0 and effective == int(s.get("total") or 0)
                    if passed:
                        cursor.execute(
                            """
                            UPDATE amazon_us.batch
                            SET status='running', updated_at=CURRENT_TIMESTAMP
                            WHERE batch_id=%s AND status='canary'
                            """,
                            (batch_id,),
                        )
                        conn.commit()
                        return {"canary_passed": True, "canary_total": s.get("total")}
                    conn.commit()
                    return {"canary_passed": False, "canary_total": s.get("total"),
                            "canary_succeeded": s.get("succeeded")}
            except Exception:
                conn.rollback()
                raise

    def request_stop(self, batch_id: str) -> bool:
        """用户请求停止：置 stop_requested，worker 领不到新任务自然收尾。

        canary 状态同样可停：探针阶段用户要求停止，批次转 stopping，
        由协调器停 worker 后 finalize 收尾（残余成员取消）。
        """
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE amazon_us.batch
                        SET stop_requested=true,
                            status=CASE WHEN status IN ('running','canary') THEN 'stopping' ELSE status END,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE batch_id=%s AND status IN ('pending','provisioning','canary','running')
                        """,
                        (batch_id,),
                    )
                    ok = cursor.rowcount > 0
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ok

    def batch_summary(self, batch_id: str) -> dict[str, Any] | None:
        """读 batch_progress 视图（进度的唯一推导处）。"""
        with self._connect_factory() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM amazon_us.batch_progress WHERE batch_id=%s",
                    (batch_id,),
                )
                return self._as_dict(cursor.fetchone())

    def finalize_batch(self, batch_id: str, *, force: bool = False) -> dict[str, Any] | None:
        """批次收尾：把残余 pending 取消，按失败构成写终态。

        规则：
          - 全部成功 → completed
          - 有 system 类失败 → failed（final_failure_class=system）
          - 有 fetch 类失败 → failed（final_failure_class=fetch）
          - 剩余全是被拦 → blocked
          - 停止导致未完成 → stopped
        幂等：批次已是终态则直接返回。

        force=True：canary 失败收尾专用——即使批次没被用户停止、
        还有大量 pending 成员（全量从未放开），也取消残余并按
        探针失败构成写终态（blocked/failed），不写 stopped。
        """
        with self._connect_factory() as conn:
            try:
                with conn.cursor() as cursor:
                    # 已终态直接返回
                    cursor.execute(
                        "SELECT status FROM amazon_us.batch WHERE batch_id=%s FOR UPDATE",
                        (batch_id,),
                    )
                    row = self._as_dict(cursor.fetchone())
                    if row is None:
                        conn.rollback()
                        return None
                    if row["status"] in {"completed", "blocked", "failed", "stopped"}:
                        conn.rollback()
                        return self.batch_summary(batch_id)
                    # 残余 pending（含等待重试的 failed）在停止时取消
                    cursor.execute(
                        """
                        UPDATE amazon_us.batch_item
                        SET status='cancelled', lease_token=NULL, lease_owner=NULL,
                            lease_expires_at=NULL, finalized_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE batch_id=%s AND status IN ('pending','running')
                          AND lease_expires_at IS NOT NULL AND lease_expires_at<=CURRENT_TIMESTAMP
                        """,
                        (batch_id,),
                    )
                    # 成员状态汇总
                    cursor.execute(
                        """
                        SELECT
                          COUNT(*) AS total,
                          COUNT(*) FILTER (WHERE status='succeeded') AS succeeded,
                          COUNT(*) FILTER (WHERE status='blocked') AS blocked,
                          COUNT(*) FILTER (WHERE status='failed' AND error_class='fetch') AS failed_fetch,
                          COUNT(*) FILTER (WHERE status='failed' AND error_class='system') AS failed_system,
                          COUNT(*) FILTER (WHERE status='cancelled') AS cancelled,
                          COUNT(*) FILTER (WHERE status='variant') AS variant,
                          COUNT(*) FILTER (WHERE status='pending') AS pending,
                          COUNT(*) FILTER (WHERE status='running'
                                            AND lease_expires_at > CURRENT_TIMESTAMP) AS running_live
                        FROM amazon_us.batch_item WHERE batch_id=%s
                        """,
                        (batch_id,),
                    )
                    s = self._as_dict(cursor.fetchone()) or {}
                    # 还有活租约 → 不收尾（等 worker 写完）
                    if int(s.get("running_live") or 0) > 0:
                        conn.rollback()
                        return None
                    stop_requested = False
                    cursor.execute(
                        "SELECT stop_requested FROM amazon_us.batch WHERE batch_id=%s", (batch_id,),
                    )
                    stop_requested = bool((self._as_dict(cursor.fetchone()) or {}).get("stop_requested"))
                    # 计算终态
                    if int(s.get("pending") or 0) > 0:
                        # 有 pending：要么停止中（取消），要么还有可重试任务（不该收尾）；
                        # force（canary 失败）例外：全量从未放开，残余一律取消
                        if not stop_requested and not force:
                            conn.rollback()
                            return None
                        cursor.execute(
                            """
                            UPDATE amazon_us.batch_item
                            SET status='cancelled', lease_token=NULL, lease_owner=NULL,
                                lease_expires_at=NULL, finalized_at=CURRENT_TIMESTAMP,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE batch_id=%s AND status='pending'
                            """,
                            (batch_id,),
                        )
                    elif not stop_requested and not force:
                        # 无 pending 但可能有等待重试的 failed（代理轮换延迟重试语义：
                        # failed+attempts<max+next_retry_at 在未来）。此时批次没跑完，
                        # 不收尾——等重试到期由协调器重新拉起 worker 跑完剩余 attempts。
                        # 若在这里收尾，批次尾段（剩余条目全在重试等待中、worker 领不到
                        # 任务退出）会被 prematurely 判成 failed/blocked 终态。
                        cursor.execute(
                            """
                            SELECT COUNT(*) AS retry_waiting FROM amazon_us.batch_item
                            WHERE batch_id=%s
                              AND status='failed' AND attempts<max_attempts
                            """,
                            (batch_id,),
                        )
                        if int((self._as_dict(cursor.fetchone()) or {}).get("retry_waiting") or 0) > 0:
                            conn.rollback()
                            return None
                    failed_total = int(s.get("failed_fetch") or 0) + int(s.get("failed_system") or 0)
                    # 变体跳转不算失败也不算原商品成功：完成度按 成功+变体 计
                    done_count = int(s.get("succeeded") or 0) + int(s.get("variant") or 0)
                    if done_count >= int(s.get("total") or 0):
                        final_status, failure_class = "completed", None
                    elif stop_requested and int(s.get("cancelled") or 0) > 0:
                        final_status = "stopped"
                        failure_class = (
                            "system" if int(s.get("failed_system") or 0) > 0
                            else "fetch" if int(s.get("failed_fetch") or 0) > 0
                            else None
                        )
                    elif int(s.get("failed_system") or 0) > 0:
                        final_status, failure_class = "failed", "system"
                    elif int(s.get("failed_fetch") or 0) > 0:
                        final_status, failure_class = "failed", "fetch"
                    elif int(s.get("blocked") or 0) > 0:
                        final_status, failure_class = "blocked", "blocked"
                    else:
                        # 只有成功+取消的组合且未被用户停止（理论少见）
                        final_status, failure_class = "stopped" if stop_requested else "completed", None
                    cursor.execute(
                        """
                        UPDATE amazon_us.batch
                        SET status=%s, final_failure_class=%s, finalized_at=CURRENT_TIMESTAMP,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE batch_id=%s
                        """,
                        (final_status, failure_class, batch_id),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self.batch_summary(batch_id)


__all__ = ["BatchStore", "BatchStoreError"]
