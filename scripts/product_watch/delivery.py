"""Small, explicit delivery boundary for product-watch reports."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Protocol


class DeliveryChannel(Protocol):
    name: str
    def send(self, delivery: Mapping[str, Any], report: Mapping[str, Any]) -> Mapping[str, Any]: ...


class LocalReceiptChannel:
    """A test/local outlet; its durable file receipt is the confirmation boundary."""
    name = "local_receipt"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def send(self, delivery: Mapping[str, Any], report: Mapping[str, Any]) -> Mapping[str, Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{delivery['delivery_id']}.json"
        if not path.exists():
            path.write_text(json.dumps({"delivery_id": delivery["delivery_id"], "report_id": report["report_id"], "content": report["content_json"]}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return {"confirmed": True, "channel": self.name, "receipt_path": str(path.name), "idempotency_key": delivery["dedupe_key"]}


class UnconfiguredChannel:
    """Never turns a locally rendered report into a claimed external delivery."""
    name = "unconfigured"

    def send(self, delivery: Mapping[str, Any], report: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"confirmed": False, "status": "unknown", "reason": "recipient_or_channel_not_configured"}
