"""Credentials are redacted as syntax units, including malformed imported snippets."""

from time import perf_counter

import pytest

from avelis.importer.candidates import redact


def _timed_redact(source: str):
    """连测 3 次取最快一次：排除机器负载的偶发抖动，仍能抓住二次方级的回溯。"""
    best = None
    for _ in range(3):
        started = perf_counter()
        result = redact(source)
        elapsed = perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return result, best


def assert_redacted(source: str, expected: str, count: int = 1) -> None:
    assert redact(source) == (expected, count)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "value",
    [
        '(\n    "SYNTH"\n)',
        '[\n    "SYNTH"\n]',
        '{\n    "credential": "SYNTH"\n}',
        '({"nested": ["SYNTH", "fake ) ] }", "escaped\\" )"]})',
        "(['SYNTH', 'escaped\\' ] )', {'nested': (1, 2)}])",
        '(\n    """SYNTH\n    ) ] }\n    """\n)',
    ],
    ids=["parentheses", "list", "dict", "nested-double-quotes", "nested-single-quotes", "triple-quote"],
)
def test_bracketed_values_are_complete_syntax_units(newline: str, value: str) -> None:
    source = f"db_password = {value}; public = 1\nprint('public')".replace("\n", newline)
    expected = f"db_password = <redacted>; public = 1{newline}print('public')"
    assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "value",
    [
        '(\n    "SYNTH"',
        '[\n    {"nested": "SYNTH"}',
        '{\n    "nested": ["SYNTH"]',
        '(\n    "SYNTH )\npublic = 1',
    ],
    ids=["parentheses", "nested-list", "dict", "unclosed-inner-quote"],
)
def test_unclosed_bracketed_values_cover_the_remaining_input(newline: str, value: str) -> None:
    source = ("password = " + value).replace("\n", newline)
    assert_redacted(source, "password = <redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("value", "replacement"),
    [
        ('("abc"\n    "SYNTH")', "<redacted>"),
        ('"a" "b"', '"<redacted>"'),
        ("'a'\t'b'", "'<redacted>'"),
        ("\"a\" 'SYNTH'", '"<redacted>"'),
        ('"a" "b" "SYNTH"', '"<redacted>"'),
        ('"""a""" "SYNTH"', '"""<redacted>"""'),
        ("'''a''' 'SYNTH'", "'''<redacted>'''"),
        ("\"\"\"a\"\"\" '''SYNTH'''", '"""<redacted>"""'),
        ('r"a" r"SYNTH"', "<redacted>"),
        ('b"a" b"SYNTH"', "<redacted>"),
        ('u"a" u"SYNTH"', "<redacted>"),
        ('br"a" BR"SYNTH"', "<redacted>"),
        ('f"a" FR"SYNTH"', "<redacted>"),
        ('"a" r"SYNTH"', '"<redacted>"'),
        ("r'''first\nSYNTH'''", "<redacted>"),
        ("\"a\" b'''first\nSYNTH'''", '"<redacted>"'),
    ],
    ids=[
        "parenthesized",
        "spaces",
        "tabs",
        "mixed-quotes",
        "three-literals",
        "triple-double-first",
        "triple-single-first",
        "mixed-triple-literals",
        "raw-prefix",
        "bytes-prefix",
        "unicode-prefix",
        "mixed-case-bytes-raw",
        "mixed-case-formatted-raw",
        "quoted-then-prefixed",
        "raw-multiline-triple",
        "quoted-then-bytes-multiline-triple",
    ],
)
def test_adjacent_string_literals_are_one_value(newline: str, value: str, replacement: str) -> None:
    source = f"API_KEY = {value}; public = 1\nprint('public')".replace("\n", newline)
    expected = f"API_KEY = {replacement}; public = 1{newline}print('public')"
    assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("indicator", [">", "|", ">-", "|+", ">2", "|2", ">2-", ">-2", "|2+", "|+2"])
