import json
import pathlib
import re

import psycopg

root = pathlib.Path(__file__).resolve().parent
pwd = next(l.split("=", 1)[1].strip() for l in (root / ".env").read_text(encoding="utf-8").splitlines() if l.startswith("POSTGRES_PASSWORD="))
dsn = f"host=127.0.0.1 port=5432 dbname=amazon_us user=postgres password={pwd}"

ASIN_RE = re.compile(r"\b[A-Z0-9]{10}\b")
candidates = set()

with psycopg.connect(dsn) as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT asin, specs, buy_box, bullets, bsr_entries FROM amazon_us.product_snapshot "
        "WHERE tenant_id='amazon_us_local'"
    )
    for asin, specs, buy_box, bullets, bsr in cur.fetchall():
        blob = json.dumps([specs, buy_box, bullets, bsr], ensure_ascii=False)
        for m in ASIN_RE.findall(blob):
            if m != asin:
                candidates.add(m)
    cur.execute(
        "SELECT asin, text, image_url, link_url FROM amazon_us.content_module WHERE tenant_id='amazon_us_local'"
    )
    for asin, text, image_url, link_url in cur.fetchall():
        blob = json.dumps([text, image_url, link_url], ensure_ascii=False)
        for m in ASIN_RE.findall(blob):
            if m != asin:
                candidates.add(m)
    cur.execute("SELECT COUNT(*) FROM amazon_us.product_snapshot WHERE tenant_id='amazon_us_local'")
    print("snapshots:", cur.fetchone()[0])
    cur.execute(
        "SELECT COUNT(*) FROM amazon_us.media_asset WHERE tenant_id='amazon_us_local' AND variant_asin IS NOT NULL"
    )
    print("media with variant_asin:", cur.fetchone()[0])
    cur.execute("SELECT DISTINCT variant_asin FROM amazon_us.media_asset WHERE tenant_id='amazon_us_local' AND variant_asin IS NOT NULL")
    for (v,) in cur.fetchall():
        if v:
            candidates.add(v)

cur2 = None
known = set()
with psycopg.connect(dsn) as conn:
    cur = conn.cursor()
    cur.execute("SELECT asin FROM amazon_us.asin_master WHERE tenant_id='amazon_us_local'")
    known = {r[0] for r in cur.fetchall()}

new_candidates = sorted(candidates - known)
print(f"asin_master known: {len(known)}")
print(f"candidate ASINs extracted from snapshots: {len(candidates)}")
print(f"new (not yet in master): {len(new_candidates)}")
print("sample:", new_candidates[:15])
