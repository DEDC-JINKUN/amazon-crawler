import pathlib
from collections import Counter

import psycopg

root = pathlib.Path(__file__).resolve().parent
pwd = next(l.split("=", 1)[1].strip() for l in (root / ".env").read_text(encoding="utf-8").splitlines() if l.startswith("POSTGRES_PASSWORD="))
dsn = f"host=127.0.0.1 port=5432 dbname=amazon_us user=postgres password={pwd}"
with psycopg.connect(dsn) as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT module_type, COUNT(*) FROM amazon_us.content_module "
        "WHERE tenant_id='amazon_us_local' GROUP BY module_type ORDER BY 2 DESC"
    )
    for row in cur.fetchall():
        print(f"{row[0]}: {row[1]}")
    cur.execute(
        "SELECT text, link_url FROM amazon_us.content_module "
        "WHERE tenant_id='amazon_us_local' AND link_url LIKE '%/dp/%' LIMIT 5"
    )
    print("--- link_url 带 /dp/ 的样例 ---")
    for text, link in cur.fetchall():
        print(f"text={str(text)[:30]!r} link={str(link)[:80]}")