def test_yaml_block_scalars_cover_indented_lines_and_internal_blanks(newline: str, indicator: str) -> None:
    source = (
        f"  password: {indicator}\n    SYNTH\n\n      nested text\n    \n  public: visible\noutside: visible"
    ).replace("\n", newline)
    expected = f"  password: <redacted>{newline}  public: visible{newline}outside: visible"
    assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("password: >2- # scalar\n  SYNTH\npublic: visible", "password: <redacted>\npublic: visible"),
        ("password: |\n  SYNTH", "password: <redacted>"),
        ("password: >\npublic: visible", "password: <redacted>\npublic: visible"),
        ("  password: |+\n    SYNTH\npublic: visible", "  password: <redacted>\npublic: visible"),
        ('password: r"a" r"SYNTH"\npublic: visible', "password: <redacted>\npublic: visible"),
        ('password: "a" r"SYNTH"\npublic: visible', 'password: "<redacted>"\npublic: visible'),
    ],
    ids=[
        "header-comment",
        "end-of-input",
        "empty-block",
        "outer-indent-boundary",
        "prefixed-adjacent-value",
        "quoted-then-prefixed",
    ],
)
def test_yaml_scalar_header_and_dedent_boundaries(newline: str, source: str, expected: str) -> None:
    assert_redacted(source.replace("\n", newline), expected.replace("\n", newline))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("clause", "replacement"),
    [
        ("CREATE USER u IDENTIFIED BY 'SYNTH'", "CREATE USER u IDENTIFIED BY '<redacted>'"),
        ('ALTER USER u IDENTIFIED BY "SYNTH"', 'ALTER USER u IDENTIFIED BY "<redacted>"'),
        ("create user u identified by 'SYNTH'", "create user u identified by '<redacted>'"),
        (
            "CREATE USER u IDENTIFIED WITH plugin BY 'SYNTH'",
            "CREATE USER u IDENTIFIED WITH plugin BY '<redacted>'",
        ),
        (
            'ALTER USER u iDeNtIfIeD wItH plugin bY "SYNTH"',
            'ALTER USER u iDeNtIfIeD wItH plugin bY "<redacted>"',
        ),
        (
            "CREATE USER u IDENTIFIED WITH 'plugin' BY 'SYNTH'",
            "CREATE USER u IDENTIFIED WITH 'plugin' BY '<redacted>'",
        ),
        (
            'CREATE USER u IDENTIFIED WITH "plugin" BY "SYNTH"',
            'CREATE USER u IDENTIFIED WITH "plugin" BY "<redacted>"',
        ),
        (
            'CREATE USER u IDENTIFIED BY r"a" r"SYNTH"',
            "CREATE USER u IDENTIFIED BY <redacted>",
        ),
        ("CREATE ROLE u PASSWORD 'SYNTH'", "CREATE ROLE u PASSWORD '<redacted>'"),
        ("ALTER ROLE u WITH PASSWORD 'SYNTH'", "ALTER ROLE u WITH PASSWORD '<redacted>'"),
        ('alter role u with password "SYNTH"', 'alter role u with password "<redacted>"'),
        ("CREATE USER u IDENTIFIED BY SYNTH", "CREATE USER u IDENTIFIED BY <redacted>"),
        (
            "ALTER USER u IDENTIFIED BY VALUES 'SYNTH'",
            "ALTER USER u IDENTIFIED BY VALUES '<redacted>'",
        ),
    ],
    ids=[
        "identified-single",
        "identified-double",
        "identified-lowercase",
        "plugin-single",
        "plugin-mixed-case",
        "plugin-single-quoted",
        "plugin-double-quoted",
        "prefixed-adjacent-value",
        "postgres-create-role",
        "postgres-alter-role",
        "postgres-lowercase",
        "oracle-bare",
        "oracle-values",
    ],
)
def test_sql_password_clauses_read_the_following_value(newline: str, clause: str, replacement: str) -> None:
    assert_redacted(f"{clause};{newline}SELECT 1;", f"{replacement};{newline}SELECT 1;")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("# password: user's choice", "# password: <redacted>"),
        ("password = admin's choice", "password = <redacted>"),
        ("password = 123's choice", "password = <redacted>"),
        ("password = l'étudiant", "password = <redacted>"),
        ("password = foo'unclosed", "password = <redacted>"),
        ('password = foo"bar baz"; public = 1', "password = <redacted>; public = 1"),
        ("password = foo'bar baz'; public = 1", "password = <redacted>; public = 1"),
        ('password = foo"bar\nbaz"; public = 1', "password = <redacted>; public = 1"),
    ],
    ids=[
        "comment-apostrophe",
        "assignment-apostrophe",
        "numeric-apostrophe",
        "unicode-apostrophe",
        "unmatched-word-quote",
        "shell-double",
        "shell-single",
        "shell-multiline-double",
    ],
)
def test_apostrophes_preserve_following_code_and_shell_quotes_stay_complete(
    newline: str, source: str, expected: str
) -> None:
    source = source.replace("\n", newline) + f"{newline}public = 1{newline}print('public')"
    expected = expected.replace("\n", newline) + f"{newline}public = 1{newline}print('public')"
    assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "value",
    [
        '(\n    "SYNTH"\n); also_covered = 1',
        '[\n    {"nested": "SYNTH"}\n]; also_covered = 1',
        '"first\nsecond"; also_covered = 1',
        ">2-\n    SYNTH\n\n    more",
        "user's choice",
    ],
    ids=["parentheses", "nested-list", "multiline-quote", "yaml-scalar", "apostrophe"],
)
def test_pending_values_expand_through_the_complete_syntax_unit(newline: str, value: str) -> None:
    prefix = f"password ={newline} \t{newline}"
    source = prefix + f"  {value}\npublic = 1".replace("\n", newline)
    assert_redacted(source, prefix + f"<redacted>{newline}public = 1")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("kind", ["brackets", "yaml", "apostrophe", "repeated-apostrophes"])
