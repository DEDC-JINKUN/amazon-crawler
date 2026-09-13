#!/usr/bin/env python3
"""Stdio MCP adapter for the scoped Amazon Collection REST API."""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx2
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

BASE_URL_ENV = "AMAZON_COLLECTION_MCP_BASE_URL"
AGENT_ID_ENV = "AMAZON_COLLECTION_AGENT_ID"
AGENT_KEY_ENV = "AMAZON_COLLECTION_AGENT_KEY"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
UNRECOVERABLE_WAIT_STATUSES = frozenset({400, 401, 403, 404, 409})
ASIN_PATTERN = r"^[A-Z0-9]{10}$"
CAMPAIGN_PATTERN = r"^campaign-[a-f0-9]{32}$"
JOB_PATTERN = r"^[A-Za-z0-9_-]{1,160}$"
IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9_.:-]{8,160}$"

Asin = Annotated[str, Field(pattern=ASIN_PATTERN, min_length=10, max_length=10)]
AsinBatch = Annotated[list[Asin], Field(min_length=1, max_length=100)]
CampaignAsins = Annotated[list[Asin], Field(min_length=1, description="The complete natural batch. Do not split it into five-item calls.")]
CampaignId = Annotated[str, Field(pattern=CAMPAIGN_PATTERN)]
JobId = Annotated[str, Field(pattern=JOB_PATTERN)]
IdempotencyKey = Annotated[str, Field(pattern=IDEMPOTENCY_PATTERN, min_length=8, max_length=160)]
RequestedField = Literal["title", "brand", "price", "availability", "rating", "review_count", "bsr", "promotions", "buy_box"]
SubjectType = Literal["own", "competitor", "candidate"]


class ActionableError(BaseModel):
    code: str
    message: str
    action: str


class ToolEnvelope(BaseModel):
    ok: bool
    http_status: int | None = None
    data: dict[str, Any] | None = None
    error: ActionableError | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class Settings:
    base_url: str
    agent_id: str
    agent_key: str = field(repr=False)


def load_settings(environment: dict[str, str] | os._Environ[str] = os.environ) -> Settings:
    base_url = str(environment.get(BASE_URL_ENV) or "").strip().rstrip("/")
    agent_id = str(environment.get(AGENT_ID_ENV) or "").strip()
    agent_key = str(environment.get(AGENT_KEY_ENV) or "").strip()
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{BASE_URL_ENV} must be an explicit loopback HTTP origin")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{3,120}", agent_id):
        raise ValueError(f"{AGENT_ID_ENV} is missing or invalid")
    if len(agent_key) < 32:
        raise ValueError(f"{AGENT_KEY_ENV} is missing or invalid")
    return Settings(base_url=base_url, agent_id=agent_id, agent_key=agent_key)


def _action_for_status(status: int) -> str:
    if status == 400:
        return "Correct the tool arguments and retry once."
    if status in {401, 403}:
        return "Ask the operator to repair the scoped Agent credential or scope."
    if status == 404:
        return "Verify the ASIN, campaign ID, or job ID before retrying."
    if status == 409:
        return "Use the original payload for this idempotency key or choose a new stable key."
    if status == 429:
        return "Wait for retry_after_seconds, then poll again."
    if status == 503:
        return "The collection worker is unavailable; wait and retry without duplicating the campaign."
    return "Ask the operator to inspect the Collection service health."


def _error_envelope(status: int | None, code: str, *, retry_after: float | None = None) -> ToolEnvelope:
    safe_code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(code))[:80] or "backend_error"
    return ToolEnvelope(
        ok=False,
        http_status=status,
        error=ActionableError(code=safe_code, message="Collection service request did not complete.", action=_action_for_status(status or 500)),
        retry_after_seconds=retry_after,
    )


