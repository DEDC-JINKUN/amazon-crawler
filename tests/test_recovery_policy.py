import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))


def test_captcha_cooldown_is_incremental_but_retry_after_is_never_capped():
    from recovery_policy import RecoveryPolicy
    policy = RecoveryPolicy({})
    assert policy.cooldown('access_control', 1) == 300
    assert policy.cooldown('access_control', 2) == 900
    assert policy.cooldown('access_control', 9) == 3600
    assert policy.cooldown('access_control', 1, 7200) == 7200
    assert policy.cooldown('transport', 1, 7200) == 7200


def test_isolated_blocks_do_not_pause_but_sustained_shared_failure_does():
    from recovery_policy import RecoveryPolicy
    policy = RecoveryPolicy({})
    scattered = [{'blocked': index in {1, 7, 14}} for index in range(19)]
    assert policy.pause_reason(scattered, False, 'access_control') is None
    assert policy.pause_reason([{'blocked': True}]*3, False, 'access_control') == 'consecutive_access_control'
    broad = [{'blocked': index % 3 != 0} for index in range(10)]
    assert policy.pause_reason(broad, False, 'access_control') == 'high_access_control_rate'
    assert policy.pause_reason([], False, 'access_control', 7200) == 'server_retry_after'


def test_invalid_policy_parameters_fail_closed():
    from recovery_policy import RecoveryPolicy
    for config in ({'recovery_policy_version':'unrecognized'}, {'recovery_captcha_initial_seconds':0},
                   {'recovery_global_min_samples':1}, {'recovery_global_block_ratio':float('nan')}):
        with pytest.raises(ValueError): RecoveryPolicy(config)
