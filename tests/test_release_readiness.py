import json
import os
from pathlib import Path
import subprocess
import sys


def test_readiness_cli_without_injected_dsn_fails_closed():
    env=os.environ.copy(); env.pop('AMAZON_US_POSTGRES_DSN',None)
    script=Path(__file__).parents[1]/'scripts/release_readiness.py'
    result=subprocess.run([sys.executable,str(script),'--tenant-id','fixture'],env=env,capture_output=True,text=True)
    assert result.returncode==2
    assert json.loads(result.stdout)=={'ok':False,'reason':'database_credentials_missing'}


def test_frozen_manifest_passes_authoritative_schema_and_cannot_be_overwritten(tmp_path):
    import pytest
    sys.path.insert(0,str(Path(__file__).parents[1]/'scripts'))
    from release_readiness import write_manifest
    from validate_us_manifest import validate_manifest
    path=tmp_path/'cohort.csv'
    result=write_manifest(path,['B0CC2FRY3J','B0CC2JBW2H'])
    assert len(result['manifest_sha256'])==64
    assert validate_manifest(path,2)==[]
    with pytest.raises(FileExistsError): write_manifest(path,['B0CJFNJCNV'])


def test_migration_apply_requires_explicit_authority_before_credentials():
    script=Path(__file__).parents[1]/'scripts/apply_recovery_migrations.py'
    result=subprocess.run([sys.executable,str(script),'--mode','apply'],capture_output=True,text=True)
    assert result.returncode==2
    assert json.loads(result.stdout)['reason']=='explicit_production_approval_and_head_required'
