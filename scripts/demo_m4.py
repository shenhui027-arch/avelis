"""异常场景走查（M4）：在数据库侧制造变化。只用于本地演示库；口令从 .env 读取，不打印。

  uv run scripts/demo_m4.py drop-paid-amount      StarRocks：删除 shop.orders.paid_amount（故事 7）
  uv run scripts/demo_m4.py mysql-drop-provinces  MySQL：删除 shop_dim.dim_region.provinces（外部表列不一致）
  uv run scripts/demo_m4.py mysql-widen-category  MySQL：shop_dim.dim_category.category_name 改为 VARCHAR(64)

这些操作不可在 Avelis 中撤销；用 uv run scripts/demo.py seed 把演示库恢复到基线。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo  # noqa: E402

ACTIONS = {
    "drop-paid-amount": ("sr", "ALTER TABLE shop.orders DROP COLUMN paid_amount"),
    "mysql-drop-provinces": ("mysql", "ALTER TABLE shop_dim.dim_region DROP COLUMN provinces"),
    "mysql-widen-category": ("mysql", "ALTER TABLE shop_dim.dim_category MODIFY category_name VARCHAR(64) NOT NULL "
                                      "COMMENT '类目名称'"),
}

if len(sys.argv) != 2 or sys.argv[1] not in ACTIONS:
    sys.exit(__doc__)
target, sql = ACTIONS[sys.argv[1]]
env = demo.load_env()
conn = demo.sr_root(env) if target == "sr" else demo.mysql_connect(env)
with conn.cursor() as cur:
    cur.execute(sql)
    if sys.argv[1] == "drop-paid-amount":  # StarRocks 的结构变更是异步作业，等它生效
        for _ in range(60):
            cur.execute("DESC shop.orders")
            if "paid_amount" not in {r[0] for r in cur.fetchall()}:
                break
            time.sleep(1)
conn.close()
print(f"已执行（{target}）：{sql}")
