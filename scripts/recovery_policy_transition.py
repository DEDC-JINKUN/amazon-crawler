"""Explicit scoped policy transition; default is read-only and preserves old timers."""
import argparse
from datetime import timedelta
import hashlib
import json
import os
import re

from recovery_policy import RecoveryPolicy


def transition(conn, tenant, asins, *, reschedule=False, legacy_no_retry_after=False,
               apply=False, expected_hash=None, confirm_stopped=False, config=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',tenant) or not asins or len(asins)>100 or len(set(asins))!=len(asins) or any(not re.fullmatch(r'[A-Z0-9]{10}',a) for a in asins):
        raise ValueError('invalid_transition_scope')
    if apply and (not confirm_stopped or not expected_hash):
        raise ValueError('explicit_stopped_and_plan_required')
    if not apply:
        conn.execute('SET TRANSACTION READ ONLY')
    conn.execute("SET LOCAL statement_timeout='5s'")
    conn.execute("SET LOCAL lock_timeout='1s'")
    policy=RecoveryPolicy(config or {})
    gate=conn.execute('SELECT *,CURRENT_TIMESTAMP AS now FROM amazon_us.recovery_egress WHERE tenant_id=%s AND egress_id=%s'+(' FOR UPDATE' if apply else ''),(tenant,'paid-residential')).fetchone()
    if not gate: raise ValueError('egress_not_found')
    active=conn.execute("SELECT EXISTS(SELECT 1 FROM amazon_us.item_state WHERE tenant_id=%s AND lease_expires_at>CURRENT_TIMESTAMP) OR EXISTS(SELECT 1 FROM amazon_us.collection_run WHERE tenant_id=%s AND status IN ('starting','running')) AS active",(tenant,tenant)).fetchone()['active']
    if reschedule and (active or gate['manually_paused'] or (gate['lease_expires_at'] and gate['lease_expires_at']>gate['now'])):
        raise ValueError('active_or_manually_paused_scope')
    rows=conn.execute('''SELECT j.*,s.next_retry_at AS state_retry,s.status AS state_status
        FROM amazon_us.recovery_job j JOIN amazon_us.item_state s
          ON s.tenant_id=j.tenant_id AND s.asin=j.asin AND s.subject_type=j.subject_type AND s.marketplace='US'
        WHERE j.tenant_id=%s AND j.asin=ANY(%s) AND j.subject_type='own' AND j.stage='product'
        ORDER BY j.asin'''+(' FOR UPDATE OF j,s' if apply else ''),(tenant,asins)).fetchall()
    if len(rows)!=len(asins): raise ValueError('job_scope_mismatch')
    jobs=[]; changes=[]
    for row in rows:
        jobs.append({key:row[key] for key in ('asin','subject_type','stage','attempts','max_attempts','request_count','max_requests','browser_request_count','relay_payload_bytes','known_bytes','unknown_byte_attempts','deadline','next_retry_at','updated_at','outcome','terminal','state_retry','state_status')})
        if not reschedule: continue
        if row['terminal'] or row['outcome']!='access_control' or row['lease_token'] or row['deadline']<=gate['now']:
            raise ValueError('job_not_cooling_access_control')
        if not legacy_no_retry_after:
            raise ValueError('retry_after_attestation_required')
        prior_audit=conn.execute('''SELECT reason FROM amazon_us.state_history WHERE tenant_id=%s
            AND asin=%s AND subject_type='own' AND reason LIKE %s ORDER BY id DESC LIMIT 1''',
            (tenant,row['asin'],'recovery_policy:%')).fetchone()
        if prior_audit and float(json.loads(prior_audit['reason'].split(':',1)[1]).get('server_retry_after_seconds') or 0)>0:
            raise ValueError('known_retry_after_preserved')
        evidence=conn.execute('''SELECT http_status,block_reason FROM amazon_us.collection_evidence
            WHERE tenant_id=%s AND asin=%s AND subject_type='own' ORDER BY id DESC LIMIT 1''',(tenant,row['asin'])).fetchone()
        if not evidence or evidence['http_status']!=200 or evidence['block_reason'] not in {'captcha','robot_check'}:
            raise ValueError('legacy_captcha_evidence_required')
        if row['state_retry']!=row['next_retry_at']:
            raise ValueError('independent_retry_timer_conflict')
        # Anchor on the original attempt, not migration time; preserve deadline,
        # attempts, bytes, outcomes and all evidence. Operator attests that old
        # 200 CAPTCHA had no Retry-After; unknown is never silently assumed zero.
        proposed=row['updated_at']+timedelta(seconds=policy.cooldown('access_control',row['attempts']))
        if row['next_retry_at'] and proposed<row['next_retry_at']:
            changes.append({'kind':'job_cooldown','asin':row['asin'],'old':row['next_retry_at'],'new':proposed})
    outcomes=gate['outcomes']
    blocked_scope={str(v.get('key','')).split('|')[0] for v in outcomes if isinstance(v,dict) and v.get('blocked')}
    legacy_window=bool(outcomes) and all(isinstance(v,dict) and 'policy' not in v for v in outcomes)
    if (reschedule and legacy_no_retry_after and gate['paused_until'] and legacy_window
            and blocked_scope and blocked_scope.issubset(set(asins))
            and policy.pause_reason(outcomes,False,'access_control') is None):
        changes.append({'kind':'legacy_global_pause','old':gate['paused_until'],'new':None})
    result={'tenant_id':tenant,'asins':sorted(asins),'policy':policy.snapshot(),'jobs':jobs,
            'old_egress':{k:gate[k] for k in ('paused_until','manually_paused','half_open','consecutive_blocks','outcomes','next_request_at','lease_expires_at')},
            'changes':changes,'legacy_no_retry_after_attested':bool(legacy_no_retry_after)}
    encoded=json.dumps(result,sort_keys=True,default=lambda x:x.isoformat(),separators=(',',':'))
    digest=hashlib.sha256(encoded.encode()).hexdigest()
    if apply and digest!=expected_hash: raise ValueError('plan_drift')
    if apply:
        for change in changes:
            if change['kind']=='job_cooldown':
                conn.execute("UPDATE amazon_us.recovery_job SET next_retry_at=%s WHERE tenant_id=%s AND asin=%s AND subject_type='own' AND stage='product'",(change['new'],tenant,change['asin']))
                conn.execute("UPDATE amazon_us.item_state SET next_retry_at=%s WHERE tenant_id=%s AND asin=%s AND subject_type='own' AND marketplace='US'",(change['new'],tenant,change['asin']))
            else:
                conn.execute("UPDATE amazon_us.recovery_egress SET paused_until=NULL WHERE tenant_id=%s AND egress_id='paid-residential'",(tenant,))
        for asin in asins:
            conn.execute("INSERT INTO amazon_us.state_history(tenant_id,marketplace,asin,subject_type,from_status,to_status,reason) VALUES(%s,'US',%s,'own','blocked','blocked',%s)",
                         (tenant,asin,'recovery_policy_transition:'+json.dumps({'plan_hash':digest,'plan':json.loads(encoded)},sort_keys=True)))
        conn.commit()
    else: conn.rollback()
    return {**json.loads(encoded),'plan_hash':digest,'committed':bool(apply),'evidence_writes':0,'budget_resets':0}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant-id',required=True)
    parser.add_argument('--asins',nargs='+',required=True)
    parser.add_argument('--reschedule-captcha',action='store_true')
    parser.add_argument('--legacy-no-retry-after-confirmed',action='store_true')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--confirm-stopped',action='store_true')
    parser.add_argument('--expected-plan-hash')
    parser.add_argument('--config')
    args=parser.parse_args(argv)
    try:
        import psycopg
        from psycopg.rows import dict_row
        config={}
        if args.config:
            import tomllib
            with open(args.config,'rb') as handle: config=tomllib.load(handle).get('worker',{})
        with psycopg.connect(os.environ['AMAZON_US_POSTGRES_DSN'],row_factory=dict_row,connect_timeout=3) as conn:
            result=transition(conn,args.tenant_id,args.asins,reschedule=args.reschedule_captcha,
                              legacy_no_retry_after=args.legacy_no_retry_after_confirmed,apply=args.apply,
                              expected_hash=args.expected_plan_hash,confirm_stopped=args.confirm_stopped,config=config)
        print(json.dumps(result,ensure_ascii=True)); return 0
    except Exception as exc:
        allowed={'invalid_transition_scope','explicit_stopped_and_plan_required','egress_not_found','active_or_manually_paused_scope',
                 'job_scope_mismatch','job_not_cooling_access_control','retry_after_attestation_required','legacy_captcha_evidence_required',
                 'independent_retry_timer_conflict','known_retry_after_preserved','plan_drift','unsupported_recovery_policy','invalid_recovery_policy_parameter'}
        reason=str(exc) if isinstance(exc,ValueError) and str(exc) in allowed else 'policy_transition_failed'
        print(json.dumps({'ok':False,'reason':reason,'committed':False})); return 2


if __name__=='__main__': raise SystemExit(main())
