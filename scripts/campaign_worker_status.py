"""Immutable file protocol for campaign worker heartbeat snapshots."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, NamedTuple


STATUS_SCHEMA = "amazon-campaign-worker-status-v1"
STATUS_SNAPSHOT_KEEP = 3
STATUS_SNAPSHOT_MAX_FILES = 64
STATUS_SNAPSHOT_MAX_BYTES = 1024 * 1024
STATUS_SNAPSHOT_MAX_DOCUMENT_BYTES = 4096
_TOKEN_PATTERN = re.compile(r"[a-f0-9]{32}")


class SnapshotCleanup(NamedTuple):
    remaining_files: int
    remaining_bytes: int
    delete_failures: int
    inventory_complete: bool


def _validate_launch_token(launch_token: str) -> None:
    if not _TOKEN_PATTERN.fullmatch(launch_token):
        raise ValueError("worker launch token is invalid")


def _snapshot_name(path: Path, launch_token: str, sequence: int) -> str:
    return f"{path.stem}.{launch_token}.{sequence:020d}{path.suffix}"


def _snapshot_pattern(path: Path, launch_token: str) -> str:
    return f"{path.stem}.{launch_token}.*{path.suffix}"


def _read_document(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _candidate_sequence(path: Path, candidate: Path, launch_token: str) -> int | None:
    prefix = f"{path.stem}.{launch_token}."
    name = candidate.name
    if not name.startswith(prefix) or not name.endswith(path.suffix):
        return None
    raw = name[len(prefix):-len(path.suffix)] if path.suffix else name[len(prefix):]
    if len(raw) != 20 or not raw.isdigit():
        return None
    return int(raw)


def _candidate_identity(path: Path, candidate: Path) -> tuple[str, int] | None:
    prefix = f"{path.stem}."
    name = candidate.name
    if not name.startswith(prefix) or not name.endswith(path.suffix):
        return None
    raw = name[len(prefix):-len(path.suffix)] if path.suffix else name[len(prefix):]
    parts = raw.split(".")
    if len(parts) != 2:
        return None
    launch_token, sequence = parts
    if not _TOKEN_PATTERN.fullmatch(launch_token) or len(sequence) != 20 or not sequence.isdigit():
        return None
    return launch_token, int(sequence)


def publish_status_snapshot(
    path: Path,
    payload: dict[str, Any],
    *,
    launch_token: str,
    sequence: int,
) -> Path:
    """Publish a new immutable snapshot without replacing a reader-held file."""
    _validate_launch_token(launch_token)
    if type(sequence) is not int:
        raise ValueError("worker status sequence must be an integer")
    if sequence < 1:
        raise ValueError("worker status sequence must be positive")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = dict(payload)
    document["schema"] = STATUS_SCHEMA
    document["launch_token"] = launch_token
    document["sequence"] = sequence
    cleanup = cleanup_status_snapshots(
        path, active_launch_token=launch_token, keep_active=STATUS_SNAPSHOT_KEEP - 1,
    )
    document["snapshot_backlog_files"] = cleanup.remaining_files + 1
    document["snapshot_cleanup_failures"] = cleanup.delete_failures
    document["snapshot_backlog_bytes"] = cleanup.remaining_bytes
    for _attempt in range(4):
        encoded = json.dumps(document, separators=(",", ":"))
        projected_bytes = cleanup.remaining_bytes + len(encoded.encode("utf-8"))
        if document["snapshot_backlog_bytes"] == projected_bytes:
            break
        document["snapshot_backlog_bytes"] = projected_bytes
    encoded = json.dumps(document, separators=(",", ":"))
    encoded_bytes = len(encoded.encode("utf-8"))
    if encoded_bytes > STATUS_SNAPSHOT_MAX_DOCUMENT_BYTES:
        raise OSError("worker status snapshot exceeds document size limit")
    if status_snapshot_backlog_exceeded(cleanup, additional_files=1, additional_bytes=encoded_bytes):
        raise OSError("worker status snapshot backlog exceeds safe limit")
    snapshot = path.with_name(_snapshot_name(path, launch_token, sequence))
    with snapshot.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
    return snapshot


def read_latest_status_snapshot(path: Path, *, launch_token: str) -> dict[str, Any] | None:
    """Return the latest complete snapshot for one launch, falling back to legacy state."""
    _validate_launch_token(launch_token)
    try:
        candidates = sorted(path.parent.glob(_snapshot_pattern(path, launch_token)), reverse=True)
    except OSError:
        candidates = []
    for candidate in candidates:
        sequence = _candidate_sequence(path, candidate, launch_token)
        if sequence is None:
            continue
        document = _read_document(candidate)
        if (
            document is not None
            and document.get("schema") == STATUS_SCHEMA
            and document.get("launch_token") == launch_token
            and type(document.get("sequence")) is int
            and document.get("sequence") >= 1
            and document.get("sequence") == sequence
        ):
            return document

    legacy = _read_document(path)
    if (
        legacy is not None
        and legacy.get("schema") == STATUS_SCHEMA
        and legacy.get("launch_token") == launch_token
    ):
        return legacy
    return None


def cleanup_status_snapshots(
    path: Path,
    *,
    active_launch_token: str | None = None,
    keep_active: int = 0,
) -> SnapshotCleanup:
    """Best-effort cleanup across launches with explicit remaining-backlog facts."""
    if active_launch_token is not None:
        _validate_launch_token(active_launch_token)
    try:
        candidates = [
            (candidate, identity)
            for candidate in path.parent.glob(f"{path.stem}.*{path.suffix}")
            if (identity := _candidate_identity(path, candidate)) is not None
        ]
    except OSError:
        return SnapshotCleanup(
            remaining_files=0,
            remaining_bytes=0,
            delete_failures=0,
            inventory_complete=False,
        )
    active = sorted(
        (item for item in candidates if item[1][0] == active_launch_token),
        key=lambda item: item[1][1],
        reverse=True,
    )
    retained = {candidate for candidate, _identity in active[:max(0, keep_active)]}
    remaining: list[Path] = list(retained)
    delete_failures = 0
    for candidate, _identity in candidates:
        if candidate in retained:
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            remaining.append(candidate)
            delete_failures += 1
    remaining_bytes = 0
    inventory_complete = True
    for candidate in remaining:
        try:
            remaining_bytes += candidate.stat().st_size
        except OSError:
            inventory_complete = False
    return SnapshotCleanup(
        remaining_files=len(remaining),
        remaining_bytes=remaining_bytes,
        delete_failures=delete_failures,
        inventory_complete=inventory_complete,
    )


def status_snapshot_backlog_exceeded(
    cleanup: SnapshotCleanup,
    *,
    additional_files: int = 0,
    additional_bytes: int = 0,
) -> bool:
    return (
        not cleanup.inventory_complete
        or cleanup.remaining_files + additional_files > STATUS_SNAPSHOT_MAX_FILES
        or cleanup.remaining_bytes + additional_bytes > STATUS_SNAPSHOT_MAX_BYTES
    )


def prepare_status_snapshot_launch(path: Path) -> SnapshotCleanup:
    """Clear old launches and reserve bounded space before starting a worker."""
    cleanup = cleanup_status_snapshots(path)
    if status_snapshot_backlog_exceeded(
        cleanup,
        additional_files=STATUS_SNAPSHOT_KEEP,
        additional_bytes=STATUS_SNAPSHOT_KEEP * STATUS_SNAPSHOT_MAX_DOCUMENT_BYTES,
    ):
        raise OSError("worker status snapshot backlog exceeds safe limit")
    return cleanup
