"""代码外发脱敏和 Python SQL 抽取的顺序、作用域及控制流回归。"""

from textwrap import dedent

import pytest

from avelis.importer.candidates import redact
from avelis.importer.extract import extract_python


@pytest.mark.parametrize("source,secrets,count", [
    ('{"password": "json secret", "api_key": "second-secret"}', ["json secret", "second-secret"], 2),
    ("{'token': 'token-secret'}", ["token-secret"], 1),
    ('password = "space secret"', ["space secret"], 1),
    ("PASSWORD=environment-secret", ["environment-secret"], 1),
    ('"password" \n : \n "multiline-secret"', ["multiline-secret"], 1),
    ("token = '''first-secret\nsecond-secret'''", ["first-secret", "second-secret"], 1),
    ('password = "escaped\\\"secret"', ["escaped", "secret"], 1),
    ('jdbc:mysql://etl_user:connection-secret@host/db', ["connection-secret"], 1),
    ('user:bare-secret@host', ["bare-secret"], 1),
    ('password="postgres://user:nested-secret@host/db"', ["nested-secret"], 1),
    ('token="sk-1234567890abcdefghijklmnop"', ["sk-1234567890abcdefghijklmnop"], 1),
    ('key = "sk-1234567890abcdefghijklmnop"; AKIA1234567890ABCDEF',
     ["sk-1234567890abcdefghijklmnop", "AKIA1234567890ABCDEF"], 2),
])
def test_redact_common_secret_forms_once(source, secrets, count):
    cleaned, hits = redact(source)
    assert hits == count
    assert cleaned.count("<redacted>") == count
    assert all(secret not in cleaned for secret in secrets)


@pytest.mark.parametrize("key", ["password", "api-key", "service-api-key"])
@pytest.mark.parametrize("separator", ["=", ":", " ", "\t", " \t ", " \\\n  ", " \\\r\n\t"])
@pytest.mark.parametrize("quote", ["", "'", '\"'])
def test_redact_cli_secret_values_and_preserve_flags(key, separator, quote):
    source = f'client --{key}{separator}{quote}synthetic-only-secret{quote} --mode=public'
    expected = f'client --{key}{separator}{quote}<redacted>{quote} --mode=public'
    assert redact(source) == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("trailing_space", ["", " \t"])