def test_pathological_syntax_units_finish_quickly(newline: str, kind: str) -> None:
    if kind == "brackets":
        source = "password = " + "(" * 20000 + f'{newline}"SYNTH"{newline}' + ")" * 20000
        source += f"{newline}public = 1"
        expected = f"password = <redacted>{newline}public = 1"
    elif kind == "yaml":
        source = f"password: >{newline}" + f"  SYNTH{newline}" * 20000 + "public: visible"
        expected = f"password: <redacted>{newline}public: visible"
    elif kind == "apostrophe":
        source = "# password: user'" + "x" * 20000 + f" choice{newline}public = 1"
        expected = f"# password: <redacted>{newline}public = 1"
    else:
        source = "password=" + "a'a'" * 20000 + f"{newline}public = 1"
        expected = f"password=<redacted>{newline}public = 1"
    actual, elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert actual == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_repeated_malformed_sql_plugins_finish_quickly(newline: str) -> None:
    source = "IDENTIFIED WITH (" * 2000 + f"{newline}SELECT 1;"
    actual, elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{len(source)} chars took {elapsed:.6f}s"
    assert actual == (source, 0)
    assert redact(source) == (source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["password=", "TOKEN=", "--password=", "client --api-key="])
@pytest.mark.parametrize("value", ["foo'bar\nSYNTH'", 'abc"x\nSYNTH"', "foo-'bar\nSYNTH'", "foo'bar\nSYNTH'tail"])
def test_compact_assignments_mask_complete_multiline_shell_values(newline: str, prefix: str, value: str) -> None:
    source = f"{prefix}{value}; public = 1\necho next".replace("\n", newline)
    assert_redacted(source, f"{prefix}<redacted>; public = 1{newline}echo next")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["password=", "TOKEN=", "--password="])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_compact_assignments_with_unclosed_inner_quotes_mask_to_end(newline: str, prefix: str, quote: str) -> None:
    assert_redacted(f"{prefix}abc{quote}first{newline}SYNTH{newline}echo next", f"{prefix}<redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("flag", ["--password", "--api-key"])
@pytest.mark.parametrize("separator", [" ", "\t", "=", " = ", "\t=\t"])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("tail", ["", "suffix"])
def test_cli_inner_quotes_mask_complete_multiline_values(newline, flag, separator, quote, tail):
    prefix = f"client {flag}{separator}"
    source = f"{prefix}foo{quote}bar{newline}SYNTH-tail{quote}{tail}; echo public{newline}echo next"
    assert_redacted(source, f"{prefix}<redacted>; echo public{newline}echo next")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("flag", ["--password", "--api-key"])
@pytest.mark.parametrize("separator", [" ", "\t", "=", " = ", "\t=\t"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_cli_unclosed_inner_quotes_mask_to_end(newline, flag, separator, quote):
    prefix = f"client {flag}{separator}"
    assert_redacted(f"{prefix}foo{quote}bar{newline}SYNTH-tail; echo public", f"{prefix}<redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", [" ", "\t", "=", " = "])
@pytest.mark.parametrize("boundary", [" ", "\t", "\n", ";", "|", "&", ")", "(", "<", ">"])
def test_cli_shell_word_boundaries_keep_only_safe_following_lines(newline, separator, boundary):
    prefix = f"client --api-key{separator}"
    suffix = f'{boundary}"public"; echo public\necho next'.replace("\n", newline)
    source = f"{prefix}foo'bar{newline}SYNTH-tail'" + suffix
    if boundary == "\n":
        assert_redacted(source, f"{prefix}<redacted>" + suffix)
    elif boundary == "(":
        # 兜底遇到未闭合括号时继续遮蔽后续行。
        assert_redacted(source, f"{prefix}<redacted>", 3)
    else:
        # 第一层留下同一行的引号内容，兜底将该行剩余内容一并遮蔽。
        assert_redacted(source, f"{prefix}<redacted>{newline}echo next", 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("flag", ["--password", "--api-key"])
@pytest.mark.parametrize("space", ["", " ", "\t"])
def test_bare_cli_flags_do_not_consume_the_next_line(newline, flag, space):
    source = f"client {flag}{space}{newline}echo public"
    assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "value",
    [
        "foo'bar\nSYNTH-tail'",
        'foo"bar\nSYNTH-tail"',
        "foo'bar\\'tail",
        'foo"bar\\"\nSYNTH-tail"',
        "foo\\;bar'\nSYNTH-tail'",
        "foo,bar'\nSYNTH-tail'",
    ],
)
def test_cli_shell_quotes_and_escapes_preserve_later_commands(newline, value):
    source = f"client --password {value}; echo public\necho next".replace("\n", newline)
    assert_redacted(source, f"client --password <redacted>; echo public{newline}echo next")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("kind", ["repeated-quotes", "unclosed-quote", "repeated-values"])
def test_cli_shell_values_finish_quickly(newline, kind):
    if kind == "repeated-quotes":
        source = "client --password " + "a'a'" * 20000 + f"; echo public{newline}echo next"
        expected, count = f"client --password <redacted>; echo public{newline}echo next", 1
    elif kind == "unclosed-quote":
        source = "client --password foo'" + f"SYNTH{newline}" * 20000
        expected, count = "client --password <redacted>", 1
    else:
        source = f"client --password foo'first{newline}SYNTH'; echo public{newline}" * 2000
        expected = f"client --password <redacted>; echo public{newline}" * 2000
        count = 2000
    actual, elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert actual == (expected, count)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "prefix",
    ["# password: ", "password:", "password = ", "password= ", "password =", "password\t=\t"],
)
def test_prose_apostrophes_keep_following_code_with_non_shell_separators(newline: str, prefix: str) -> None:
    assert_redacted(f"{prefix}user's choice{newline}import os", f"{prefix}<redacted>{newline}import os")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "source",
    [
        "SELECT password FROM users;",
        "SELECT u.password, name FROM t WHERE password IS NOT NULL;",
        "SELECT password IS NULL FROM users;",
        "ORDER BY password",
        "GROUP BY password",
        "SELECT password > 1, password < 2, password != 3 FROM users;",
        "SELECT password_hash, u.password_hash FROM users;",
        'SELECT "password" FROM users;',
        "SELECT \"password\" 'public' FROM users;",
        "SELECT `password` 'public' FROM users;",
        "SELECT [password] 'public' FROM users;",
        "SELECT 'password' 'public' FROM users;",
        '"password" \n : \n "public"',
        "PASSWORD NULL;",
        "CREATE ROLE r PASSWORD NULL;",
        "ALTER USER u WITH UNENCRYPTED PASSWORD NULL;",
        "CREATE ROLE r; SELECT password FROM users;",
        "-- CREATE ROLE r\nSELECT password FROM users;",
        "SELECT PASSWORD <> other, PASSWORD IS NULL FROM t;",
        "PASSWORD ORDER BY name;",
        "PASSWORD GROUP BY name;",
        "SELECT password, name FROM users;",
        "SELECT password::text FROM users;",
        "ALTER ROLE r PASSWORD DISABLE;",
    ],
)
def test_sql_password_columns_and_null_are_preserved(newline: str, source: str) -> None:
    source = source.replace("\n", newline)
    assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_bare_secret_key_family_with_quoted_tail_triggers_line_fallback(newline: str) -> None:
    # 完整键名族包含 myPASSWORD；同一行的引号内容由整行兜底遮蔽。
    source = "myPASSWORD 'public'; PASSWORD_hash 'public';"
    assert_redacted(source.replace("\n", newline), "myPASSWORD<redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("PASSWORD\nSELECT 1;", "PASSWORD\n<redacted> 1;"),
        ("SELECT 1; PASSWORD SYNTH;", "SELECT 1; PASSWORD <redacted>;"),
        ("SELECT 'CREATE ROLE r;'; PASSWORD SYNTH;", "SELECT 'CREATE ROLE r;'; PASSWORD <redacted>;"),
        ("CREATE ROLE r PASSWORD SYNTH;", "CREATE ROLE r PASSWORD <redacted>;"),
        (
            "ALTER USER u WITH UNENCRYPTED PASSWORD SYNTH;",
            "ALTER USER u WITH UNENCRYPTED PASSWORD <redacted>;",
        ),
        (
            "CREATE\nROLE r WITH LOGIN ENCRYPTED PASSWORD SYNTH;",
            "CREATE\nROLE r WITH LOGIN ENCRYPTED PASSWORD <redacted>;",
        ),
        ('ALTER ROLE "name;quoted" WITH PASSWORD SYNTH;', 'ALTER ROLE "name;quoted" WITH PASSWORD <redacted>;'),
        ("/* heading; */ ALTER USER u PASSWORD SYNTH;", "/* heading; */ ALTER USER u PASSWORD <redacted>;"),
        ("PASSWORD $1tag$public$1tag$;", "PASSWORD <redacted>;"),
        ("PASSWORD $tag-name$public$tag-name$;", "PASSWORD <redacted>;"),
        ("PASSWORD Q'public';", "PASSWORD <redacted>;"),
    ],
)
def test_sql_password_unknown_units_are_redacted_by_default(newline: str, source: str, expected: str) -> None:
    # 旧期望只接受已列出的字面量；反向规则下，这些单元都不属于可保留的结构。
    assert_redacted(source.replace("\n", newline), expected.replace("\n", newline))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("host_quote", ["'", '"'])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("value", ["SYNTH", "first\nSYNTH", "", "first; ) SYNTH"])
