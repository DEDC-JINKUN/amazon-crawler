import pathlib

import psycopg

root = pathlib.Path(__file__).resolve().parent
pwd = next(l.split("=", 1)[1].strip() for l in (root / ".env").read_text(encoding="utf-8").splitlines() if l.startswith("POSTGRES_PASSWORD="))
dsn = f"host=127.0.0.1 port=5432 dbname=amazon_us user=postgres password={pwd}"
with psycopg.connect(dsn) as conn:
    cur = conn.cursor()
    cur.execute(
        "SELECT asin, status, attempts, last_error FROM amazon_us.item_state "
        "WHERE tenant_id='amazon_us_main' AND subject_type='own' AND status IN ('failed','blocked') "
        "ORDER BY updated_at DESC LIMIT 10"
    )
    for row in cur.fetchall():
        print(row[0], "|", row[1], "| attempts:", row[2], "|", str(row[3])[:120])
