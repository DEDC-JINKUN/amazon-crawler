"""Product-watch v1 normalization, comparison and persistence contract."""

from .changes import detect_changes
from .models import ChangeEvent, Observation
from .normalize import normalize_keyword_rank, normalize_snapshot
from .repository import ProductWatchRepository
from .pipeline import PostgresSnapshotIngestor, SearchRankRecoveryIngestor, WatchPipeline
from .delivery_repository import WatchDeliveryRepository

__all__ = [
    "ChangeEvent", "Observation", "ProductWatchRepository", "detect_changes",
    "normalize_keyword_rank", "normalize_snapshot",
    "WatchPipeline", "PostgresSnapshotIngestor", "SearchRankRecoveryIngestor", "WatchDeliveryRepository",
]
