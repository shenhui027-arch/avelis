"""审计自由文本脱敏的兼容性、扫描上限和线性耗时回归。"""

from statistics import median
from time import perf_counter

import pytest

from avelis import events
from avelis.events import sanitize_detail


@pytest.mark.parametrize(
    "text",
    [
        "SELECT private_column\nFROM private_table",
        "UPDATE private_table\nSET value = 1",
        "insert into private_table values (1)",
        "delete from private_table",
        "create table private_table (id int)",
        "AlTeR\nViEw private_view",
        "drop table private_table",
        "Bearer private-token",
        "password=private-pass",
        "passwd : private-pass",
        "secret=private-secret",
        "token:private-token",
        "API-key = private-key",
        "postgresql://u:p@h/db",
        "://u@name:p@host",
        "://:@host",
        "://a://u:p@host",
        "select   from",
        "select x from_suffix",
        "update x setting",
        "select from select x from source",
        "update set update x set value",
        "x" * 2001 + " SELECT x FROM source",
    ],
)
def test_sanitize_redacts_existing_private_text(text):
    assert sanitize_detail({"message": text}) == {"message": "[redacted]"}


@pytest.mark.parametrize(
    "text",
    [
        "ordinary audit message",
        "普通审计文本",
        "",
        "select from",
        "select  from",
        "update set",
        "update  set",
        "from source select column",
        "set value update table_name",
        "unselect x from source",
        "reupdate x set value",
        "https://example.invalid/path",
        "://user:pass/host@db",
        "://user:pass host@db",
        "://user@host",
        "x" * events._MAX_TEXT_SCAN_CHARS,
    ],
)
def test_sanitize_preserves_and_truncates_ordinary_text(text):
    assert sanitize_detail({"message": text}) == {"message": text[:2000]}


def test_sanitize_rejects_oversized_nested_text_before_scanning(monkeypatch):
    def unexpected_scan(value):
        pytest.fail("oversized text reached private-text scanning")

    monkeypatch.setattr(events, "_contains_private_text", unexpected_scan)
    text = "x" * (events._MAX_TEXT_SCAN_CHARS + 1)
    assert sanitize_detail({"message": text, "nested": [{"message": text}]}) == {
        "message": "[redacted]",
        "nested": [{"message": "[redacted]"}],
    }


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("select " * 9000, id="repeated-select"),
        pytest.param("update " * 9000, id="repeated-update"),
        pytest.param("://a:" * 12000, id="repeated-url-prefix"),
        pytest.param("://" + ":" * 60000, id="url-overlapping-colons"),
    ],
)
def test_sanitize_adversarial_text_has_bounded_linear_runtime(text):
    assert len(text) <= events._MAX_TEXT_SCAN_CHARS
    short_text = text[: len(text) // 4]
    # 先预热；交错测量两种长度，减少首次调用和机器负载变化的影响。
    sanitize_detail({"message": short_text})
    sanitize_detail({"message": text})
    short_times, long_times = [], []
    for _ in range(5):
        for value, timings in ((short_text, short_times), (text, long_times)):
            start = perf_counter()
            result = sanitize_detail({"message": value})
            timings.append(perf_counter() - start)
            assert result == {"message": value[:2000]}
    assert max(short_times + long_times) < 0.05
    assert median(long_times) / max(median(short_times), 0.010) <= 4.0
