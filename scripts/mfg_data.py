"""合成制造 · 生产运营数据集（ADR 0010、ADR 0011 第 9 点）。

在仓库根目录运行：
  uv run scripts/mfg_data.py load [--force]   生成并灌入独立的制造演示库
  uv run scripts/mfg_data.py truth            只由生成数据计算标准答案，不连接数据库
  uv run scripts/mfg_data.py check            用只读账号逐行核对源表和全部日汇总视图

固定 SEED 与 VERSION，同版本逐行确定；时间为 2026-06-01 至 2026-09-30，
业务时区 Asia/Shanghai。load 只重建 DATABASES 中的独立制造库，完成后重新授予
演示采集与查询账号 SELECT 权限。truth 仅用于评测，不能作为模型上下文或知识发布。

预埋场景（标准答案，不提供给模型）：
  1. 恒达材料于 2026-08-20 入库的精密合金批次 HD-20260820-A，在 9 月用于
     华南 1、2 号线，裂纹不良上升。入库表通过批次号关联报工，但不在良率血缘中。
  2. 华南 3 号线 2026-09-08 至 09-14 计划停线检修，维护工单说明原因。
  3. 西南 4 号线 2026-08-01 投产，8、9 月逐步爬坡；前 14 天良率偏低。
  4. 华南、西南工厂 2026-09-25 至 09-27 中秋停产，华东工厂照常生产。
  5. 华东 1、2 号线 9 月产量一升一降；工厂月合计基本不变。
  6. 西南 2 号线 2026-09-18 三个班次电表读数缺失，报工正常；缺读保留 NULL。
  7. 华东 3 号线 8、9 月单位能耗仅有小幅随机波动，没有实质变化。
  8. 设备型号与单位能耗有关：精益 A 型最低、标准 B 型居中、重载 C 型最高。
     这是合成数据中的相关关系，单凭分组结果不能声称真实世界因果。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

VERSION = "mfg-1"
SEED = 20261031
START, END = date(2026, 6, 1), date(2026, 9, 30)
TIMEZONE = "Asia/Shanghai"
DATABASES = ("mfg_mes", "mfg_qms", "mfg_eam", "mfg_scm", "mfg_dw")
TRUTH_FILE = Path(__file__).resolve().parents[1] / ".demo" / "mfg-truth.json"
CENT = Decimal("0.01")
FACTORIES = [(1, "华东工厂", "华东"), (2, "华南工厂", "华南"), (3, "西南工厂", "西南")]
EQUIPMENT_MODELS = [(1, "精益A型", "0.45"), (2, "标准B型", "0.65"), (3, "重载C型", "0.95")]
SHIFTS = [(1, "早", 0, 8), (2, "中", 8, 8), (3, "夜", 16, 8)]
PRODUCT_FAMILIES = [(1, "精密组件"), (2, "动力模组"), (3, "控制组件"), (4, "结构组件")]
SUPPLIERS = [(1, "恒达材料"), (2, "华鑫合金"), (3, "川盛工业"), (4, "南方精材")]
MATERIALS = [(1, "精密合金", "千克"), (2, "工程塑料", "千克"), (3, "电子基材", "千克"),
             (4, "结构钢材", "千克")]
# 产线编码、名称、工厂、设备型号、产品族、日基准产量、上线日期。
LINES = [
    ("EC1", "华东1号线", 1, 1, 1, 1500, date(2024, 1, 1)),
    ("EC2", "华东2号线", 1, 3, 2, 1500, date(2024, 1, 1)),
    ("EC3", "华东3号线", 1, 2, 3, 1200, date(2024, 1, 1)),
    ("SC1", "华南1号线", 2, 1, 1, 1500, date(2024, 1, 1)),
    ("SC2", "华南2号线", 2, 2, 1, 1500, date(2024, 1, 1)),
    ("SC3", "华南3号线", 2, 3, 4, 1800, date(2024, 1, 1)),
    ("SW1", "西南1号线", 3, 1, 2, 1350, date(2024, 1, 1)),
    ("SW2", "西南2号线", 3, 2, 3, 1350, date(2024, 1, 1)),
    ("SW3", "西南3号线", 3, 3, 4, 1500, date(2024, 1, 1)),
    ("SW4", "西南4号线", 3, 1, 1, 1800, date(2026, 8, 1)),
]
BAD_BATCH = "HD-20260820-A"
HOLIDAY_START, HOLIDAY_END = date(2026, 9, 25), date(2026, 9, 27)
MAINTENANCE_START, MAINTENANCE_END = date(2026, 9, 8), date(2026, 9, 14)
ENERGY_GAP = ("SW2", date(2026, 9, 18))


def amount(value) -> Decimal:
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def days() -> list[date]:
    return [START + timedelta(days=i) for i in range((END - START).days + 1)]


@dataclass
class Dataset:
    tables: dict[str, list[tuple]] = field(default_factory=dict)
    truth: dict = field(default_factory=dict)

    def add(self, table: str, row: tuple) -> None:
        self.tables.setdefault(table, []).append(row)


def generate() -> Dataset:
    rng = random.Random(SEED)
    ds = Dataset()
    for row in FACTORIES:
        ds.add("mfg_mes.factory", (*row, TIMEZONE))
    for row in EQUIPMENT_MODELS:
        ds.add("mfg_mes.equipment_model", (row[0], row[1], Decimal(row[2])))
    for row in SHIFTS:
        ds.add("mfg_mes.shift", row)
    for row in PRODUCT_FAMILIES:
        ds.add("mfg_mes.product_family", row)
    for row in SUPPLIERS:
        ds.add("mfg_scm.supplier", row)
    for row in MATERIALS:
        ds.add("mfg_scm.material", row)
    for line in LINES:
        ds.add("mfg_mes.production_line", (line[0], line[1], line[2], line[3], line[4], line[6],
                                           line[5], "运行"))

    # 每厂每月每种物料都有足量入库；报工采用真实入库批次，不制造悬空批次号。
    batches = {}
    receipt_id = 0
    for month in range(6, 10):
        for factory_id, _, _ in FACTORIES:
            for material_id, _, _ in MATERIALS:
                receipt_id += 1
                supplier_id = (factory_id + material_id - 2) % len(SUPPLIERS) + 1
                batch = f"M{factory_id}{material_id}-2026{month:02d}01"
                batches[(factory_id, material_id, month)] = batch
                ds.add("mfg_scm.material_receipt", (receipt_id, batch, date(2026, month, 1), factory_id,
                                                    supplier_id, material_id, Decimal("250000.00"), "合格"))
    receipt_id += 1
    ds.add("mfg_scm.material_receipt", (receipt_id, BAD_BATCH, date(2026, 8, 20), 2, 1, 1,
                                        Decimal("150000.00"), "入库抽检合格"))

    # 工单记录完整的起止时间；跨日停机按业务日拆分，防止整周时长计入单日。
    maintenance_id, downtime_id = 0, 0
    downtime_by_shift: dict[tuple, int] = defaultdict(int)

    def add_maintenance(line_id, start, end, kind, reason):
        nonlocal maintenance_id, downtime_id
        maintenance_id += 1
        factory_id = next(line[2] for line in LINES if line[0] == line_id)
        minutes = int((end - start).total_seconds() // 60)
        ds.add("mfg_eam.maintenance_order", (maintenance_id, line_id, factory_id, start.date(), start, end,
                                             kind, reason, minutes, "已完成"))
        cursor = start
        while cursor < end:
            finish = min(end, datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time()))
            downtime_id += 1
            duration = int((finish - cursor).total_seconds() // 60)
            ds.add("mfg_eam.downtime", (downtime_id, line_id, cursor.date(), cursor, finish, duration,
                                       kind, reason, maintenance_id))
            for shift_id, _, hour, length in SHIFTS:
                shift_start = datetime.combine(cursor.date(), datetime.min.time()) + timedelta(hours=hour)
                shift_end = shift_start + timedelta(hours=length)
                overlap = max(0, int((min(finish, shift_end) - max(cursor, shift_start)).total_seconds() // 60))
                downtime_by_shift[(cursor.date(), line_id, shift_id)] += overlap
            cursor = finish

    for line_id, _, _, _, _, _, launch in LINES:
        for month in range(6, 10):
            d = date(2026, month, 5)
            if d >= launch:
                at = datetime.combine(d, datetime.min.time()) + timedelta(hours=9)
                add_maintenance(line_id, at, at + timedelta(minutes=60), "计划性", "月度预防保养")
            fault_day = date(2026, month, 16 + int(line_id[-1]))
            if (line_id, fault_day) == ENERGY_GAP:
                fault_day += timedelta(days=2)
            if fault_day >= launch:
                at = datetime.combine(fault_day, datetime.min.time()) + timedelta(hours=14)
                add_maintenance(line_id, at, at + timedelta(minutes=30 + int(line_id[-1]) * 5),
                                "故障性", "传感器故障排除")
    add_maintenance("SC3", datetime(2026, 9, 8), datetime(2026, 9, 15), "计划性", "年度主轴检修与安全联锁升级")

    report_id, reading_id, inspection_id, schedule_id = 0, 0, 0, 0
    energy_intensity = {row[0]: Decimal(row[2]) for row in EQUIPMENT_MODELS}
    for d in days():
        for line_id, _, factory_id, model_id, family_id, capacity, launch in LINES:
            launched = d >= launch
            holiday = factory_id in (2, 3) and HOLIDAY_START <= d <= HOLIDAY_END
            planned = line_id == "SC3" and MAINTENANCE_START <= d <= MAINTENANCE_END
            status = "未投产" if not launched else ("节假日停产" if holiday else ("计划检修" if planned else "生产"))
            for shift_id, _, start_hour, hours in SHIFTS:
                schedule_id += 1
                work_hours = amount(hours - downtime_by_shift[(d, line_id, shift_id)] / 60) if status == "生产" else Decimal("0.00")
                ds.add("mfg_mes.schedule", (schedule_id, d, line_id, shift_id, status, work_hours,
                                            "中秋放假" if holiday else ("投产准备" if not launched else "正常排班")))
                qty, good, defect = 0, 0, 0
                if status == "生产":
                    factor = 1.0
                    # 华东按月总量维持排产，1/2 线调配负荷，合计基本不变。
                    if factory_id == 1 and d.month == 9:
                        factor *= 31 / 30
                        factor *= {"EC1": 1.30, "EC2": 0.70}.get(line_id, 1)
                    if line_id == "SW4":
                        factor *= (0.45 if d.day <= 7 else 0.65 if d.day <= 14 else 0.85) if d.month == 8 else 1.25
                    qty = max(1, round(capacity / 3 * factor * float(work_hours) / hours * rng.uniform(0.985, 1.015)))
                    bad_material = line_id in ("SC1", "SC2") and d.month == 9
                    ramping = line_id == "SW4" and date(2026, 8, 1) <= d <= date(2026, 8, 14)
                    defect_rate = (0.145 if bad_material else 0.105 if ramping else 0.024) + rng.uniform(-0.002, 0.002)
                    defect = round(qty * defect_rate)
                    good = qty - defect
                    material_id = 1 if line_id in ("SC1", "SC2") else family_id
                    batch = BAD_BATCH if bad_material else batches[(factory_id, material_id, d.month)]
                    report_id += 1
                    ds.add("mfg_mes.production_report", (report_id, d, line_id, shift_id, family_id, batch,
                                                          qty, good, defect, work_hours))
                    inspection_id += 1
                    defect_type = "裂纹" if bad_material else rng.choice(["尺寸偏差", "表面划伤", "装配偏差"])
                    ds.add("mfg_qms.inspection", (inspection_id, report_id, d, line_id, shift_id,
                                                   defect_type, qty, defect, "终检"))
                if launched and (line_id, d) != ENERGY_GAP:
                    reading_id += 1
                    # 各型号间的差异显著大于随机噪声；缺读不补为零，停产零读数仍是真实记录。
                    energy = amount(qty * energy_intensity[model_id] * Decimal(str(rng.uniform(0.997, 1.003))))
                    at = datetime.combine(d, datetime.min.time()) + timedelta(hours=start_hour + hours, minutes=-1)
                    ds.add("mfg_mes.energy_reading", (reading_id, d, line_id, shift_id, at, energy, "正常"))
    # 保留空表接口，避免后续规则变更造成 TABLES 与 Dataset.tables 不一致。
    for table in TABLE_COLUMNS:
        ds.tables.setdefault(table, [])
    ds.truth = truth(ds)
    return ds


# --- 建表与视图：每张表和每一列均有中文注释 --------------------------------------------

TABLE_COLUMNS: dict[str, list[str]] = {}
TABLE_METADATA: dict[str, dict] = {}


def _t(name: str, comment: str, columns: list[tuple[str, str, str]], key: list[str]) -> str:
    TABLE_COLUMNS[name] = [c[0] for c in columns]
    TABLE_METADATA[name] = {"comment": comment, "columns": columns, "key": key}
    sized = [(c, re.sub(r"VARCHAR\((\d+)\)", lambda m: f"VARCHAR({int(m.group(1)) * 4})", t), text)
             for c, t, text in columns]
    cols = ",\n  ".join(f"{c} {t} COMMENT '{text}'" for c, t, text in sized)
    return (f"CREATE TABLE {name} (\n  {cols}\n) PRIMARY KEY ({', '.join(key)})\nCOMMENT '{comment}'\n"
            f"DISTRIBUTED BY HASH({key[0]}) BUCKETS 2\nPROPERTIES (\"replication_num\" = \"1\")")


DB_COMMENTS = {"mfg_mes": "制造生产执行", "mfg_qms": "制造质量管理", "mfg_eam": "制造设备维护",
               "mfg_scm": "制造采购与物料", "mfg_dw": "制造数仓（日汇总视图）"}
TABLES = [
    _t("mfg_mes.factory", "工厂维度", [
        ("factory_id", "INT NOT NULL", "工厂编号"), ("factory_name", "VARCHAR(32)", "工厂名称"),
        ("region_name", "VARCHAR(16)", "所在区域"), ("business_timezone", "VARCHAR(32)", "业务时区")], ["factory_id"]),
    _t("mfg_mes.equipment_model", "设备型号维度", [
        ("model_id", "INT NOT NULL", "设备型号编号"), ("model_name", "VARCHAR(32)", "设备型号名称"),
        ("rated_unit_energy", "DECIMAL(12,2)", "额定单位能耗（千瓦时每件）")], ["model_id"]),
    _t("mfg_mes.shift", "班次维度", [
        ("shift_id", "INT NOT NULL", "班次编号"), ("shift_name", "VARCHAR(8)", "班次名称：早、中、夜"),
        ("start_hour", "INT", "开始小时（业务日内）"), ("duration_hours", "INT", "班次时长（小时）")], ["shift_id"]),
    _t("mfg_mes.product_family", "产品族维度", [
        ("product_family_id", "INT NOT NULL", "产品族编号"), ("product_family", "VARCHAR(32)", "产品族名称")], ["product_family_id"]),
    _t("mfg_mes.production_line", "产线维度", [
        ("line_id", "VARCHAR(16) NOT NULL", "产线编码"), ("line_name", "VARCHAR(32)", "产线名称"),
        ("factory_id", "INT", "所属工厂编号"), ("model_id", "INT", "设备型号编号"),
        ("product_family_id", "INT", "主产产品族编号"), ("launch_date", "DATE", "正式投产日期"),
        ("daily_capacity", "INT", "日基准产能（件）"), ("status", "VARCHAR(16)", "期末运行状态")], ["line_id"]),
    _t("mfg_scm.supplier", "供应商维度", [
        ("supplier_id", "INT NOT NULL", "供应商编号"), ("supplier_name", "VARCHAR(32)", "供应商名称")], ["supplier_id"]),
    _t("mfg_scm.material", "物料维度", [
        ("material_id", "INT NOT NULL", "物料编号"), ("material_name", "VARCHAR(32)", "物料名称"),
        ("unit", "VARCHAR(8)", "入库数量单位")], ["material_id"]),
    _t("mfg_scm.material_receipt", "物料批次入库记录；批次号关联生产报工", [
        ("receipt_id", "BIGINT NOT NULL", "入库记录编号"), ("batch_no", "VARCHAR(48)", "物料批次号"),
        ("receipt_date", "DATE", "入库日期（业务时区）"), ("factory_id", "INT", "收货工厂编号"),
        ("supplier_id", "INT", "供货供应商编号"), ("material_id", "INT", "物料编号"),
        ("received_qty", "DECIMAL(18,2)", "入库数量（单位见物料维度）"),
        ("acceptance_result", "VARCHAR(32)", "入库验收结果")], ["receipt_id"]),
    _t("mfg_mes.production_report", "生产报工；良品数与不良数之和等于产量", [
        ("report_id", "BIGINT NOT NULL", "报工记录编号"), ("production_date", "DATE", "生产业务日期（Asia/Shanghai）"),
        ("line_id", "VARCHAR(16)", "产线编码"), ("shift_id", "INT", "班次编号"),
        ("product_family_id", "INT", "产品族编号"), ("batch_no", "VARCHAR(48)", "投入物料批次号"),
        ("production_qty", "INT", "产量（件）"), ("good_qty", "INT", "良品数（件）"),
        ("defect_qty", "INT", "不良数（件）"), ("work_hours", "DECIMAL(12,2)", "实际生产工时（小时）")], ["report_id"]),
    _t("mfg_mes.schedule", "生产排班；停产与未投产也保留计划记录", [
        ("schedule_id", "BIGINT NOT NULL", "排班编号"), ("stat_date", "DATE", "排班业务日期"),
        ("line_id", "VARCHAR(16)", "产线编码"), ("shift_id", "INT", "班次编号"),
        ("schedule_status", "VARCHAR(16)", "状态：生产、节假日停产、计划检修、未投产"),
        ("scheduled_hours", "DECIMAL(12,2)", "计划可生产工时（小时）"),
        ("schedule_reason", "VARCHAR(32)", "排班说明")], ["schedule_id"]),
    _t("mfg_mes.energy_reading", "班次能耗计量；缺失读数不写零记录", [
        ("reading_id", "BIGINT NOT NULL", "计量记录编号"), ("reading_date", "DATE", "计量业务日期"),
        ("line_id", "VARCHAR(16)", "产线编码"), ("shift_id", "INT", "班次编号"),
        ("reading_time", "DATETIME", "读取时间（Asia/Shanghai）"),
        ("energy_kwh", "DECIMAL(18,2)", "班次耗电量（千瓦时）"),
        ("reading_status", "VARCHAR(16)", "读数状态")], ["reading_id"]),
    _t("mfg_qms.inspection", "报工终检记录；按主要缺陷类型记录不良数", [
        ("inspection_id", "BIGINT NOT NULL", "质检记录编号"), ("report_id", "BIGINT", "关联报工记录编号"),
        ("inspection_date", "DATE", "质检业务日期"), ("line_id", "VARCHAR(16)", "产线编码"),
        ("shift_id", "INT", "班次编号"), ("defect_type", "VARCHAR(32)", "主要缺陷类型"),
        ("inspected_qty", "INT", "检验件数"), ("defect_qty", "INT", "不良件数"),
        ("inspection_stage", "VARCHAR(16)", "检验阶段")], ["inspection_id"]),
    _t("mfg_eam.maintenance_order", "设备维护工单；包含计划性与故障性维护", [
        ("order_id", "BIGINT NOT NULL", "维护工单编号"), ("line_id", "VARCHAR(16)", "维护产线编码"),
        ("factory_id", "INT", "所属工厂编号"), ("start_date", "DATE", "开工业务日期（日汇总计数日期）"),
        ("start_time", "DATETIME", "开工时间（Asia/Shanghai）"), ("end_time", "DATETIME", "完工时间（Asia/Shanghai）"),
        ("maintenance_type", "VARCHAR(16)", "维护类别：计划性、故障性"),
        ("reason", "VARCHAR(64)", "维护原因及工作内容"), ("duration_minutes", "INT", "工单历时（分钟）"),
        ("status", "VARCHAR(16)", "工单状态")], ["order_id"]),
    _t("mfg_eam.downtime", "设备停机事件；跨日事件按业务日拆分", [
        ("downtime_id", "BIGINT NOT NULL", "停机记录编号"), ("line_id", "VARCHAR(16)", "停机产线编码"),
        ("stat_date", "DATE", "停机业务日期"), ("start_time", "DATETIME", "本业务日停机开始时间"),
        ("end_time", "DATETIME", "本业务日停机结束时间"),
        ("duration_minutes", "INT", "本业务日停机时长（分钟）"),
        ("downtime_type", "VARCHAR(16)", "停机类别：计划性、故障性"),
        ("reason", "VARCHAR(64)", "停机原因"), ("maintenance_order_id", "BIGINT", "关联维护工单编号")], ["downtime_id"]),
]

VIEW_COLUMNS = {
    "mfg_dw.dws_production_daily": [
        ("stat_date", "生产业务日期"), ("factory_id", "工厂编号"), ("factory_name", "工厂名称"),
        ("region_name", "所在区域"), ("line_id", "产线编码"), ("line_name", "产线名称"),
        ("model_name", "设备型号"), ("product_family", "产品族"), ("production_qty", "产量（件）"),
        ("good_qty", "良品数（件）"), ("defect_qty", "不良数（件）"), ("work_hours", "生产工时（小时）"),
        ("downtime_minutes", "停机时长（分钟）"), ("downtime_count", "停机事件数"),
        ("energy_kwh", "能耗（千瓦时）；缺读为未知而非零"),
        ("energy_reading_count", "实际能耗读数数"), ("expected_reading_count", "按排班应有能耗读数数")],
    "mfg_dw.dws_material_daily": [
        ("stat_date", "入库业务日期"), ("supplier_id", "供应商编号"), ("supplier_name", "供应商名称"),
        ("material_id", "物料编号"), ("material_name", "物料名称"),
        ("batch_count", "入库批次数"), ("received_qty", "入库数量（千克）")],
    "mfg_dw.dws_maintenance_daily": [
        ("stat_date", "工单开工业务日期"), ("factory_id", "工厂编号"), ("factory_name", "工厂名称"),
        ("region_name", "所在区域"), ("order_count", "维护工单数"),
        ("planned_count", "计划性维护工单数"), ("corrective_count", "故障性维护工单数"),
        ("maintenance_minutes", "按开工日归属的工单历时（分钟）")],
}

VIEWS = [
    ("mfg_dw.dws_production_daily", "工厂产线日生产汇总；各事实先聚合再关联，缺读保留未知值", """