@pytest.mark.parametrize("assignment", ['password = "actual secret"', '"password": "actual secret"'])
def test_redact_cli_flag_does_not_consume_next_line_assignment(newline, trailing_space, assignment):
    source = f"# run with --password{trailing_space}{newline}{assignment}{newline}"
    expected = source.replace("actual secret", "<redacted>")
    assert redact(source) == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["# run with --password", "password"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("spacing", ["", " \t"])
@pytest.mark.parametrize("assignment", [
    'password = "synthetic-only"',
    '\t"password": "synthetic-only"',
    'service-api-key = "synthetic-only"',
    '--auth-token "synthetic-only"',
])
def test_redact_separator_at_line_end_does_not_consume_next_assignment(
    newline, prefix, separator, spacing, assignment,
):
    source = f"{prefix}{spacing}{separator}{spacing}{newline}{assignment}"
    expected = source.replace("synthetic-only", "<redacted>")
    assert redact(source) == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("prefix", ["client --password", "password"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("value", ['"s3cret"', "s3cret", "'password literal'"])
def test_redact_value_on_next_line(newline, prefix, separator, value):
    source = f"{prefix} \t{separator} \t{newline}    {value}"
    expected = source.replace(value.strip("\"'"), "<redacted>")
    assert redact(source) == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("position", ["before", "after", "both"])
def test_redact_cli_separator_with_explicit_shell_continuation(newline, separator, position):
    continuation = f" \\{newline}\t"
    before = continuation if position in ("before", "both") else " \t"
    after = continuation if position in ("after", "both") else " \t"
    source = f'client --password{before}{separator}{after}"synthetic-only" --mode=public'
    expected = source.replace("synthetic-only", "<redacted>")
    assert redact(source) == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("key", ["--password-policy", "--auth-token-expiry", "public--password-policy"])
def test_redact_preserves_cli_names_without_a_sensitive_ending(key):
    source = f'client {key}=public'
    assert redact(source) == (source, 0)


@pytest.mark.parametrize("source,expected", [
    ('DB_PASSWORD = "x"', 'DB_PASSWORD = "<redacted>"'),
    ("mysql_pwd: 'x'", "mysql_pwd: '<redacted>'"),
    ('client_secret="x"', 'client_secret="<redacted>"'),
    ('auth_token = x', 'auth_token = <redacted>'),
    ('SECRET_KEY = "x"', 'SECRET_KEY = "<redacted>"'),
    ('private_key = "x"', 'private_key = "<redacted>"'),
    ('{"db_password": "x"}', '{"db_password": "<redacted>"}'),
])
def test_redact_prefixed_secret_assignments_and_json_keys(source, expected):
    assert redact(source) == (expected, 1)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("key", [
    "db_password", "mysql_passwd", "mysql_pwd", "client_secret", "auth_token",
    "service_api_key", "service_apikey", "service_access_key", "service_secret_key",
    "service_private_key", "db-password", "service-api-key", "service-access-key",
    "service-secret-key", "service-private-key", "serviceAPIKEY", "serviceAcCeSsKeY",
    "_TOKEN", "\u00fcber_token",
])
def test_redact_identifier_ending_in_sensitive_word(key):
    assert redact(f'{key} = "value"') == (f'{key} = "<redacted>"', 1)


@pytest.mark.parametrize("key", [
    "password_policy", "db_password_hash", "auth_token_expiry", "private_key_path", "api_key_name",
])
def test_redact_preserves_identifiers_with_non_sensitive_suffix(key):
    source = f'{key} = "public"'
    assert redact(source) == (source, 0)


def test_redaction_count_is_idempotent_and_preserves_other_code():
    source = 'password = "first secret"\nconfig = {"token": "second-secret"}\nurl = "u:third-secret@host"'
    cleaned, count = redact(source)
    assert count == 3
    assert cleaned == 'password = "<redacted>"\nconfig = {"token": "<redacted>"}\nurl = "u:<redacted>@host"'
    assert redact(cleaned) == (cleaned, 0)
    assert redact('password_policy = "public"\nspark.sql("SELECT 1")')[1] == 0


def extract(source):
    return extract_python(dedent(source).strip())


def test_execute_uses_assignment_before_each_call():
    segments, notes = extract('''
        sql = "INSERT INTO ads.first SELECT id FROM shop.first"
        cursor.execute(sql)
        sql = "INSERT INTO ads.second SELECT id FROM shop.second"
        cursor.execute(sql)
    ''')
    assert not notes
    assert [(seg.line_start, seg.method, seg.text) for seg in segments] == [
        (2, "python_ast", "INSERT INTO ads.first SELECT id FROM shop.first"),
        (4, "python_ast", "INSERT INTO ads.second SELECT id FROM shop.second"),
    ]


def test_future_assignment_does_not_resolve_earlier_call():
    segments, notes = extract('''
        cursor.execute(sql)
        sql = "SELECT id FROM future_table"
    ''')
    assert [(seg.method, seg.text) for seg in segments] == [("unrecognized", "cursor.execute(sql)")]
    assert len(notes) == 1 and "无法确定" in notes[0]


def test_function_bindings_do_not_leak_and_parameters_shadow_outer_values():
    segments, notes = extract('''
        sql = "SELECT id FROM module_table"
        def first():
            sql = "SELECT id FROM first_table"
            cursor.execute(sql)
        def second():
            cursor.execute(sql)
            sql = "SELECT id FROM second_table"
            cursor.execute(sql)
        def third(sql):
            cursor.execute(sql)
        cursor.execute(sql)
    ''')
    assert [(seg.method, seg.text) for seg in segments] == [
        ("python_ast", "SELECT id FROM first_table"),
        ("unrecognized", "    cursor.execute(sql)"),
        ("python_ast", "SELECT id FROM second_table"),
        ("unrecognized", "    cursor.execute(sql)"),
        ("python_ast", "SELECT id FROM module_table"),
    ]
    assert len(notes) == 2


@pytest.mark.parametrize("control", [
    'if flag:\n    sql = "SELECT id FROM first_table"\nelse:\n    sql = "SELECT id FROM second_table"',
    'if flag:\n    sql = "SELECT id FROM first_table"',
    'try:\n    sql = "SELECT id FROM first_table"\nexcept Exception:\n    sql = "SELECT id FROM second_table"',
    'for item in items:\n    sql = "SELECT id FROM first_table"',
    'while flag:\n    sql = "SELECT id FROM first_table"',
    ('for item in items:\n    sql = "SELECT id FROM first_table"\n'
     '    if flag:\n        break\n    sql = "SELECT id FROM initial_table"'),
])
def test_uncertain_control_flow_goes_to_model_candidates(control):
    source = 'sql = "SELECT id FROM initial_table"\n' + control + '\ncursor.execute(sql)'
    segments, notes = extract_python(source)
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert segments[0].text == "cursor.execute(sql)"
    assert len(notes) == 1 and "模型候选" in notes[0]


def test_equal_branches_and_later_assignment_are_deterministic():
    segments, notes = extract('''
        if flag:
            sql = "SELECT id FROM same_table"
        else:
            sql = "SELECT id FROM same_table"
        cursor.execute(sql)
        if flag:
            sql = "SELECT id FROM other_table"
        sql = "SELECT id FROM final_table"
        cursor.execute(sql)
    ''')
    assert not notes
    assert [seg.text for seg in segments] == ["SELECT id FROM same_table", "SELECT id FROM final_table"]
    assert all(seg.method == "python_ast" for seg in segments)


def test_calls_inside_branches_use_their_own_prior_bindings():
    segments, notes = extract('''
        if flag:
            sql = "SELECT id FROM first_table"
            cursor.execute(sql)
        else:
            sql = "SELECT id FROM second_table"
            cursor.execute(sql)
    ''')
    assert not notes
    assert [seg.text for seg in segments] == ["SELECT id FROM first_table", "SELECT id FROM second_table"]


def test_loop_call_before_reassignment_cannot_assume_first_iteration():
    segments, notes = extract('''
        sql = "SELECT id FROM initial_table"
        for item in items:
            cursor.execute(sql)
            sql = "SELECT id FROM next_table"
    ''')
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert len(notes) == 1


def test_try_handler_and_finally_do_not_reuse_pre_try_binding():
    segments, notes = extract('''
        sql = "SELECT id FROM initial_table"
        try:
            sql = "SELECT id FROM next_table"
            may_fail()
        except Exception:
            cursor.execute(sql)
        finally:
            cursor.execute(sql)
    ''')
    assert len(segments) == 2 and all(seg.method == "unrecognized" for seg in segments)
    assert len(notes) == 2


def test_overwrite_with_non_string_invalidates_previous_binding():
    segments, notes = extract('''
        sql: str = "SELECT id FROM initial_table"
        sql = make_sql()
        cursor.execute(sql)
        sql = "SELECT id "
        sql += "FROM final_table"
        cursor.execute(sql)
    ''')
    assert [seg.method for seg in segments] == ["unrecognized", "python_ast"]
    assert segments[1].text == "SELECT id FROM final_table"
    assert len(notes) == 1


def test_dynamic_parameter_is_preserved_but_ambiguous_binding_is_not():
    segments, notes = extract('''
        def snapshot(dt):
            spark.sql(f"SELECT id FROM shop.orders WHERE dt = '{dt}'")
        if flag:
            table = "first_table"
        else:
            table = "second_table"
        spark.sql(f"SELECT id FROM {table}")
    ''')
    assert segments[0].method == "python_ast" and segments[0].placeholders == ["dt"]
    assert segments[1].method == "unrecognized"
    assert len(notes) == 1


@pytest.mark.parametrize("argument", [
    'f"SELECT id FROM {alias}"',
    '"SELECT id FROM {table}".format(table=alias)',
    '"SELECT id FROM {}".format(alias)',
])
def test_ambiguous_alias_and_format_arguments_stay_unknown(argument):
    source = ('if flag:\n    table = "first_table"\nelse:\n    table = "second_table"\n'
              'alias = table\ncursor.execute(' + argument + ')')
    segments, notes = extract_python(source)
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert len(notes) == 1


@pytest.mark.parametrize("declaration", ["", "global sql\n    "])
def test_function_free_variable_does_not_use_definition_time_value(declaration):
    source = ('sql = "SELECT id FROM first_table"\ndef run():\n    ' + declaration
              + 'cursor.execute(sql)\nsql = "SELECT id FROM second_table"\nrun()')
    segments, notes = extract_python(source)
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert len(notes) == 1


def test_closure_nonlocal_variable_is_not_frozen_at_definition():
    segments, notes = extract('''
        def outer():
            sql = "SELECT id FROM first_table"
            def inner():
                nonlocal sql
                cursor.execute(sql)
            sql = "SELECT id FROM second_table"
            inner()
    ''')
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert len(notes) == 1


@pytest.mark.parametrize("conditional", [
    'flag and (sql := "SELECT id FROM second_table")',
    '(sql := "SELECT id FROM second_table") if flag else None',
])
def test_conditional_expression_assignment_remains_uncertain(conditional):
    source = 'sql = "SELECT id FROM first_table"\n' + conditional + '\ncursor.execute(sql)'
    segments, notes = extract_python(source)
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert len(notes) == 1


def test_dataframe_blocks_still_reach_model_candidates():
    segments, notes = extract('''
        def rollup():
            jdbc_url = "jdbc:mysql://etl_user:dummy-secret@host:3306/shop"
            rows = spark.table("shop.orders")
            rows.write.saveAsTable("ads.orders")
    ''')
    assert not notes
    assert len(segments) == 1 and segments[0].method == "unrecognized"
    assert 'spark.table("shop.orders")' in segments[0].text
    cleaned, count = redact(segments[0].text)
    assert "dummy-secret" not in cleaned and count == 1
