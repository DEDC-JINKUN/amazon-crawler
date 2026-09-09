from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "schedule_postgres_refresh.py"


def load_module():
    spec = importlib.util.spec_from_file_location("schedule_postgres_refresh_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_scheduler_reads_dsn_from_environment_and_reports_queued_count(capsys):
    module = load_module()

    class Storage:
        def __init__(self, dsn, tenant_id, subject_type):
            assert dsn == "postgresql://from-env"
            assert (tenant_id, subject_type) == ("tenant-a", "own")

        def enqueue_due_refreshes(self, min_age_hours, limit):
            assert (min_age_hours, limit) == (24, 100)
            return 7

    with patch.object(module, "PostgresWorkerStorage", Storage), patch.dict(
        "os.environ", {"TEST_POSTGRES_DSN": "postgresql://from-env"}, clear=True
    ):
        assert module.main([
            "--dsn-env", "TEST_POSTGRES_DSN", "--tenant-id", "tenant-a", "--subject-type", "own",
            "--min-age-hours", "24", "--limit", "100",
        ]) == 0

    assert json.loads(capsys.readouterr().out)["queued"] == 7

