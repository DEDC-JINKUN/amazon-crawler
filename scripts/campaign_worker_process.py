#!/usr/bin/env python3
"""One isolated campaign queue consumer process; no HTTP server or API key."""
from __future__ import annotations
import argparse, os, signal, threading, time
from pathlib import Path

from agent_collection_service import AgentRefreshWorker
from amazon_us_worker import _build_http_adapter, load_config
from postgres_worker_storage import PostgresWorkerStorage

def main() -> int:
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,required=True); parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--tenant-id',required=True); parser.add_argument('--subject-type',default='own'); parser.add_argument('--dsn-env',default='AMAZON_US_POSTGRES_DSN'); parser.add_argument('--poll-seconds',type=float,default=1.0); parser.add_argument('--lease-seconds',type=int,default=600)
    args=parser.parse_args(); dsn=os.environ.get(args.dsn_env,'').strip()
    if not dsn: raise RuntimeError('worker database credential unavailable')
    config=load_config(args.config.resolve()); config['output_dir']=args.output_dir.resolve(); config['raw_html_dir']=args.output_dir.resolve()/'raw_html'
    storage=PostgresWorkerStorage(dsn,args.tenant_id,args.subject_type,default_lease_seconds=args.lease_seconds)
    worker=AgentRefreshWorker(storage=storage,adapter_factory=lambda:_build_http_adapter(config),config=config,poll_seconds=args.poll_seconds,lease_seconds=args.lease_seconds)
    stopped=threading.Event()
    def stop(*_args): stopped.set()
    signal.signal(signal.SIGINT,stop)
    if hasattr(signal,'SIGTERM'): signal.signal(signal.SIGTERM,stop)
    worker.start()
    exit_code=0
    try:
        while not stopped.wait(1.0):
            if worker.status().get('state')=='failed':
                exit_code=2
                break
    finally: worker.stop()
    return exit_code

if __name__=='__main__': raise SystemExit(main())
