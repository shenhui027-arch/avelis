"""整行兜底独立于语法单元层，覆盖漏值、跨行范围和线性耗时。"""

from time import perf_counter

import pytest

from avelis.importer.candidates import _FALLBACK_MAX_LINES, _redact_lines, _redact_units, redact


def assert_redacted(source: str, expected: str, count: int) -> None:
    assert redact(source) == (expected, count)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("source", "expected", "count"),
    [
        (
            """cur.execute("CREATE ROLE r PASSWORD " "'SYNTH1';")""",
            'cur.execute("CREATE ROLE r PASSWORD<redacted>',
            2,
        ),
        (
            """# user's account\ncur.execute("CREATE ROLE r PASSWORD ')SYNTH2';")""",
            """# user's account\ncur.execute("CREATE ROLE r PASSWORD<redacted>""",
            1,
        ),
        ("password: :SYNTH3", "password: <redacted>", 1),
        ('connect("password::SYNTH4@db")', 'connect("password:<redacted>', 1),
    ],
    ids=["adjacent-host-literals", "comment-apostrophe", "yaml-colon-value", "url-colon-password"],
)
def test_line_fallback_covers_leaks_without_teaching_units_new_cases(newline, source, expected, count):
    source = source.replace("\n", newline)
    # 保留第一层的漏值，证明修复由独立兜底层承担。
    assert "SYNTH" in _redact_units(source)[0]
    assert_redacted(source, expected.replace("\n", newline), count)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("source", "expected", "count"),
    [
        (
            'cur.execute(\n    "CREATE ROLE r PASSWORD "\n    "\'SYNTH42\';"\n)\nnext_call()',
            'cur.execute(\n    "CREATE ROLE r PASSWORD<redacted>\n)\nnext_call()',
            2,
        ),
        (
            'sql = ("ALTER ROLE r PASSWORD "\n       "\'SYNTH42\'")\nnext_call()',
            'sql = ("ALTER ROLE r PASSWORD<redacted>\nnext_call()',
            2,
        ),
        (
            "cur.execute(\n    'CREATE ROLE r PASSWORD '\n    '\"SYNTH42\";'\n)\nnext_call()",
            "cur.execute(\n    'CREATE ROLE r PASSWORD<redacted>\n)\nnext_call()",
            2,
        ),
        (
            'cfg = dict(\n    password=\n        "SYNTH42",\n)\nnext_call()',
            "cfg = dict(\n    password=\n<redacted>\n)\nnext_call()",
            1,
        ),
    ],
    ids=["paren-adjacent", "assign-adjacent", "single-quoted-host", "kw-next-line"],
)
def test_multiline_adjacent_literal_review_regressions(newline, source, expected, count):
    assert_redacted(source.replace("\n", newline), expected.replace("\n", newline), count)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "source, expected",
    [
        (
            "cur.execute('ALTER ROLE r PASSWORD \"'\n            \"A\"\n            'SYNTH42\";')\nnext_call()",
            "cur.execute('ALTER ROLE r PASSWORD<redacted>\nnext_call()",
        ),
        (
            "sql = ('ALTER ROLE r PASSWORD \"ab'\n       'SYNTH42\"')\nnext_call()",
            "sql = ('ALTER ROLE r PASSWORD<redacted>\nnext_call()",
        ),
    ],
    ids=["partial-then-adjacent", "partial-two-lines"],
)
def test_partial_password_values_extend_through_all_adjacent_literals(newline, source, expected):
    source = source.replace("\n", newline)
    expected = expected.replace("\n", newline)
    for redactor in (_redact_lines, redact):
        cleaned, count = redactor(source)
        assert cleaned == expected
        assert count >= 2
        assert redactor(cleaned) == (cleaned, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("comment", ["", "  # trailing comment with 'quotes' + \\"])
@pytest.mark.parametrize("config", [False, True])
def test_password_dictionary_entries_preserve_following_entries(newline, comment, config):
    value = 'ConfigKey("AVELIS_REDIS_PASSWORD", str, "SYNTH")' if config else '"SYNTH"'
    head = f'    "redis.password": {value},{comment}'
    tail = newline.join(
        [f'    "redis.option{index}": ConfigKey("AVELIS_OPTION{index}", bool, False),' for index in range(50)]
        + ["}", "next_call()"]
    )
    source = "config = {" + newline + head + newline + tail
    for redactor in (_redact_lines, redact):
        cleaned, count = redactor(source)
        assert "SYNTH" not in cleaned
        assert cleaned.endswith(newline + tail)
        assert count >= 1


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("suffix", [",", ")", "]", "}", ";", ":", " + pw"])
@pytest.mark.parametrize("comment", ["", "  # 'quoted' + \\"])
@pytest.mark.parametrize("location", ["keyword", "adjacent"])
def test_adjacent_literal_extension_stops_at_expression_boundaries(newline, suffix, comment, location):
    head = 'token "head"'
    if location == "adjacent":
        head += newline + '"SYNTH"'
    source = head + suffix + comment + newline + '"public"' + newline + "next_call()"
    for redactor in (_redact_lines, redact):
        cleaned, count = redactor(source)
        assert "SYNTH" not in cleaned
        assert cleaned.endswith(newline + '"public"' + newline + "next_call()")
        assert count >= 1


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("tail", ['"', "<redacted>", '" +', '" \\'])
def test_literal_tails_ignore_comments_but_preserve_hashes_inside_strings(newline, tail):
    source = 'token "head#value' + tail + "  # comment ends with , ) ] } ; :" + newline
    source += '"SYNTH#value"  # another comment ending with ,' + newline
    source += '"SYNTH-tail");' + newline + '"public"' + newline + "next_call()"
    # A marker is a complete value; do not leave an unmatched quote before it.
    if tail == "<redacted>":
        source = source.replace('"head#value<redacted>', "<redacted>")
    for redactor in (_redact_lines, redact):
        cleaned, count = redactor(source)
        assert "SYNTH" not in cleaned
        assert cleaned.endswith(newline + '"public"' + newline + "next_call()")
        assert count >= 3


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["", "r", "u", "b", "f", "br", "rb", "fr", "rf", "R", "BR", "RF"])
@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
@pytest.mark.parametrize("head", ['token "head"', "token <redacted>", 'password="head"'])
def test_existing_or_masked_values_extend_through_prefixed_literals(newline, prefix, quote, head):
    source = (
        f"{head}{newline} \t{newline}    {prefix}{quote}SYNTH{quote}{newline}"
        f"{newline}    {prefix}{quote}SYNTH-tail{quote}{newline}){newline}next_call()"
    )
    for redactor in (_redact_lines, redact):
        cleaned, count = redactor(source)
        assert "SYNTH" not in cleaned
        assert cleaned.endswith(f"{newline}){newline}next_call()")
        assert count >= 1
        assert redactor(cleaned) == (cleaned, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_cached_masked_line_still_checks_its_own_adjacent_literals(newline):
    source = f'token <redacted>{newline}public=1{newline}token <redacted>{newline}r"SYNTH"{newline}next_call()'
    expected = f"token <redacted>{newline}public=1{newline}token<redacted>{newline}next_call()"
    assert_redacted(source, expected, 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_masked_value_on_the_next_line_still_extends_through_literals(newline):
    source = f'password={newline}<redacted>{newline}b"SYNTH"{newline}){newline}next_call()'
    assert_redacted(source, f"password=<redacted>{newline}){newline}next_call()", 3)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("tail", ["", "public=1", 'raw_value + "SYNTH"', "repr('SYNTH')"])
def test_existing_value_preserves_blank_lines_without_a_following_literal(newline, tail):
    source = f"token <redacted>{newline} \t{newline}{tail}"
    assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_masked_value_does_not_extend_to_a_literal_beyond_the_line_budget(newline):
    source = f"token <redacted>{newline}" + newline * _FALLBACK_MAX_LINES + 'r"SYNTH"'
    assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("head", ['token "head"', "token <redacted>"])
def test_existing_values_cannot_restart_extension_from_a_covered_keyword(newline, head):
    literals = [f'r"SYNTH-{index}"' for index in range(24)]
    literals[18] = 'r"token SYNTH"'
    source = newline.join([head, *literals, ")", "next_call()"])
    expected = newline.join(["token<redacted>", *literals[19:], ")", "next_call()"])
    assert redact(source) == (expected, _FALLBACK_MAX_LINES)
    # 截断后仍有相邻字面量，再次调用按同一通用规则继续遮蔽。
    assert redact(expected) == (f"token<redacted>{newline}){newline}next_call()", 6)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("tail", ["", " +", " \\"])
def test_empty_host_tail_extends_through_adjacent_literals_and_keeps_closing_parenthesis(newline, quote, tail):
    source = (
        f"sql = ({quote}ALTER ROLE r PASSWORD {quote}{tail}{newline}"
        f"{newline}    {quote}SYNTH{quote} +{newline}"
        f"    {quote}SYNTH-tail{quote}{newline}){newline}next_call()"
    )
    expected = f"sql = ({quote}ALTER ROLE r PASSWORD<redacted>{newline}){newline}next_call()"
    for redactor in (_redact_lines, redact):
        assert redactor(source) == (expected, 3 if tail.endswith("\\") else 4)
        assert redactor(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("key", ["token", "secret", "api_key"])
def test_empty_bare_key_stops_after_a_nonliteral_value(newline, key):
    source = f'{key}{newline} \t{newline}SYNTH{newline}"SYNTH-tail"{newline}next_call()'
    assert redact(source) == (f'{key}<redacted>{newline}"SYNTH-tail"{newline}next_call()', 3)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_variable_concatenation_is_preserved_but_existing_values_extend(newline, quote):
    source = f"sql = {quote}ALTER ROLE r PASSWORD {quote} + pw{newline}next_call()"
    assert_redacted(source, source, 0)
    value_quote = '"' if quote == "'" else "'"
    source = (
        f"sql = {quote}ALTER ROLE r PASSWORD {value_quote}x{value_quote}{quote}{newline}"
        f"{quote}visible{quote}{newline}next_call()"
    )
    expected = f"sql = {quote}ALTER ROLE r PASSWORD<redacted>{newline}next_call()"
    assert _redact_lines(source) == (expected, 2)
    assert_redacted(source, expected, 3)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_adjacent_literal_extension_is_bounded_and_covered_lines_cannot_extend_again(newline):
    source = f'"set token "{newline}' + f'"SYNTH"{newline}' * 24 + f"){newline}next_call()"
    expected = f'"set token<redacted>{newline}' + f'"SYNTH"{newline}' * 5 + f"){newline}next_call()"
    assert redact(source) == (expected, _FALLBACK_MAX_LINES)
    # 每次调用仍只延伸 20 行；截断后的残留字面量不再满足幂等条件。
    assert redact(expected) == (f'"set token<redacted>{newline}){newline}next_call()', 6)
    source = f'"set token "{newline}"other token "{newline}public=1{newline}next_call()'
    assert_redacted(source, f'"set token<redacted>{newline}public=1{newline}next_call()', 2)


@pytest.mark.parametrize("scheme", ["postgres", "mysql", "postgresql+psycopg"])
@pytest.mark.parametrize("password", ["SYNTH", ":SYNTH"])
def test_url_userinfo_still_uses_unit_redaction_before_line_fallback(scheme, password):
    source = f'connect("{scheme}://user:{password}@db")'
    assert _redact_units(source) == (f'connect("{scheme}://user:<redacted>@db")', 1)
    assert_redacted(source, f'connect("{scheme}://user:<redacted>', 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "key",
    ["password", "PASSWD", "pwd", "secret", "Token", "api_key", "access-key", "secret_key", "private_key"],
)
def test_bare_secret_keywords_mask_only_their_logical_line(newline, key):
    # PASSWORD 裸值已由 SQL 单元层准确遮蔽，保留其空格。
    separator = " " if key == "password" else ""
    assert_redacted(
        f"before=1{newline}{key} SYNTH{newline}after=2",
        f"before=1{newline}{key}{separator}<redacted>{newline}after=2",
        1,
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("value", ['"SYNTH"', "'SYNTH'", '"""SYNTH"""'])
def test_fallback_uses_continuation_mapping_and_keeps_unaffected_text(newline, value):
    source = f"before=1{newline}token \\{newline}  {value}{newline}after=2"
    assert_redacted(source, f"before=1{newline}token<redacted>{newline}after=2", 1)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("value", ["'", '"', "'''", '"""', "(", "[", "{"])
@pytest.mark.parametrize("closed", [True, False])
def test_fallback_extends_through_paired_units_or_to_end(newline, value, closed):
    closing = {"(": ")", "[": "]", "{": "}"}.get(value, value)
    source = f"token {value}{newline}SYNTH"
    if closed:
        source += f"{newline}{closing}; also_covered=1{newline}public=2"
        expected, count = f"token<redacted>{newline}public=2", 3
    else:
        source += f"{newline}also_covered=1{newline}"
        expected, count = "token<redacted>", 3
    assert_redacted(source, expected, count)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("indicator", ["|", ">", "|2-", ">+2"])
def test_bare_yaml_indicator_does_not_enable_scalar_reader_in_fallback(newline, indicator):
    source = f"  token {indicator}{newline}    SYNTH{newline}{newline}    more{newline}  public: visible"
    expected = f"  token<redacted>{newline}    SYNTH{newline}{newline}    more{newline}  public: visible"
    assert_redacted(source, expected, 1)


@pytest.mark.parametrize("indicator", ["|", ">", "|2-", ">+2"])
def test_valid_yaml_scalars_remain_the_responsibility_of_units(indicator):
    source = f"  token: {indicator}\n    SYNTH\n\n    more\n  public: visible"
    expected = "  token: <redacted>\n  public: visible"
    assert _redact_units(source) == (expected, 1)
    assert_redacted(source, expected, 1)


@pytest.mark.parametrize("quote", ["'", '"'])
def test_prior_comment_quotes_cannot_hide_fallback_multiline_value(quote):
    prefix = f"# user{quote}s account\n"
    source = prefix + f"token {quote}\nSYNTH\n{quote}\nplain=1"
    assert_redacted(source, prefix + "token<redacted>\nplain=1", 3)


def test_closed_url_does_not_swallow_the_next_multiline_secret_key():
    source = 'connect("postgres://user:one@db")\nsecret "\nSYNTH\n"\nplain=1'
    assert_redacted(source, 'connect("postgres://user:<redacted>\nsecret<redacted>\nplain=1', 5)


def test_fallback_follows_triple_quote_opened_before_the_keyword():
    source = '\'secret"\'\'\'"""\npassword"""'
    assert_redacted(source, "'secret<redacted>", 2)


def test_marker_after_a_bare_value_does_not_bypass_fallback():
    assert_redacted("token SYNTH <redacted>", "token<redacted>", 1)


def test_sql_marker_is_a_redacted_value_rather_than_a_column_comparison():
    source = """cur.execute("CREATE ROLE r PASSWORD N'one' " "'SYNTH';")"""
    assert "SYNTH" in _redact_units(source)[0]
    assert_redacted(source, 'cur.execute("CREATE ROLE r PASSWORD<redacted>', 2)


@pytest.mark.parametrize(
    "source",
    [
        "SELECT password FROM users",
        "WHERE password IS NOT NULL",
        'SELECT "password" FROM t',
        "ORDER BY password",
        "password::text",
        "max(password)",
        "sql = 'ALTER ROLE r PASSWORD ' + pw",
        'password="SYNTH"; public=1',
        "# password: user's choice\npublic=1",
        'public="unclosed\nstill public\n( [ { :value @host',
    ],
)
def test_fallback_preserves_safe_unit_results_and_unrelated_lines(source):
    expected, count = _redact_units(source)
    assert_redacted(source, expected, count)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "kind",
    ["missed-values", "bare-values", "closed-nesting", "unclosed-nesting", "triple-quote", "yaml", "safe-markers"],
)
def test_twenty_thousand_fallback_lines_finish_quickly(newline, kind):
    if kind == "missed-values":
        source = f"password: :SYNTH{newline}" * 20000
        expected, count = f"password: <redacted>{newline}" * 20000, 20000
    elif kind == "bare-values":
        source = f'token "SYNTH"{newline}' * 20000
        expected, count = f"token<redacted>{newline}" * 20000, 20000
    elif kind == "closed-nesting":
        source = f"token ({newline}" * 19999 + ")" * 19999 + f"{newline}public=1"
        expected, count = f"token<redacted>{newline}" * 1000 + "public=1", 20000
    elif kind == "unclosed-nesting":
        source = f"token ({newline}" * 20000
        expected, count = f"token<redacted>{newline}" * 1000, 20000
    elif kind == "triple-quote":
        source = f'token """{newline}' + f"SYNTH{newline}" * 19999
        expected = f"token<redacted>{newline}" + f"SYNTH{newline}" * (20000 - _FALLBACK_MAX_LINES)
        count = _FALLBACK_MAX_LINES
    elif kind == "yaml":
        source = f"token: |{newline}" + f"  SYNTH{newline}" * 19999 + "public=1"
        expected, count = f"token: <redacted>{newline}public=1", 1
    else:
        source = f"token <redacted>{newline}" * 20000
        expected, count = source, 0
    best = float("inf")
    for _ in range(3):
        started = perf_counter()
        result = redact(source)
        best = min(best, perf_counter() - started)
    assert result == (expected, count)
    assert best < 0.5, f"{kind}: 20000 lines took {best:.6f}s"
    assert redact(expected) == (expected, 0)


def test_many_safe_keywords_on_one_line_do_not_rescan_the_same_suffix():
    source = "token <redacted> " * 20000
    best = float("inf")
    for _ in range(3):
        started = perf_counter()
        result = redact(source)
        best = min(best, perf_counter() - started)
    assert result == (source, 0)
    assert best < 0.5, f"20000 keywords took {best:.6f}s"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "line",
    [
        '"""累计 token 计入 Budget，到上限不再请求；"""',
        "def __init__(self, client, username: str, password: <redacted>):",
        'raise ValueError("token missing (" + name)',
    ],
    ids=["docstring-prose", "masked-annotation", "bracket-inside-string"],
)
def test_corpus_regressions_preserve_all_following_lines(newline, line):
    tail = newline + f'public = "user\'s choice"{newline}result = source.select("id")'
    source = line + tail
    for redactor in (_redact_lines, redact):
        cleaned, count = redactor(source)
        assert cleaned[cleaned.index(newline) :] == tail
        if "<redacted>" in line:
            assert (cleaned, count) == (source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_keyword_only_string_tail_with_comma_preserves_following_lines(newline):
    source = f'"offline-test-password",{newline}public = "user\'s choice"{newline}result = source.select("id")'
    expected = (
        f'"offline-test-password<redacted>{newline}public = "user\'s choice"{newline}result = source.select("id")'
    )
    for redactor in (_redact_lines, redact):
        assert redactor(source) == (expected, 1)


@pytest.mark.parametrize("suffix", [")", "]", "}", ",", ";", ")]},;"])
def test_closing_delimiters_after_a_masked_value_are_safe(suffix):
    source = f"token <redacted>{suffix}\npublic=1"
    assert _redact_lines(source) == (source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("opener", ['"', "'", '"""', "'''", "(", "[", "{"])
def test_fallback_stops_after_twenty_physical_lines_even_without_a_closer(newline, opener):
    source = f"token {opener}{newline}" + f"payload{newline}" * 24 + "public=1"
    expected = f"token<redacted>{newline}" + f"payload{newline}" * 5 + "public=1"
    assert _FALLBACK_MAX_LINES == 20
    assert _redact_lines(source) == (expected, 20)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_continuations_cannot_bypass_the_physical_line_limit(newline):
    source = f"token value \\{newline}" + f"payload \\{newline}" * 24 + "public=1"
    expected = f"token<redacted>\\{newline}" + f"payload \\{newline}" * 5 + "public=1"
    # 延续行在原有计数逻辑中算一个逻辑行；上限按原文物理行计算。
    assert _redact_lines(source) == (expected, 1)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_escaped_quotes_and_literal_brackets_do_not_extend_past_the_closing_line(newline):
    source = 'token "escaped \\" ( [ {' + newline + 'payload"' + newline + "public=1"
    assert _redact_lines(source) == ("token<redacted>" + newline + "public=1", 2)


def test_multiline_docstring_closing_quote_is_not_a_new_opener():
    source = '"""说明\n累计 token 计入 Budget；\n其余说明"""\npublic=1\n'
    cleaned, count = _redact_lines(source)
    assert cleaned == '"""说明\n累计 token<redacted>\npublic=1\n'
    assert count == 2


def test_masked_host_tails_cannot_start_another_multiline_value():
    source = 'items = [\'service-api-key = "<redacted>" literal\'"])\npublic=1'
    cleaned, count = _redact_lines(source)
    assert cleaned == "items = ['service-api-key = <redacted>\npublic=1"
    assert count == 1
