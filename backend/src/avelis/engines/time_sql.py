"""引擎的业务时间表达式：有时区的时间点显式转换，不依赖列或会话的默认时区。"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlglot import exp

from avelis.governance.errors import Invalid
from avelis.lineage.sql import SqlDialect


def _time_type(data_type: str) -> str:
    normalized = data_type.lower().strip()
    for wrapper in ("nullable(", "lowcardinality("):
        if normalized.startswith(wrapper) and normalized.endswith(")"):
            return _time_type(normalized[len(wrapper):-1])
    return " ".join(normalized.split())


def _zoned(data_type: str, dialect: str) -> bool:
    kind = _time_type(data_type)
    if dialect == "clickhouse":
        return kind.startswith("datetime")
    if dialect == "tsql":
        return kind.startswith("datetimeoffset")
    if dialect in ("postgres", "oracle"):
        return kind.startswith("timestamptz") or (kind.startswith("timestamp") and (
            "with time zone" in kind or "with local time zone" in kind))
    return False


def _timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise Invalid("带时区的时间列需要有效的业务时区") from exc


def time_conditions(column: exp.Expression, start: date, end: date, *, data_type: str,
                    business_timezone: str | None, dialect: SqlDialect) -> list[exp.Expression]:
    """日期用闭区间，时间点用 [本地午夜, 次日本地午夜)，分别计算两个边界的夏令时偏移。"""
    if _zoned(data_type, dialect.name):
        zone = _timezone(business_timezone)

        def bound(day: date) -> exp.Expression:
            midnight = datetime.combine(day, time.min, zone)
            if dialect.name == "clickhouse":
                # SQLGlot 输出带 UTC 类型的 DateTime64；比较双方均为时间点，避免字符串按列时区解释。
                return exp.TimeStrToTime(this=exp.Literal.string(
                    midnight.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
                    zone=exp.Literal.string("UTC"))
            if dialect.name == "oracle":
                # SQLGlot 的 StrToTime 只生成 TO_TIMESTAMP，无法表达 TO_TIMESTAMP_TZ 的格式掩码。
                return exp.Anonymous(this="TO_TIMESTAMP_TZ", expressions=[
                    exp.Literal.string(midnight.isoformat(sep=" ")),
                    exp.Literal.string("YYYY-MM-DD HH24:MI:SSTZH:TZM")])
            return exp.Cast(this=exp.Literal.string(midnight.isoformat(sep=" ")),
                            to=exp.DataType.build("TIMESTAMPTZ"))

        low, high = bound(start), bound(end + timedelta(days=1))
    elif (_time_type(data_type).startswith("date") and not _time_type(data_type).startswith("datetime")
          and not dialect.date_has_time):
        return [exp.Between(this=column, low=exp.Literal.string(str(start)), high=exp.Literal.string(str(end)))]
    else:
        low, high = (exp.Literal.string(f"{day} 00:00:00") for day in (start, end + timedelta(days=1)))
    return [exp.GTE(this=column, expression=low), exp.LT(this=column.copy(), expression=high)]


def period_expression(column: exp.Expression, grain: str, *, data_type: str,
                      business_timezone: str | None, dialect: str) -> exp.Expression:
    """原生日期/时间的期间标签；日/周/季为 YYYY-MM-DD，月为 YYYY-MM，年为 YYYY。"""
    if _zoned(data_type, dialect):
        if dialect == "tsql":
            # SQL Server 接受 Windows 时区名，不能将口径的 IANA 名称直接传给 AT TIME ZONE。
            raise Invalid("SQL Server 的 datetimeoffset 时间列不支持按时间分组")
        zone = exp.Literal.string(_timezone(business_timezone).key)
        if dialect == "clickhouse":
            column = exp.Anonymous(this="toTimeZone", expressions=[column, zone])
        else:
            column = exp.AtTimeZone(this=column, zone=zone)
            if dialect == "oracle":
                # 先保留业务墙上时间再截断，避免 TRUNC 隐式转 DATE 时再次使用会话时区。
                column = exp.Cast(this=exp.Paren(this=column), to=exp.DataType.build("TIMESTAMP"))
    if grain in ("week", "quarter"):
        if dialect == "tsql":
            column = exp.TimestampTrunc(this=column, unit=exp.Var(this="ISO_WEEK" if grain == "week" else "QUARTER"))
        elif dialect == "oracle":
            # DateTrunc 会把 Q 规整成 Oracle 不接受的 QUARTER，保留原生 TRUNC 格式符。
            column = exp.Anonymous(this="TRUNC", expressions=[column, exp.Literal.string("IW" if grain == "week" else "Q")])
        elif dialect == "mysql" and grain == "week":
            column = exp.DateSub(this=column, expression=exp.Anonymous(this="WEEKDAY", expressions=[column.copy()]),
                                 unit=exp.Var(this="DAY"))
        else:
            column = exp.DateTrunc(this=column, unit=exp.Literal.string(grain))
    return exp.TimeToStr(this=column, format=exp.Literal.string({"month": "%Y-%m", "year": "%Y"}.get(grain, "%Y-%m-%d")))
