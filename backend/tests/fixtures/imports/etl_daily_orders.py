"""每日订单作业（演示：内嵌 SQL + DataFrame + 动态 SQL）。"""
from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.getOrCreate()


def load_refunds():
    spark.sql("""
        INSERT INTO ads.refund_daily
        SELECT refund_date, SUM(amount) AS refund_total
        FROM shop.refund_raw
        GROUP BY refund_date
    """)


def region_snapshot(dt):
    spark.sql(f"""
        INSERT INTO ads.region_snapshot
        SELECT region, SUM(paid_amount) AS paid_total
        FROM shop.orders
        WHERE order_date = '{dt}'
        GROUP BY region
    """)


def item_rollup():
    jdbc_url = "jdbc:mysql://etl_user:Pa55word@shop-mysql:3306/shop_dim"
    items = spark.table("shop.order_items")
    orders = spark.table("shop.orders")
    joined = items.join(orders, "order_id")
    daily = joined.groupBy("order_date").agg(F.sum("item_amount").alias("item_total"))
    daily.write.mode("overwrite").option("url", jdbc_url).saveAsTable("ads.item_daily")
