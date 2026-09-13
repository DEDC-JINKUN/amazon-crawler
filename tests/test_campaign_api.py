import importlib.util
from pathlib import Path
import threading
import urllib.error

ROOT=Path(__file__).parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py')
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

class CampaignRepository:
    tenant_id='campaign-test'
    def __init__(self): self.by_key={}; self.audits=[]; self.conflict_error=ValueError
    def load_schema_contract(self): return {'item_state':['lease_expires_at','lease_owner','lease_token','next_retry_at'],'collection_evidence':['context_json','transfer_bytes']}
    def load_job_status(self): return {'counts':{}}
    def record_api_audit(self,**event): self.audits.append(event)
    def submit_campaign(self,*,asins,idempotency_key,max_members,**options):
        request=tuple(dict.fromkeys(asins))
        if len(request)>max_members: raise ValueError('configured quota')
        if idempotency_key in self.by_key:
            if self.by_key[idempotency_key]['request']!=request: raise self.conflict_error('campaign idempotency key conflicts with another request')
            return self.by_key[idempotency_key]['campaign']
        campaign={'campaign_id':f"campaign-{'1'*32}",'total':len(request),'terminal':0,'counts':{'pending':len(request)},'members':[{'asin':asin,'status':'pending'} for asin in request]}
        self.by_key[idempotency_key]={'request':request,'campaign':campaign}; return campaign
    def load_campaign(self,campaign_id):
        return next((value['campaign'] for value in self.by_key.values() if value['campaign']['campaign_id']==campaign_id),None)
    def cancel_campaign(self,campaign_id):
        campaign=self.load_campaign(campaign_id)
        if campaign is not None: campaign['status']='cancelled'
        return campaign

def test_campaign_submits_thirty_once_and_returns_progress_without_atomic_refresh_limit():
    api=load('collection_api'); client_module=load('amazon_collection_client'); repository=CampaignRepository(); repository.conflict_error=api.CampaignConflictError; notifications=[]
    server=api.CollectionServer(('127.0.0.1',0),repository=repository,api_key='master',campaign_max_asins=1093,refresh_notifier=lambda:notifications.append(1),refresh_worker_status=lambda:{'state':'running'})
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        key=api.derive_agent_key('master','refresh-agent')
        client=client_module.AmazonCollectionClient(f'http://127.0.0.1:{server.server_port}','refresh-agent',key)
        asins=[f'B0CAM{i:05d}' for i in range(30)]
        first=client.submit_campaign(asins,'campaign-idempotency-0001',priority=70,deadline_seconds=3600)
        second=client.submit_campaign(asins,'campaign-idempotency-0001',priority=70,deadline_seconds=3600)
        assert first['campaign']['campaign_id']==second['campaign']['campaign_id']
        assert first['campaign']['total']==30 and len(notifications)==2
        progress=client.get_campaign(first['campaign']['campaign_id'])
        assert progress['campaign']['counts']['pending']==30
        large=[f'B{index:09d}' for index in range(1093)]
        accepted=client.submit_campaign(large,'campaign-idempotency-1093',deadline_seconds=3600)
        assert accepted['campaign']['total']==1093
        try: client.submit_campaign(asins[:-1],'campaign-idempotency-0001')
        except urllib.error.HTTPError as exc: assert exc.code==409
        else: raise AssertionError('conflicting idempotency key accepted')
        cancelled=client.cancel_campaign(first['campaign']['campaign_id'])
        assert cancelled['campaign']['status']=='cancelled'
        assert client.cancel_campaign(first['campaign']['campaign_id'])['campaign']['status']=='cancelled'
    finally: server.shutdown(); server.server_close(); thread.join(2)

def test_read_agent_cannot_submit_campaign_and_capability_reports_configured_quota():
    api=load('collection_api'); client_module=load('amazon_collection_client'); repository=CampaignRepository()
    server=api.CollectionServer(('127.0.0.1',0),repository=repository,api_key='master',campaign_max_asins=1093,refresh_worker_status=lambda:{'state':'running'})
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        client=client_module.AmazonCollectionClient(f'http://127.0.0.1:{server.server_port}','read-agent',api.derive_agent_key('master','read-agent'))
        assert client.get_capabilities()['limits']['campaign_asins']==1093
        try: client.submit_campaign(['B0CAM00000'],'campaign-idempotency-0002')
        except urllib.error.HTTPError as exc: assert exc.code==403
        else: raise AssertionError('read agent submitted campaign')
    finally: server.shutdown(); server.server_close(); thread.join(2)