@pytest.mark.parametrize("command", ["CREATE", "ALTER"])
def test_host_escaped_sql_password_quotes_are_complete_units(newline, host_quote, quote, value, command):
    prefix = f"cur.execute({host_quote}{command} ROLE r PASSWORD "
    suffix = f";{host_quote}); token=next\nSELECT 1;"
    source = prefix + "\\" + quote + value + "\\" + quote + suffix
    # 第一层遮住口令与 token；兜底只补遮当前行，保留下一条 SQL。
    expected = f"cur.execute({host_quote}{command} ROLE r PASSWORD<redacted>{newline}SELECT 1;"
    assert_redacted(source.replace("\n", newline), expected, 3)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
@pytest.mark.parametrize("suffix", [" + pw", ")", ", pw)", "", "\npublic = 1"])
def test_password_at_end_of_host_string_preserves_only_clear_concatenation(newline, quote, suffix):
    source = f"sql = {quote}ALTER ROLE r PASSWORD {quote}{suffix}".replace("\n", newline)
    if suffix == " + pw":
        assert_redacted(source, source, 0)
    else:
        expected = f"sql = {quote}ALTER ROLE r PASSWORD<redacted>"
        if suffix.startswith("\n"):
            expected += newline + "public = 1"
        assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("value", [" + ", ")", ",", ";", "", "SYNTH"])
