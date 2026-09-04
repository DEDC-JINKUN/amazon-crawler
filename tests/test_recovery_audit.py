import hashlib
import importlib.util
from pathlib import Path


def test_audit_is_hash_bound_unknown_on_missing_raw_and_does_not_expose_text(tmp_path):
    spec = importlib.util.spec_from_file_location("recovery_audit", Path(__file__).parents[1] / "scripts/recovery_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    body = '<html>Enter the characters you see below</html>'
    raw = tmp_path / 'challenge.html'
    raw.write_text(body, encoding='utf-8')
    row = dict(asin='B0CC2FRY3J', status='blocked', evidence_id=7,
               content_hash=hashlib.sha256(body.encode()).hexdigest(), raw_html_path=str(raw),
               block_reason='captcha', http_status=200, last_error='SECRET_SENTINEL')
    result = module.build_dry_run('test-tenant', [row], tmp_path)
    assert result['candidates'][0]['category'] == 'access_control'
    assert result['candidates'][0]['evidence_id'] == 7
    assert 'SECRET_SENTINEL' not in str(result)
    assert result == module.build_dry_run('test-tenant', [row], tmp_path)
    raw.unlink()
    assert module.build_dry_run('test-tenant', [row], tmp_path)['candidates'][0]['category'] == 'unknown'


def test_business_projection_uses_whole_cohort_not_last_capacity_batch():
    import sys
    sys.path.insert(0,str(Path(__file__).parents[1] / 'scripts'))
    from collection_console import project_amazon_business
    result = project_amazon_business([{'outcome':'blocked'},{'outcome':'completed'}],requested_actions=20,recorded_actions=2,unrequested_actions=3)
    assert result['unrequested_actions'] == 18
