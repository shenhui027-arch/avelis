"""演示走查（uv run scripts/demo_view.py v1|v2）：用 root 修改 shop.daily_sales 视图（v1 = 实付，v2 = 净额）。口令从 .env 读取，不打印。"""
import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import demo  # noqa: E402

SQL = {
    "v1": "SELECT order_date, region, SUM(paid_amount) AS sales_amount FROM shop.orders GROUP BY order_date, region",
    "v2": "SELECT order_date, region, SUM(paid_amount - refund_amount) AS sales_amount FROM shop.orders GROUP BY order_date, region",
}
conn = demo.sr_connect(demo.load_env())
with conn.cursor() as cur:
    cur.execute(f"ALTER VIEW shop.daily_sales AS {SQL[sys.argv[1]]}")
    cur.execute("SELECT sales_amount FROM shop.daily_sales WHERE order_date='2026-09-15' AND region='华东'")
    print(sys.argv[1], "华东 2026-09-15 =", cur.fetchone()[0])
conn.close()
