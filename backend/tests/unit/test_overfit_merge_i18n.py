"""合并后的语言边界：固定文案翻译，业务原值与时区参数保持原样。"""

from __future__ import annotations

import pymysql
import pytest
from pydantic import ValidationError

from avelis.analysis import export
from avelis.api.schemas import BusinessCalendarIn
from avelis.connectors import starrocks
from avelis.connectors.base import QueryError, checked_timezone
from avelis.governance.errors import Invalid
from avelis.i18n import Msg, locale_context, translate
from tests.pymysql_contract import mogrify
from tests.unit.test_overfit_batch2 import meta_validate


@pytest.mark.parametrize("locale,separator", [("zh-CN", "："), ("zh-TW", "："), ("en", ": ")])
@pytest.mark.parametrize("member", ["因子指标 / 原始成員", 0])
def test_export_member_label_translates_separator_and_keeps_business_values(locale, separator, member):
    dimension = "未标注实体 / 原始維度"
    cause = {"type": "member", "label": "不应替代实体", "entity": {"dimension_label": dimension, "member": member}}
    with locale_context(locale):
        assert export._cause_label(cause) == f"{dimension}{separator}{member}"
        assert export._cause_label({"type": "member", "entity": {"member": member}}) == str(member)


@pytest.mark.parametrize(
    "locale,unlabeled,factor",
    [
        ("zh-CN", "未标注实体", "因子指标"),
        ("zh-TW", "未標註實體", "因子指標"),
        ("en", "Unlabeled entity", "Factor metric"),
    ],
)
def test_export_cause_fallbacks_translate_and_explicit_labels_remain_original(locale, unlabeled, factor):
    with locale_context(locale):
        assert export._cause_label({"type": "member"}) == unlabeled
        assert export._cause_label({"type": "member", "entity": {"member": None}}) == unlabeled
        assert export._cause_label({"type": "factor"}) == factor
        assert export._cause_label({}) == unlabeled
        for kind in ("member", "factor"):
            assert export._cause_label({"type": kind, "label": "因子指标 / 原始標籤"}) == "因子指标 / 原始標籤"


@pytest.mark.parametrize(
    "locale,date_message,overlap_message",
    [
        ("zh-CN", "日期必须是合法的 YYYY-MM-DD", "同一天不能既是节假日又是调休上班日"),
        ("zh-TW", "日期必須是有效的 YYYY-MM-DD", "同一天不能同時是假日與補班日"),
        (
            "en",
            "Dates must be valid YYYY-MM-DD values",
            "A date cannot be both a holiday and a compensatory working day",
        ),
    ],
)
def test_business_calendar_validation_messages_follow_locale(locale, date_message, overlap_message):
    with locale_context(locale):
        for values, expected in (
            ({"holidays": ["2026-02-29"]}, date_message),
            ({"workdays": ["2026-02-29"]}, date_message),
            ({"holidays": ["2026-01-01"], "workdays": ["2026-01-01"]}, overlap_message),
        ):
            with pytest.raises(ValidationError) as caught:
                BusinessCalendarIn(**values)
            errors = caught.value.errors()
            assert len(errors) == 1
            assert str(errors[0]["ctx"]["error"]) == expected
            assert errors[0]["msg"] == f"Value error, {expected}"


@pytest.mark.parametrize(
    "locale,prefix",
    [
        ("zh-CN", "业务时区无效："),
        ("zh-TW", "業務時區無效："),
        ("en", "Invalid business time zone: "),
    ],
)
def test_invalid_connector_timezone_translates_without_rewriting_original_name(locale, prefix):
    name = "User/原始時區"
    with locale_context(locale), pytest.raises(QueryError) as caught:
        checked_timezone(name)
    assert caught.value.code == "other" and caught.value.db_errno is None
    assert caught.value.message == prefix + name


@pytest.mark.parametrize(
    "locale,required_phrase",
    [
        ("zh-TW", "資料庫不支援業務時區"),
        ("en", "The database does not support business time zone"),
    ],
)
def test_starrocks_fixed_offset_warning_translates_and_keeps_timezone_parameters(monkeypatch, locale, required_phrase):
    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, sql, params=None):
            mogrify(sql, params)
            self.statements.append(sql)
            if len(self.statements) == 1:
                raise pymysql.err.OperationalError(1298, "Unknown or incorrect time zone")

    def offset_for(name):
        assert name == "Asia/Kathmandu"
        return "+05:45"

    monkeypatch.setattr(starrocks, "current_timezone_offset", offset_for)
    cursor = Cursor()
    with locale_context(locale):
        warnings = starrocks._set_timezone(cursor, "Asia/Kathmandu")
    assert cursor.statements == ["SET time_zone = 'Asia/Kathmandu'", "SET time_zone = '+05:45'"]
    assert len(warnings) == 1
    assert required_phrase in warnings[0]
    assert warnings[0].count("Asia/Kathmandu") == warnings[0].count("+05:45") == 1
    assert "{name}" not in warnings[0] and "{offset}" not in warnings[0]


@pytest.mark.parametrize("locale", ["zh-CN", "zh-TW", "en"])
@pytest.mark.parametrize(
    "data_type,time_format,template",
    [
        ("decimal(8, 0)", None, "时间字段必须是日期或时间类型：{column}（{data_type}）"),
        ("decimal(8, 0)", "yyyymmdd", "编码时间字段必须是整数或字符串类型：{column}（{data_type}）"),
        ("integer", "yyyy-mm-dd", "yyyy-mm-dd 时间格式要求时间字段为字符串类型：{column}（{data_type}）"),
    ],
)
def test_metric_time_type_errors_keep_deferred_translation_and_original_field_values(
    monkeypatch, locale, data_type, time_format, template
):
    with pytest.raises(Invalid) as caught:
        meta_validate(monkeypatch, data_type=data_type, time_format=time_format)
    message = caught.value.message
    assert isinstance(message, Msg)
    assert message.template == template
    assert message.params == {"column": "时间甲", "data_type": data_type}
    with locale_context(locale):
        rendered = translate(message)
    assert "时间甲" in rendered and data_type in rendered
    assert "{column}" not in rendered and "{data_type}" not in rendered
    assert (rendered == str(message)) == (locale == "zh-CN")
