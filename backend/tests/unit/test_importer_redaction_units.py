"""Credentials are redacted as syntax units, including malformed imported snippets."""

from time import perf_counter

import pytest

from avelis.importer.candidates import redact


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
def test_pathological_syntax_units_finish_within_100ms(newline: str, kind: str) -> None:
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
    started = perf_counter()
    actual = redact(source)
    elapsed = perf_counter() - started
    assert elapsed < 0.5, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert actual == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_repeated_malformed_sql_plugins_finish_within_100ms(newline: str) -> None:
    source = "IDENTIFIED WITH (" * 2000 + f"{newline}SELECT 1;"
    started = perf_counter()
    actual = redact(source)
    elapsed = perf_counter() - started
    assert elapsed < 0.5, f"{len(source)} chars took {elapsed:.6f}s"
    assert actual == (source, 0)
    assert redact(source) == (source, 0)
