"""Shell word boundaries and linear, nested SQL comment scanning."""

from time import perf_counter

import pytest

from avelis.importer.candidates import _redact_lines, _redact_units, _SecretValueReader, redact


@pytest.mark.parametrize("newline", [chr(10), chr(13) + chr(10)])
@pytest.mark.parametrize("prefix", ["password=", "client --password=", "client --password ", "client --api-key "])
@pytest.mark.parametrize("unit", ["first", "$(printf first)", "${FIRST}", "`printf first`", "'first'"])
def test_shell_nbsp_is_part_of_the_secret_word(newline, prefix, unit):
    source = prefix + unit + chr(0xA0) + "SYNTH; echo public" + newline + "next=1"
    reader = _SecretValueReader(source)
    assert reader.shell_value_end(len(prefix)) == source.index(";")
    for redactor in (_redact_units, redact):
        cleaned, count = redactor(source)
        assert "SYNTH" not in cleaned
        assert chr(0xA0) not in cleaned
        assert cleaned.endswith("; echo public" + newline + "next=1")
        assert count == 1
        assert redactor(cleaned) == (cleaned, 0)


@pytest.mark.parametrize("boundary", [" ", chr(9), chr(13), chr(10), ";", "|", "&", "<", ">", ")"])
def test_shell_word_still_stops_at_ascii_whitespace_and_metacharacters(boundary):
    source = "first" + chr(0xA0) + "SYNTH" + boundary + "public"
    assert _SecretValueReader(source).shell_value_end(0) == source.index(boundary)


@pytest.mark.parametrize("newline", [chr(10), chr(13) + chr(10)])
@pytest.mark.parametrize("wrapper", ["{}", 'cur.execute("{}")'])
@pytest.mark.parametrize("comment", ["/*outer/*inner*/FROM*/", "/*outer\n/*inner*/\nFROM*/", "/*outer/*a*//*b*/FROM*/"])
def test_nested_sql_comments_do_not_expose_password_values(newline, wrapper, comment):
    clause = "ALTER ROLE r PASSWORD " + comment.replace(chr(10), newline) + "'SYNTH';"
    source = wrapper.format(clause) + newline + "SELECT 1;"
    expected = wrapper.format(clause.replace("SYNTH", "<redacted>")) + newline + "SELECT 1;"
    assert _redact_units(source) == (expected, 1)
    cleaned, count = redact(source)
    assert "SYNTH" not in cleaned
    assert cleaned.endswith(newline + "SELECT 1;")
    assert count >= 1
    assert redact(cleaned) == (cleaned, 0)


@pytest.mark.parametrize("closed", [False, True])
def test_block_comment_cache_keeps_each_openers_own_depth(closed):
    source = "/*outer /*inner*/ FROM" + ("*/" if closed else "")
    reader = _SecretValueReader(source)
    inner_start = source.index("/*", 2) + 2
    inner_end = source.index("*/")
    outer_end = source.rfind("*/") if closed else -1
    # Both lookup orders must work; a closed inner comment does not close its parent.
    for start, expected in [(2, outer_end), (inner_start, inner_end), (2, outer_end), (len(source), -1)]:
        assert reader.block_comment_end(start) == expected
    reader = _SecretValueReader(source)
    assert reader.block_comment_end(inner_start) == inner_end
    assert reader.block_comment_end(2) == outer_end


@pytest.mark.parametrize("newline", [chr(10), chr(13) + chr(10)])
def test_unclosed_outer_comment_is_a_value_even_when_inner_comment_closes(newline):
    prefix = "ALTER ROLE r PASSWORD "
    source = prefix + "/*outer" + newline + "/*inner*/ FROM SYNTH"
    for masked_values in (False, True):
        reader = _SecretValueReader(source)
        assert reader.password_value_start(len(prefix) - 1, masked_values=masked_values) == len(prefix)
    for redactor in (_redact_units, _redact_lines, redact):
        assert redactor(source)[1] >= 1


@pytest.mark.parametrize("newline", [chr(10), chr(13) + chr(10)])
@pytest.mark.parametrize("comment, closing", [("/*", "*/ "), ("--", "")])
def test_repeated_masked_comment_lines_still_consult_their_own_suffix(newline, comment, closing):
    line = "PASSWORD " + comment + " token <redacted>"
    source = line + newline + closing + "FROM users" + newline + line + newline + closing + "'SYNTH'"
    cleaned, count = _redact_lines(source)
    assert cleaned.startswith(line + newline + closing + "FROM users" + newline + "PASSWORD<redacted>")
    assert count >= 1


def _timed_comment_chain(size, newline):
    source = "sql = '''" + newline + ("-- PASSWORD" + newline) * size + "SELECT 1;" + newline + "'''" + newline
    best = float("inf")
    for _ in range(3):
        started = perf_counter()
        actual = redact(source)
        best = min(best, perf_counter() - started)
        # Existing fallback covers pairs of empty PASSWORD lines, then SELECT.
        expected = (
            "sql = '''"
            + newline
            + ("-- PASSWORD<redacted>" + newline) * (size // 2)
            + "<redacted> 1;"
            + newline
            + "'''"
            + newline
        )
        assert actual == (expected, size + 1)
    return best


@pytest.mark.parametrize("newline", [chr(10), chr(13) + chr(10)])
def test_line_comment_chains_are_linear(newline):
    small = _timed_comment_chain(4000, newline)
    large = _timed_comment_chain(8000, newline)
    longest = _timed_comment_chain(20000, newline)
    assert longest < 0.5, f"20000 comments took {longest:.6f}s"
    assert large / small <= 2.3, f"4000={small:.6f}s, 8000={large:.6f}s, ratio={large / small:.3f}"