SELECT s.stat_date, f.factory_id, f.factory_name, f.region_name, l.line_id, l.line_name,
       em.model_name, pf.product_family,
       COALESCE(p.production_qty, 0) AS production_qty, COALESCE(p.good_qty, 0) AS good_qty,
       COALESCE(p.defect_qty, 0) AS defect_qty, COALESCE(p.work_hours, 0) AS work_hours,
       COALESCE(d.downtime_minutes, 0) AS downtime_minutes, COALESCE(d.downtime_count, 0) AS downtime_count,
       e.energy_kwh, COALESCE(e.energy_reading_count, 0) AS energy_reading_count, s.expected_reading_count
FROM (
  SELECT stat_date, line_id,
         SUM(CASE WHEN schedule_status <> '未投产' THEN 1 ELSE 0 END) AS expected_reading_count
  FROM mfg_mes.schedule GROUP BY stat_date, line_id
) s
JOIN mfg_mes.production_line l ON l.line_id = s.line_id
JOIN mfg_mes.factory f ON f.factory_id = l.factory_id
JOIN mfg_mes.equipment_model em ON em.model_id = l.model_id
JOIN mfg_mes.product_family pf ON pf.product_family_id = l.product_family_id
LEFT JOIN (
  SELECT production_date, line_id, SUM(production_qty) AS production_qty, SUM(good_qty) AS good_qty,
         SUM(defect_qty) AS defect_qty, SUM(work_hours) AS work_hours
  FROM mfg_mes.production_report GROUP BY production_date, line_id
) p ON p.production_date = s.stat_date AND p.line_id = s.line_id
LEFT JOIN (
  SELECT stat_date, line_id, SUM(duration_minutes) AS downtime_minutes, COUNT(*) AS downtime_count
  FROM mfg_eam.downtime GROUP BY stat_date, line_id
) d ON d.stat_date = s.stat_date AND d.line_id = s.line_id
LEFT JOIN (
  SELECT reading_date, line_id, SUM(energy_kwh) AS energy_kwh, COUNT(*) AS energy_reading_count
  FROM mfg_mes.energy_reading GROUP BY reading_date, line_id
) e ON e.reading_date = s.stat_date AND e.line_id = s.line_id"""),
    ("mfg_dw.dws_material_daily", "按供应商物料入库业务日汇总批次与数量", """