def test_sql_literal_punctuation_is_not_a_host_string_end(newline, quote, value):
    # 与宿主引号不同的 SQL 引号，以及没有宿主字符串的 SQL，均按完整字面量遮蔽。
    host_quote = '"' if quote == "'" else "'"
    clause = f"ALTER ROLE r PASSWORD {quote}{value}{quote};"
    expected = f"ALTER ROLE r PASSWORD {quote}<redacted>{quote};"
    for wrapper in ("{}", f"cur.execute({host_quote}{{}}{host_quote})"):
        source = wrapper.format(clause) + newline
        if wrapper == "{}":
            assert_redacted(source, expected + newline)
        else:
            assert_redacted(source, f"cur.execute({host_quote}ALTER ROLE r PASSWORD<redacted>{newline}", 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    "word",
    "FROM WHERE AND OR NOT IS NULL AS ON IN LIKE ILIKE BETWEEN ORDER GROUP BY HAVING LIMIT OFFSET ASC DESC "
    "UNION JOIN LEFT RIGHT INNER OUTER WHEN THEN ELSE END SET VALUES INTO RETURNING DISABLE".split(),
)
def test_password_structure_keywords_are_preserved_after_whitespace(newline, word):
    for space in (" ", "\t", newline + " \t"):
        source = f"SELECT pAsSwOrD{space}{word.lower()}"
        assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("symbol", list(",);=<>!.+-*/|%:"))
