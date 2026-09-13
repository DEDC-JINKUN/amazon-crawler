"""Versioned experiment policy; transport/time/byte/lease hard budgets unchanged."""
import math

VERSION = 'captcha-first-pass-v2'


class RecoveryPolicy:
    def __init__(self, config):
        if config.get('recovery_policy_version', VERSION) != VERSION:
            raise ValueError('unsupported_recovery_policy')
        def bounded(name, default, lower, upper):
            value = float(config.get(name, default))
            if not math.isfinite(value) or not lower <= value <= upper:
                raise ValueError('invalid_recovery_policy_parameter')
            return value
        self.initial = bounded('recovery_captcha_initial_seconds', 300, 60, 3600)
        self.multiplier = bounded('recovery_captcha_backoff_multiplier', 3, 1, 6)
        self.minimum_samples = int(bounded('recovery_global_min_samples', 10, 5, 20))
        self.block_ratio = bounded('recovery_global_block_ratio', .6, .5, 1)
        self.consecutive_limit = int(bounded('recovery_global_consecutive_blocks', 3, 3, 10))
        self.global_seconds = bounded('recovery_global_pause_seconds', 300, 60, 3600)

    def snapshot(self):
        return {'version':VERSION,'captcha_initial_seconds':self.initial,'captcha_backoff_multiplier':self.multiplier,
                'captcha_cap_seconds':3600,'global_min_samples':self.minimum_samples,'global_block_ratio':self.block_ratio,
                'global_consecutive_blocks':self.consecutive_limit,'global_pause_seconds':self.global_seconds,
                'window_unique_asins':20,'first_pass_priority':True}

    def cooldown(self, outcome, attempts, retry_after=None):
        requested = float(retry_after or 0)
        if not math.isfinite(requested) or requested < 0:
            raise ValueError('invalid_server_retry_after')
        base = min(3600, self.initial * self.multiplier ** min(8, max(0, attempts-1))) if outcome == 'access_control' else 0 if outcome == 'review_page' else 60
        return max(base, requested)

    def pause_reason(self, window, half_open, outcome, retry_after=None):
        if retry_after and retry_after > 0:
            return 'server_retry_after'
        if half_open and outcome not in {'completed','partial','variant'}:
            return 'half_open_failed'
        consecutive = 0
        for value in reversed(window):
            if not value['blocked']: break
            consecutive += 1
        if consecutive >= self.consecutive_limit:
            return 'consecutive_access_control'
        if len(window) >= self.minimum_samples and sum(bool(v['blocked']) for v in window)/len(window) >= self.block_ratio:
            return 'high_access_control_rate'
        return None