SELECT r.receipt_date AS stat_date, s.supplier_id, s.supplier_name, m.material_id, m.material_name,
       COUNT(DISTINCT r.batch_no) AS batch_count, SUM(r.received_qty) AS received_qty
FROM mfg_scm.material_receipt r
JOIN mfg_scm.supplier s ON s.supplier_id = r.supplier_id
JOIN mfg_scm.material m ON m.material_id = r.material_id
GROUP BY r.receipt_date, s.supplier_id, s.supplier_name, m.material_id, m.material_name"""),
    ("mfg_dw.dws_maintenance_daily", "工厂日维护工单汇总（按开工业务日）", """
SELECT o.start_date AS stat_date, f.factory_id, f.factory_name, f.region_name, COUNT(*) AS order_count,
       SUM(CASE WHEN o.maintenance_type = '计划性' THEN 1 ELSE 0 END) AS planned_count,
       SUM(CASE WHEN o.maintenance_type = '故障性' THEN 1 ELSE 0 END) AS corrective_count,
       SUM(o.duration_minutes) AS maintenance_minutes
FROM mfg_eam.maintenance_order o
JOIN mfg_mes.factory f ON f.factory_id = o.factory_id
GROUP BY o.start_date, f.factory_id, f.factory_name, f.region_name"""),
]


# --- 标准答案（完全由生成行计算，不查询数据库）-----------------------------------------

def _records(ds: Dataset, table: str) -> list[dict]:
    return [dict(zip(TABLE_COLUMNS[table], row, strict=True)) for row in ds.tables[table]]


def views_daily(ds: Dataset) -> dict:
    factories = {r[0]: r for r in ds.tables["mfg_mes.factory"]}
    lines = {r[0]: r for r in ds.tables["mfg_mes.production_line"]}
    models = {r[0]: r[1] for r in ds.tables["mfg_mes.equipment_model"]}
    families = {r[0]: r[1] for r in ds.tables["mfg_mes.product_family"]}
    suppliers = {r[0]: r[1] for r in ds.tables["mfg_scm.supplier"]}
    materials = {r[0]: r[1] for r in ds.tables["mfg_scm.material"]}
    expected: dict = defaultdict(int)
    for row in _records(ds, "mfg_mes.schedule"):
        expected[(row["stat_date"], row["line_id"])] += int(row["schedule_status"] != "未投产")
    production = defaultdict(lambda: [0, 0, 0, Decimal("0.00")])
    for row in _records(ds, "mfg_mes.production_report"):
        values = [row[c] for c in ("production_qty", "good_qty", "defect_qty", "work_hours")]
        key = (row["production_date"], row["line_id"])
        production[key] = [a + b for a, b in zip(production[key], values, strict=True)]
    downtime = defaultdict(lambda: [0, 0])
    for row in _records(ds, "mfg_eam.downtime"):
        key = (row["stat_date"], row["line_id"])
        downtime[key][0] += row["duration_minutes"]
        downtime[key][1] += 1
    energy = defaultdict(lambda: [Decimal("0.00"), 0])
    for row in _records(ds, "mfg_mes.energy_reading"):
        key = (row["reading_date"], row["line_id"])
        energy[key][0] += row["energy_kwh"]
        energy[key][1] += 1
    production_rows = []
    for (d, line_id), expected_n in sorted(expected.items()):
        line = lines[line_id]
        factory = factories[line[2]]
        energy_value, reading_n = energy.get((d, line_id), (None, 0))
        values = [d, factory[0], factory[1], factory[2], line_id, line[1], models[line[3]], families[line[4]],
                  *production[(d, line_id)], *downtime[(d, line_id)], energy_value, reading_n, expected_n]
        production_rows.append([None if v is None else str(v) for v in values])
    material = {}
    for r in _records(ds, "mfg_scm.material_receipt"):
        key = (r["receipt_date"], r["supplier_id"], suppliers[r["supplier_id"]], r["material_id"], materials[r["material_id"]])
        batches, qty = material.setdefault(key, (set(), Decimal("0.00")))
        batches.add(r["batch_no"])
        material[key] = (batches, qty + r["received_qty"])
    maintenance = defaultdict(lambda: [0, 0, 0, 0])
    for r in _records(ds, "mfg_eam.maintenance_order"):
        factory = factories[r["factory_id"]]
        value = maintenance[(r["start_date"], factory[0], factory[1], factory[2])]
        value[0] += 1
        value[1] += int(r["maintenance_type"] == "计划性")
        value[2] += int(r["maintenance_type"] == "故障性")
        value[3] += r["duration_minutes"]
    return {
        "dws_production_daily": {"columns": [c[0] for c in VIEW_COLUMNS["mfg_dw.dws_production_daily"]], "rows": production_rows},
        "dws_material_daily": {"columns": [c[0] for c in VIEW_COLUMNS["mfg_dw.dws_material_daily"]],
                               "rows": [[*map(str, k), str(len(v[0])), str(v[1])] for k, v in sorted(material.items())]},
        "dws_maintenance_daily": {"columns": [c[0] for c in VIEW_COLUMNS["mfg_dw.dws_maintenance_daily"]],
                                  "rows": [[*map(str, k), *map(str, v)] for k, v in sorted(maintenance.items())]},
    }


def _totals(rows: list[dict]) -> dict:
    fields = ("production_qty", "good_qty", "defect_qty", "work_hours", "downtime_minutes", "downtime_count",
              "energy_reading_count", "expected_reading_count")
    values = {c: sum((Decimal(r[c]) for r in rows), Decimal("0")) for c in fields}
    energies = [Decimal(r["energy_kwh"]) for r in rows if r["energy_kwh"] is not None]
    missing = values["energy_reading_count"] < values["expected_reading_count"]
    energy = sum(energies, Decimal("0")) if energies else None
    qty = values["production_qty"]
    return {
        **{c: int(v) if c != "work_hours" else str(v) for c, v in values.items()},
        "energy_kwh": str(energy) if energy is not None else None,
        "yield_rate": str(values["good_qty"] / qty) if qty else None,
        "unit_energy": str(energy / qty) if qty and energy is not None and not missing else None,
        "energy_complete": not missing,
    }


def truth(ds: Dataset) -> dict:
    daily = views_daily(ds)
    production = [dict(zip(daily["dws_production_daily"]["columns"], r, strict=True))
                  for r in daily["dws_production_daily"]["rows"]]

    def select(start, end, line=None, factory=None):
        return [r for r in production if start <= r["stat_date"] <= end
                and (line is None or r["line_name"] in ([line] if isinstance(line, str) else line))
                and (factory is None or r["factory_name"] == factory)]

    def period(start, end, **filters):
        return {"period": {"start": start, "end": end}, **_totals(select(start, end, **filters))}

    def grouped(fields, day=False):
        groups = defaultdict(list)
        for r in production:
            groups[tuple(r[f] for f in fields) + (r["stat_date"] if day else r["stat_date"][:7],)].append(r)
        return {str(k): _totals(v) for k, v in sorted(groups.items())}

    aug, sep = ("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-30")
    reports = _records(ds, "mfg_mes.production_report")
    bad_reports = [r for r in reports if r["batch_no"] == BAD_BATCH]
    receipts = _records(ds, "mfg_scm.material_receipt")
    bad_receipt = next(r for r in receipts if r["batch_no"] == BAD_BATCH)
    supplier_names = dict(ds.tables["mfg_scm.supplier"])
    material_names = {r[0]: r[1] for r in ds.tables["mfg_scm.material"]}
    order = next(r for r in _records(ds, "mfg_eam.maintenance_order") if r["line_id"] == "SC3" and r["duration_minutes"] > 1440)
    inspections = _records(ds, "mfg_qms.inspection")
    bad_report_ids = {r["report_id"] for r in bad_reports}
    bad_defects = defaultdict(int)
    for row in inspections:
        if row["report_id"] in bad_report_ids:
            bad_defects[row["defect_type"]] += row["defect_qty"]
    model_month = grouped(("model_name",))
    # 关系题使用完整的 8 月，不能以 9 月标准 B 型的部分能耗计算有效单位能耗。
    model_comparison = {name: model_month[str((name, "2026-08"))] for _, name, _ in EQUIPMENT_MODELS}
    ranked_models = sorted(model_comparison, key=lambda n: Decimal(model_comparison[n]["unit_energy"]))
    return {
        "version": VERSION, "seed": SEED, "timezone": TIMEZONE,
        "rows": {t: len(r) for t, r in sorted(ds.tables.items())},
        "factory_month": grouped(("factory_name",)), "line_month": grouped(("line_name",)),
        "factory_line_month": grouped(("factory_name", "line_name")), "model_month": model_month,
        "line_day": grouped(("line_name",), day=True), "factory_day": grouped(("factory_name",), day=True),
        "views_daily": daily,
        "scenarios": {
            "material_batch": {
                "before": period(*aug, line=["华南1号线", "华南2号线"]),
                "after": period(*sep, line=["华南1号线", "华南2号线"]),
                "primary_entity": {"supplier_name": supplier_names[bad_receipt["supplier_id"]], "batch_no": bad_receipt["batch_no"]},
                "mechanism_assets": ["mfg_scm.material_receipt", "mfg_qms.inspection"],
                "line_names": sorted({next(line[1] for line in LINES if line[0] == r["line_id"]) for r in bad_reports}),
                "receipt_date": str(bad_receipt["receipt_date"]), "material_name": material_names[bad_receipt["material_id"]],
                "batch_production_qty": sum(r["production_qty"] for r in bad_reports),
                "batch_defect_qty": sum(r["defect_qty"] for r in bad_reports),
                "defects_by_type": dict(sorted(bad_defects.items())), "outside_yield_lineage": True,
            },
            "planned_maintenance": {
                "before": period(*aug, line="华南3号线"), "after": period(*sep, line="华南3号线"),
                "primary_entity": "华南3号线", "mechanism_assets": ["mfg_eam.maintenance_order", "mfg_eam.downtime"],
                "start": str(order["start_time"]), "end_exclusive": str(order["end_time"]),
                "planned_downtime_minutes": order["duration_minutes"], "reason": order["reason"],
                "shutdown_period": period("2026-09-08", "2026-09-14", line="华南3号线"),
            },
            "new_line": {
                "before": period("2026-07-01", "2026-07-31", factory="西南工厂"),
                "after": period(*aug, factory="西南工厂"), "september": period(*sep, factory="西南工厂"),
                "primary_entity": "西南4号线", "mechanism_assets": ["mfg_mes.production_line", "mfg_qms.inspection"],
                "launch_date": str(next(line[6] for line in LINES if line[0] == "SW4")),
                "line_july": period("2026-07-01", "2026-07-31", line="西南4号线"),
                "line_august": period(*aug, line="西南4号线"), "line_september": period(*sep, line="西南4号线"),
                "first_14_days": period("2026-08-01", "2026-08-14", line="西南4号线"),
                "later_august": period("2026-08-15", "2026-08-31", line="西南4号线"),
            },
            "mid_autumn": {
                "before": period("2026-09-22", "2026-09-24"), "after": period("2026-09-25", "2026-09-27"),
                "primary_entity": ["华南工厂", "西南工厂"], "mechanism_assets": ["mfg_mes.schedule"],
                "factories": {name: {"before": period("2026-09-22", "2026-09-24", factory=name),
                                      "after": period("2026-09-25", "2026-09-27", factory=name)}
                              for _, name, _ in FACTORIES},
            },
            "offsetting": {
                "before": period(*aug, factory="华东工厂"), "after": period(*sep, factory="华东工厂"),
                "primary_entity": ["华东1号线", "华东2号线"], "mechanism_assets": ["mfg_mes.production_report"],
                "lines": {name: {"before": period(*aug, line=name), "after": period(*sep, line=name)}
                          for name in ("华东1号线", "华东2号线", "华东3号线")},
            },
            "energy_gap": {
                "before": period("2026-09-17", "2026-09-17", line="西南2号线"),
                "after": period("2026-09-18", "2026-09-18", line="西南2号线"),
                "primary_entity": "西南2号线", "mechanism_assets": ["mfg_mes.energy_reading", "mfg_mes.schedule"],
                "date": str(ENERGY_GAP[1]), "following_day": period("2026-09-19", "2026-09-19", line="西南2号线"),
            },
            "stable": {
                "before": period(*aug, line="华东3号线"), "after": period(*sep, line="华东3号线"),
                "primary_entity": "华东3号线", "mechanism_assets": ["mfg_mes.energy_reading", "mfg_mes.production_report"],
                "metric": "unit_energy", "interpretation": "差异在噪声范围内",
            },
            "energy_relationship": {
                "before": {"period": {"start": "2026-07-01", "end": "2026-07-31"},
                           "models": {name: model_month[str((name, "2026-07"))] for _, name, _ in EQUIPMENT_MODELS}},
                "after": {"period": {"start": aug[0], "end": aug[1]}, "models": model_comparison},
                "primary_entity": ranked_models, "mechanism_assets": ["mfg_mes.equipment_model", "mfg_mes.energy_reading"],
                "interpretation": "型号分组的单位能耗相关关系；不单凭分组结果断言因果",
            },
        },
    }


def checksum(ds: Dataset) -> str:
    h = hashlib.sha256()
    for table in sorted(ds.tables):
        h.update(table.encode("utf-8"))
        for row in ds.tables[table]:
            h.update(repr(row).encode("utf-8"))
    return h.hexdigest()


def ddl_checksum() -> str:
    return hashlib.sha256(repr((TABLES, VIEWS, VIEW_COLUMNS)).encode("utf-8")).hexdigest()


# --- 灌数：只在显式 load/check 子命令调用时连接演示库 ----------------------------------

def _literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, datetime):
        return f"'{value:%Y-%m-%d %H:%M:%S}'"
    if isinstance(value, date):
        return f"'{value.isoformat()}'"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


def _view_ddl(name: str, comment: str, sql: str) -> str:
    columns = ", ".join(f"{column} COMMENT {_literal(text)}" for column, text in VIEW_COLUMNS[name])
    return f"CREATE VIEW {name} ({columns}) COMMENT {_literal(comment)} AS {sql.strip()}"


def loaded_checksum(conn) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT TABLE_COMMENT FROM information_schema.tables WHERE TABLE_SCHEMA = 'mfg_mes' "
                    "AND TABLE_NAME = 'factory'")
        row = cur.fetchone()
    comment = row[0] if row else ""
    return comment.split("#", 1)[1] if comment and "#" in comment else None


def load(env: dict[str, str], force: bool = False) -> dict:
    import demo

    ds = generate()
    table_digest = hashlib.sha256(repr(TABLES).encode("utf-8")).hexdigest()[:12]
    digest = f"{VERSION}:{checksum(ds)[:12]}:{table_digest}:{ddl_checksum()[:12]}"
    conn = demo.sr_root(env)
    try:
        loaded = loaded_checksum(conn)
        if not force and loaded == digest:
            demo.log(f"制造数据集已是 {digest}，跳过灌数（--force 可强制重建）")
        elif not force and loaded and loaded.rsplit(":", 1)[0] == digest.rsplit(":", 1)[0]:
            demo.log(f"制造数据与源表未变，只重建视图：{loaded} → {digest}")
            with conn.cursor() as cur:
                for name, comment, sql in VIEWS:
                    cur.execute(f"DROP VIEW IF EXISTS {name}")
                    cur.execute(_view_ddl(name, comment, sql))
                cur.execute(f"ALTER TABLE mfg_mes.factory COMMENT = '工厂维度#{digest}'")
        else:
            demo.log(f"灌入制造数据集 {digest} ...")
            with conn.cursor() as cur:
                for db in DATABASES:
                    cur.execute(f"DROP DATABASE IF EXISTS {db} FORCE")
                    cur.execute(f"CREATE DATABASE {db}")
                for ddl in TABLES:
                    cur.execute(ddl)
                for table, rows in ds.tables.items():
                    for i in range(0, len(rows), 4000):
                        values = ",".join("(" + ",".join(_literal(v) for v in row) + ")" for row in rows[i:i + 4000])
                        cur.execute(f"INSERT INTO {table} VALUES {values}")
                    demo.log(f"  {table}：{len(rows)} 行")
                for name, comment, sql in VIEWS:
                    cur.execute(_view_ddl(name, comment, sql))
                cur.execute(f"ALTER TABLE mfg_mes.factory COMMENT = '工厂维度#{digest}'")
        grant(conn, env)
    finally:
        conn.close()
    TRUTH_FILE.parent.mkdir(exist_ok=True)
    TRUTH_FILE.write_text(json.dumps(ds.truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return ds.truth


def grant(conn, env: dict[str, str]) -> None:
    """演示采集与查询账号对制造库只读；seed 重建账号后可以单独重新授权。"""
    import demo

    with conn.cursor() as cur:
        for user in demo.SR_USERS:
            for db in DATABASES:
                cur.execute(f"GRANT SELECT ON ALL TABLES IN DATABASE {db} TO USER '{user}'@'%'")
                cur.execute(f"GRANT SELECT ON ALL VIEWS IN DATABASE {db} TO USER '{user}'@'%'")


def _canonical(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value)
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return str(Decimal(text).normalize())
    return text


def check(env: dict[str, str]) -> bool:
    """比较所有源表及全部日汇总视图的完整多重行集合，双向检测缺行、多行和错值。"""
    import demo

    ds = generate()
    expected = {table: {"columns": TABLE_COLUMNS[table], "rows": rows} for table, rows in ds.tables.items()}
    expected.update({f"mfg_dw.{name}": view for name, view in ds.truth["views_daily"].items()})
    conn = demo.sr_connect(env, "avelis_reader")
    ok = True
    try:
        with conn.cursor() as cur:
            for table, spec in sorted(expected.items()):
                columns = ", ".join(spec["columns"])
                cur.execute(f"SELECT {columns} FROM {table}")
                actual_rows = Counter(tuple(_canonical(v) for v in row) for row in cur.fetchall())
                want_rows = Counter(tuple(_canonical(v) for v in row) for row in spec["rows"])
                same = actual_rows == want_rows
                ok &= same
                missing, extra = sum((want_rows - actual_rows).values()), sum((actual_rows - want_rows).values())
                demo.log(f"  {'通过' if same else '不符'}  {table}：期望 {sum(want_rows.values())} 行，"
                         f"实际 {sum(actual_rows.values())} 行，缺少 {missing}，多出或错值 {extra}")
    finally:
        conn.close()
    return ok


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    load_parser = sub.add_parser("load")
    load_parser.add_argument("--force", action="store_true")
    sub.add_parser("truth")
    sub.add_parser("check")
    args = parser.parse_args()
    if args.cmd == "truth":
        print(json.dumps(generate().truth, ensure_ascii=False, indent=2))
    else:
        import demo

        env = demo.load_env()
        if args.cmd == "load":
            load(env, args.force)
        sys.exit(0 if check(env) else 1)


if __name__ == "__main__":
    main()
