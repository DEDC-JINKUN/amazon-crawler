import pathlib

import psycopg

root = pathlib.Path(__file__).resolve().parent
pwd = next(l.split("=", 1)[1].strip() for l in (root / ".env").read_text(encoding="utf-8").splitlines() if l.startswith("POSTGRES_PASSWORD="))
dsn = f"host=127.0.0.1 port=5432 dbname=amazon_us user=postgres password={pwd}"
with psycopg.connect(dsn) as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT asin, title, price, availability, bsr_category FROM amazon_us.product_snapshot "
        "WHERE tenant_id='amazon_us_local' AND price IS NOT NULL LIMIT 5"
    )
    for r in cur.fetchall():
        print(r[0], "|", str(r[1])[:45], "|", r[2], "|", str(r[3])[:35])