def _campaign_view(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    result = {key: value.get(key) for key in (
        "campaign_id", "marketplace", "subject_type", "priority", "requested_fields",
        "freshness_seconds", "deadline", "status", "created_at", "updated_at", "total", "terminal", "counts",
    )}
    result["members"] = [
        {key: member.get(key) for key in (
            "asin", "ordinal", "refresh_job_id", "job_status", "requested_at", "claimed_at",
            "completed_at", "item_status", "recovery_outcome", "recovery_terminal", "status",
        )}
        for member in value.get("members", []) if isinstance(member, dict)
    ]
    return result


def _campaign_all_terminal(campaign: dict[str, Any]) -> bool:
    total = int(campaign.get("total") or 0)
    terminal = int(campaign.get("terminal") or 0)
    return total > 0 and terminal == total


def _product_view(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    product = value.get("product") if isinstance(value.get("product"), dict) else {}
    task = value.get("task") if isinstance(value.get("task"), dict) else {}
    evidence = value.get("evidence") if isinstance(value.get("evidence"), dict) else {}
    return {
        "schema_version": value.get("schema_version"),
        "marketplace": value.get("marketplace"),
        "asin": value.get("asin"),
        "retrieved_at": value.get("retrieved_at"),
        "freshness": value.get("freshness"),
        "quality_status": value.get("quality_status"),
        "source": value.get("source"),
        "product": {key: product.get(key) for key in (
            "asin", "subject_type", "availability", "title", "brand", "rating", "reported_rating_count",
            "reported_review_count", "review_count", "price", "bullets", "product_description", "specs",
            "buy_box", "aplus_present", "collected_at", "status",
        )},
        "task": {key: task.get(key) for key in ("subject_type", "status", "next_retry_at", "block_reason", "last_error")},
        "evidence": {key: evidence.get(key) for key in ("http_status", "transfer_bytes", "retrieved_at", "source_type", "block_reason", "error_code")},
        "counts": value.get("counts") if isinstance(value.get("counts"), dict) else {},
    }


def _job_view(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    job = value.get("job") if isinstance(value.get("job"), dict) else {}
    result = value.get("result") if isinstance(value.get("result"), dict) else None
    safe_job = {key: job.get(key) for key in (
        "job_id", "marketplace", "asin", "subject_type", "requested_by", "reason", "status",
        "requested_at", "claimed_at", "completed_at",
    )}
    safe_result = None
    if result is not None:
        product = result.get("product") if isinstance(result.get("product"), dict) else {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        timing = result.get("timing") if isinstance(result.get("timing"), dict) else {}
        traffic = result.get("traffic") if isinstance(result.get("traffic"), dict) else {}
        safe_result = {
            "quality_status": result.get("quality_status"),
            "product": {key: product.get(key) for key in ("asin", "title", "price", "availability", "rating")},
            "evidence": {key: evidence.get(key) for key in ("run_id", "retrieved_at", "source", "http_status", "transfer_bytes", "error")},
            "evidence_after_request": result.get("evidence_after_request"),
            "timing": {key: timing.get(key) for key in ("requested_at", "claimed_at", "completed_at", "elapsed_seconds")},
            "traffic": {key: traffic.get(key) for key in (
                "transfer_bytes", "proxy_tunnel_bytes", "proxy_tunnel_client_to_upstream_bytes",
                "proxy_tunnel_upstream_to_client_bytes",
            )},
            "details_available": bool(result.get("details_available")),
        }
    return {"schema_version": value.get("schema_version"), "job": safe_job, "result": safe_result}


class CollectionAPIClient:
    def __init__(self, settings: Settings, *, timeout_seconds: float = 15.0):
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    async def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> ToolEnvelope:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Collection-Agent": self.settings.agent_id,
            "X-Collection-Agent-Key": self.settings.agent_key,
        }
        try:
            async with httpx2.AsyncClient(follow_redirects=False, trust_env=False, timeout=self.timeout_seconds) as client:
                response = await client.request(method, self.settings.base_url + path, headers=headers, json=payload)
        except (httpx2.TimeoutException, httpx2.NetworkError, OSError):
            return _error_envelope(None, "backend_unavailable")
        retry_after = None
        if response.status_code == 429:
            try:
                retry_after = max(1.1, min(float(response.headers.get("Retry-After", "1.1")), 60.0))
            except ValueError:
                retry_after = 1.1
        if 300 <= response.status_code < 400:
            return _error_envelope(response.status_code, "redirect_refused")
        try:
            document = response.json()
        except ValueError:
            return _error_envelope(response.status_code, "invalid_backend_response", retry_after=retry_after)
        if response.status_code >= 400:
            code = str(document.get("error") or "backend_error") if isinstance(document, dict) else "backend_error"
            return _error_envelope(response.status_code, code[:80], retry_after=retry_after)
        return ToolEnvelope(ok=True, http_status=response.status_code, data=document if isinstance(document, dict) else {})


_runtime_client: CollectionAPIClient | None = None


def get_runtime_client() -> CollectionAPIClient:
    global _runtime_client
    if _runtime_client is None:
        _runtime_client = CollectionAPIClient(load_settings())
    return _runtime_client


def create_server(client: CollectionAPIClient | None = None) -> MCPServer:
    resolve_client = (lambda: client) if client is not None else get_runtime_client
    server = MCPServer(
        "amazon_collection_mcp",
        instructions=(
            "Completion contract: when the user asks for collection results and wants to wait, never give a final answer while any campaign member is non-terminal. Call campaign_wait repeatedly with the same campaign_id whenever next_action is call_campaign_wait_again; never resubmit or change the idempotency key. Stop only when every member is terminal, the user explicitly requests background execution, or an unrecoverable service error is reported. campaign_status is one snapshot only. Use cached reads only for existing data and report quality/failure evidence."
        ),
        log_level="WARNING",
    )

    read_annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    submit_annotations = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
    cancel_annotations = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False)

    @server.tool(title="Collection capabilities", annotations=read_annotations, structured_output=True)
    async def collection_capabilities() -> ToolEnvelope:
        """Read supported Collection operations, limits, and transport behavior before planning a workflow."""
        result = await resolve_client().request("GET", "/v1/capabilities")
        if result.ok and result.data is not None:
            allowed = {key: result.data.get(key) for key in ("schema_version", "transport", "execution", "limits", "tools")}
            result.data = allowed
        return result

    @server.tool(title="Read cached product", annotations=read_annotations, structured_output=True)
    async def product_get(asin: Asin) -> ToolEnvelope:
        """Read the latest cached product snapshot without requesting a refresh. Report freshness and quality status."""
        result = await resolve_client().request("GET", f"/v1/asin/US/{asin}")
        if result.ok and result.data is not None:
            result.data = _product_view(result.data)
        return result

    @server.tool(title="Read cached product batch", annotations=read_annotations, structured_output=True)
    async def batch_get(asins: AsinBatch) -> ToolEnvelope:
        """Read up to 100 cached product snapshots without scheduling collection work."""
        result = await resolve_client().request("POST", "/v1/asin/batch", {"marketplace": "US", "asins": asins})
        if result.ok and result.data is not None:
            items = result.data.get("items") if isinstance(result.data.get("items"), list) else []
            result.data = {"schema_version": result.data.get("schema_version"), "items": [_product_view(item) for item in items]}
        return result

    @server.tool(title="Submit collection campaign", annotations=submit_annotations, structured_output=True)
    async def campaign_submit(
        asins: CampaignAsins,
        idempotency_key: IdempotencyKey,
        subject_type: SubjectType = "own",
        priority: Annotated[int, Field(ge=0, le=100)] = 50,
        deadline_seconds: Annotated[int, Field(ge=60, le=604800)] = 86400,
        freshness_seconds: Annotated[int | None, Field(ge=0, le=31536000)] = None,
        fields: list[RequestedField] | None = None,
    ) -> ToolEnvelope:
        """Submit one idempotent natural batch for asynchronous collection. Send the complete list; the service enforces its configured campaign quota."""
        result = await resolve_client().request("POST", "/v1/campaigns", {
            "asins": asins,
            "idempotency_key": idempotency_key,
            "subject_type": subject_type,
            "priority": priority,
            "deadline_seconds": deadline_seconds,
            "freshness_seconds": freshness_seconds,
            "fields": fields or [],
        })
        if result.ok and result.data is not None:
            result.data = {"campaign": _campaign_view(result.data.get("campaign"))}
        return result

    @server.tool(title="Read campaign progress snapshot", annotations=read_annotations, structured_output=True)
    async def campaign_status(campaign_id: CampaignId) -> ToolEnvelope:
        """Read one campaign progress snapshot. Use this when the user asks only for current progress, not when waiting for results."""
        result = await resolve_client().request("GET", f"/v1/campaigns/{campaign_id}")
        if result.ok and result.data is not None:
            campaign = _campaign_view(result.data.get("campaign"))
            terminal = _campaign_all_terminal(campaign)
            result.data = {
                "campaign": campaign,
                "progress": {
                    "terminal": terminal,
                    "next_action": "campaign_terminal" if terminal else "call_campaign_wait_if_user_requested_results",
                },
            }
        return result

    @server.tool(title="Wait for campaign results", annotations=read_annotations, structured_output=True)
    async def campaign_wait(
        campaign_id: CampaignId,
        max_wait_seconds: Annotated[int, Field(ge=1, le=300)] = 30,
        poll_interval_seconds: Annotated[float, Field(ge=1.1, le=60)] = 3.0,
    ) -> ToolEnvelope:
        """Wait for all campaign members to become terminal. If next_action is call_campaign_wait_again, call this tool again with the same campaign ID instead of answering or resubmitting."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + max_wait_seconds
        last_result: ToolEnvelope | None = None

        def timed_out(result: ToolEnvelope) -> ToolEnvelope:
            waited = round(loop.time() - started, 3)
            if result.ok and result.data is not None:
                result.data["wait"] = {
                    "terminal": False,
                    "timed_out": True,
                    "waited_seconds": waited,
                    "next_action": "call_campaign_wait_again",
                    "campaign_id": campaign_id,
                }
                return result
            return ToolEnvelope(
                ok=False,
                http_status=result.http_status,
                data={"wait": {
                    "terminal": False,
                    "timed_out": True,
                    "waited_seconds": waited,
                    "next_action": "call_campaign_wait_again",
                    "campaign_id": campaign_id,
                }},
                error=ActionableError(
                    code="poll_timeout",
                    message="Campaign did not reach terminal state before the polling timeout.",
                    action="Call campaign_wait again with the same campaign ID.",
                ),
                retry_after_seconds=result.retry_after_seconds,
            )

        while True:
            if last_result is not None and loop.time() >= deadline:
                return timed_out(last_result)
            result = await resolve_client().request("GET", f"/v1/campaigns/{campaign_id}")
            last_result = result
            if result.ok and result.data is not None:
                campaign = _campaign_view(result.data.get("campaign"))
                terminal = _campaign_all_terminal(campaign)
                elapsed = round(loop.time() - started, 3)
                result.data = {"campaign": campaign, "wait": {
                    "terminal": terminal,
                    "timed_out": False,
                    "waited_seconds": elapsed,
                    "next_action": "inspect_terminal_members_with_job_get" if terminal else "continue_waiting",
                    "campaign_id": campaign_id,
                }}
                if terminal:
                    return result
            elif result.http_status in UNRECOVERABLE_WAIT_STATUSES or (
                result.error is not None and result.error.code == "redirect_refused"
            ):
                result.data = {"wait": {
                    "terminal": False,
                    "timed_out": False,
                    "waited_seconds": round(loop.time() - started, 3),
                    "next_action": "report_unrecoverable_service_error",
                    "campaign_id": campaign_id,
                }}
                return result
            remaining = deadline - loop.time()
            if remaining <= 0:
                return timed_out(result)
            sleep_for = max(poll_interval_seconds, result.retry_after_seconds or 0.0)
            sleep_duration = min(sleep_for, remaining)
            await asyncio.sleep(sleep_duration)
            if sleep_duration >= remaining:
                return timed_out(result)

    @server.tool(title="Read collection job", annotations=read_annotations, structured_output=True)
    async def job_get(job_id: JobId) -> ToolEnvelope:
        """Read one member job summary, including quality status, evidence freshness, traffic, and a safe failure reason."""
        result = await resolve_client().request("GET", f"/v1/jobs/{job_id}?view=summary")
        if result.ok and result.data is not None:
            result.data = _job_view(result.data)
        return result

    @server.tool(title="Cancel collection campaign", annotations=cancel_annotations, structured_output=True)
    async def campaign_cancel(campaign_id: CampaignId) -> ToolEnvelope:
        """Idempotently cancel future queued work for a campaign. Already running members retain their lease and report their terminal outcome."""
        result = await resolve_client().request("POST", f"/v1/campaigns/{campaign_id}/cancel", {})
        if result.ok and result.data is not None:
            result.data = {"campaign": _campaign_view(result.data.get("campaign"))}
        return result

    return server


mcp = create_server()


def main() -> int:
    get_runtime_client()
    mcp.run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