def test_password_structure_symbols_are_preserved_after_whitespace(newline, symbol):
    for space in (" ", "\t", newline + " \t"):
        source = f"SELECT password{space}{symbol}"
        assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("space", ["", " ", "\t"])
def test_password_type_cast_is_not_an_assignment(newline, space):
    source = f"SELECT password{space}::text, password{space}::varchar FROM users;{newline}SELECT 1;"
    assert_redacted(source, source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("value", "replacement"),
    [
        ("SYNTH", "<redacted>"),
        ("from_secret", "<redacted>"),
        ("$PW", "<redacted>"),
        ("${PW}", "<redacted>"),
        ("%(pw)s", "<redacted>"),
        (":pw", ":<redacted>"),
        ("Q'SYNTH with space'", "<redacted>"),
        ('unknown"SYNTH with space"', "<redacted>"),
        (r"custom\'SYNTH with space\'", "<redacted>"),
        (r"custom\"SYNTH with space\"", "<redacted>"),
        (r"\'first\'\'SYNTH\'", "<redacted>"),
        (r"\"first\"\"SYNTH\"", "<redacted>"),
        (r"E\'first\\\'SYNTH\'", "<redacted>"),
    ],
)
def test_password_bare_values_variables_and_unknown_prefixes_are_redacted(newline, value, replacement):
    source = f"cur.execute('ALTER ROLE r PASSWORD {value};'){newline}SELECT 1;"
    # 首值已经遮蔽时仍检查行内剩余内容，后续 SQL 原样保留。
    if replacement == ":<redacted>":
        expected = "cur.execute('ALTER ROLE r PASSWORD :<redacted>"
    else:
        expected = "cur.execute('ALTER ROLE r PASSWORD<redacted>"
    assert_redacted(source, expected + newline + "SELECT 1;", 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_unclosed_host_escaped_password_literal_masks_to_end(newline, quote):
    source = f"cur.execute('ALTER ROLE r PASSWORD \\{quote}first{newline}SYNTH"
    assert_redacted(source, "cur.execute('ALTER ROLE r PASSWORD <redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_password_uses_word_boundaries_and_preserves_bare_cli_flag(newline):
    source = f"pre-PASSWORD SYNTH;{newline}client --password{newline}public = 1"
    expected = f"pre-PASSWORD <redacted>;{newline}client --password{newline}public = 1"
    assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("kind", ["escaped-literal", "bare-value", "host-end", "long-escaped-literal"])
def test_default_password_units_finish_quickly(newline, kind):
    if kind == "escaped-literal":
        source = f"cur.execute('CREATE ROLE r PASSWORD \\'SYNTH\\';'){newline}" * 2000
        expected, count = f"cur.execute('CREATE ROLE r PASSWORD<redacted>{newline}" * 2000, 4000
    elif kind == "bare-value":
        source = f"cur.execute('ALTER ROLE r PASSWORD SYNTH'){newline}" * 2000
        expected, count = f"cur.execute('ALTER ROLE r PASSWORD<redacted>{newline}" * 2000, 4000
    elif kind == "host-end":
        source = f"sql = 'ALTER ROLE r PASSWORD ' + pw{newline}" * 2000
        expected, count = source, 0
    else:
        source = "PASSWORD \\" + '"' + f"SYNTH{newline}" * 20000 + '\\"; SELECT 1;'
        expected, count = "PASSWORD <redacted>; SELECT 1;", 1
    actual, elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert actual == (expected, count)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("clause", "expected"),
    [
        ("CREATE ROLE r PASSWORD 'SYNTH'", "CREATE ROLE r PASSWORD '<redacted>'"),
        ("ALTER ROLE r WITH ENCRYPTED PASSWORD 'SYNTH'", "ALTER ROLE r WITH ENCRYPTED PASSWORD '<redacted>'"),
        ("ALTER USER u PASSWORD 'SYNTH'", "ALTER USER u PASSWORD '<redacted>'"),
        ("PASSWORD 'SYNTH'", "PASSWORD '<redacted>'"),
        ("PASSWORD'SYNTH'", "PASSWORD'<redacted>'"),
        ('PASSWORD "SYNTH"', 'PASSWORD "<redacted>"'),
        ('PASSWORD"SYNTH"', 'PASSWORD"<redacted>"'),
        ("PASSWORD E'SYNTH with space'", "PASSWORD <redacted>"),
        ("PASSWORD e'first\nSYNTH'", "PASSWORD <redacted>"),
        ("PASSWORD $$SYNTH with space$$", "PASSWORD <redacted>"),
        ("PASSWORD$$SYNTH with space$$", "PASSWORD<redacted>"),
        ("PASSWORD $$first\nSYNTH$$", "PASSWORD <redacted>"),
        ("PASSWORD\n  'SYNTH'", "PASSWORD\n  '<redacted>'"),
        # 双引号跟在独立 PASSWORD 后面时按字面量处理，不再判断列引用语境。
        ('SELECT u.password "display_name" FROM users', 'SELECT u.password "<redacted>" FROM users'),
        ('SELECT u. password "display_name" FROM users', 'SELECT u. password "<redacted>" FROM users'),
        (
            "CREATE ROLE \"reporting\"PASSWORD 'SYNTH'",
            "CREATE ROLE \"reporting\"PASSWORD '<redacted>'",
        ),
        ("UPDATE users SET password = 'SYNTH'", "UPDATE users SET password = '<redacted>'"),
        ("ALTER USER u IDENTIFIED BY SYNTH", "ALTER USER u IDENTIFIED BY <redacted>"),
        ("ALTER USER u IDENTIFIED WITH plugin BY SYNTH", "ALTER USER u IDENTIFIED WITH plugin BY <redacted>"),
    ],
)
def test_sql_password_credentials_remain_redacted(newline: str, clause: str, expected: str) -> None:
    source = f"{clause};\nSELECT password FROM users;".replace("\n", newline)
    expected = f"{expected};\nSELECT password FROM users;".replace("\n", newline)
    assert_redacted(source, expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_repeated_sql_password_columns_finish_quickly(newline: str) -> None:
    source = f"SELECT password FROM users;{newline}" * 2000
    actual, elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{len(source)} chars took {elapsed:.6f}s"
    assert actual == (source, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("wrapper", ["{}", 'psql -c "{}"', 'cur.execute("{}")'])
@pytest.mark.parametrize("keyword", ["PASSWORD", "pAsSwOrD"])
@pytest.mark.parametrize(
    ("literal", "replacement"),
    [
        ("'SYNTH'", "'<redacted>'"),
        ('"SYNTH"', '"<redacted>"'),
        ("E'first\\'SYNTH\nlast'", "<redacted>"),
        ("e'first\\\\'", "<redacted>"),
        ("U&'first\\0061SYNTH\nlast'", "<redacted>"),
        ("u&'SYNTH'", "<redacted>"),
        ("N'SYNTH'", "<redacted>"),
        ("n'SYNTH'", "<redacted>"),
        ("B'010101'", "<redacted>"),
        ("b'010101'", "<redacted>"),
        ("X'ABCD'", "<redacted>"),
        ("x'ABCD'", "<redacted>"),
        ("$$first\nSYNTH$$", "<redacted>"),
        ("$pw$SYNTH$pw$", "<redacted>"),
        ("$tag_1$first\nSYNTH$tag_1$", "<redacted>"),
        ("$_标签1$first$other$SYNTH$_标签1$", "<redacted>"),
        ("'first''SYNTH\nlast'", "'<redacted>'"),
        ('"first""SYNTH\nlast"', '"<redacted>"'),
        ("U&'first''SYNTH'", "<redacted>"),
        ("N'first''SYNTH'", "<redacted>"),
        ("'SYNTH\\'", "'<redacted>'"),
        ('"SYNTH\\"', '"<redacted>"'),
    ],
)
def test_sql_password_literals_are_redacted_inside_any_wrapper(
    newline: str, wrapper: str, keyword: str, literal: str, replacement: str
) -> None:
    source = wrapper.format(f"CREATE ROLE reporting {keyword} {literal};") + "\nSELECT 1;"
    expected = wrapper.format(f"CREATE ROLE reporting {keyword} {replacement};") + "\nSELECT 1;"
    if wrapper != "{}":
        # 剩余宿主语法只影响当前行，不能让下一条 SQL 消失。
        if wrapper == 'psql -c "{}"':
            expected = f'psql -c "CREATE ROLE reporting {keyword}<redacted>'
        else:
            expected = f'cur.execute("CREATE ROLE reporting {keyword}<redacted>'
        assert_redacted(source.replace("\n", newline), expected + newline + "SELECT 1;", 2)
    else:
        assert_redacted(source.replace("\n", newline), expected.replace("\n", newline))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("literal", ["'SYNTH'", "U&'SYNTH'", "N'SYNTH'", "$$SYNTH$$", "$tag$SYNTH$tag$"])
@pytest.mark.parametrize("space", ["", " \t", "\n  "])
def test_sql_password_literal_boundaries_preserve_surrounding_text(newline: str, literal: str, space: str) -> None:
    # 前一个角色名的闭合引号不应把 PASSWORD 误判成引号标识符。
    prefix = f'cur.execute("ALTER ROLE "reporting"PASSWORD{space}'
    replacement = "'<redacted>'" if literal.startswith("'") else "<redacted>"
    source = prefix + literal + '); PUBLIC;"  # visible\nSELECT 1;'
    expected = prefix + replacement + '); PUBLIC;"  # visible\nSELECT 1;'
    if not space and literal.startswith(("U", "N")):
        # PASSWORDU / PASSWORDN 没有词边界，不是独立关键字。
        assert_redacted(source.replace("\n", newline), source.replace("\n", newline), 0)
        return
    if space != "\n  ":
        assert_redacted(
            source.replace("\n", newline),
            'cur.execute("ALTER ROLE "reporting"PASSWORD<redacted>' + newline + "SELECT 1;",
            2,
        )
        return
    assert_redacted(source.replace("\n", newline), expected.replace("\n", newline))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize(
    ("literal", "replacement"),
    [
        ("'first\nSYNTH", "'<redacted>'"),
        ('"first\nSYNTH', '"<redacted>"'),
        ("E'first\\'\nSYNTH", "<redacted>"),
        ("U&'first\nSYNTH", "<redacted>"),
        ("N'first\nSYNTH", "<redacted>"),
        ("B'0101\n0101", "<redacted>"),
        ("X'ABCD\nABCD", "<redacted>"),
        ("$$first\nSYNTH", "<redacted>"),
        ("$pw$first\nSYNTH", "<redacted>"),
        ("$Tag$SYNTH$tag$", "<redacted>"),
    ],
)
def test_unclosed_sql_password_literals_cover_to_end(newline: str, literal: str, replacement: str) -> None:
    source = 'cur.execute("ALTER ROLE r PASSWORD ' + literal + "\nremaining input"
    expected = 'cur.execute("ALTER ROLE r PASSWORD ' + replacement
    assert_redacted(source.replace("\n", newline), expected)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_sql_literal_closing_resumes_redaction_on_the_same_line(newline: str) -> None:
    source = "PASSWORD E'first\\'SYNTH'; PASSWORD $pw$second$pw$; token=third;\nSELECT 1;"
    expected = "PASSWORD <redacted>; PASSWORD <redacted>; token=<redacted>;\nSELECT 1;"
    assert_redacted(source.replace("\n", newline), expected.replace("\n", newline), 3)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("kind", ["doubled-quotes", "dollar-body", "long-tag", "repeated-literals"])
def test_sql_password_literals_finish_quickly(newline: str, kind: str) -> None:
    if kind == "doubled-quotes":
        source = "PASSWORD '" + "''" * 20000 + "SYNTH'"
        expected, count = "PASSWORD '<redacted>'", 1
    elif kind == "dollar-body":
        source = "PASSWORD $pw$" + (f"$other$SYNTH{newline}" * 20000) + "$pw$"
        expected, count = "PASSWORD <redacted>", 1
    elif kind == "long-tag":
        delimiter = "$" + "t" * 20000 + "$"
        source = "PASSWORD " + delimiter + "SYNTH" + delimiter
        expected, count = "PASSWORD <redacted>", 1
    else:
        source = f'cur.execute("ALTER ROLE r PASSWORD $pw$SYNTH$pw$"){newline}' * 2000
        expected, count = f'cur.execute("ALTER ROLE r PASSWORD<redacted>{newline}' * 2000, 4000
    actual, elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert actual == (expected, count)
    assert redact(expected) == (expected, 0)
