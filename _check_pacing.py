import pathlib

import psycopg

root = pathlib.Path(__file__).resolve().parent
pwd = next(l.split("=", 1)[1].strip() for l in (root / ".env").read_text(encoding="utf-8").splitlines() if l.startswith("POSTGRES_PASSWORD="))
dsn = f"host=127.0.0.1 port=5432 dbname=amazon_us user=postgres password={pwd}"
with psycopg.connect(dsn) as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT asin, retrieved_at FROM amazon_us.collection_evidence "
        "WHERE tenant_id='amazon_us_main' AND batch_id='45bcb3f4-c7ab-4722-a04e-559bc36d0db0' "
        "ORDER BY retrieved_at"
    )
    rows = cur.fetchall()
    print(f"evidence rows: {len(rows)}")
    gaps = []
    for (a1, t1), (a2, t2) in zip(rows, rows[1:]):
        gap = (t2 - t1).total_seconds()
        gaps.append(gap)
        print(f"  {a1} -> {a2}: {gap:.1f}s")
    if gaps:
        print(f"平均间隔: {sum(gaps)/len(gaps):.1f}s | 最小 {min(gaps):.1f}s | 最大 {max(gaps):.1f}s")
