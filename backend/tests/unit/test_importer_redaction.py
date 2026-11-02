"""逻辑行脱敏：跨行泄露、计数、幂等与病态输入的回归。"""

from time import perf_counter

import pytest

from avelis.importer.candidates import SECRET_PATTERN, redact


def _timed_redact(source: str):
    """连测 3 次取最快一次：排除机器负载的偶发抖动，仍能抓住二次方级的回溯。"""
    best = None
    for _ in range(3):
        started = perf_counter()
        result = redact(source)
        elapsed = perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return result, best


def assert_redacted(source, expected, count=1):
    assert redact(source) == (expected, count)
    assert redact(expected) == (expected, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("flag", ["--password", "--api-key"])
@pytest.mark.parametrize("key", ["self.password", "config.api_key", 'obj["password"]', "public_setting"])
def test_empty_cli_value_masks_next_assignment_regardless_of_key(newline, separator, flag, key):
    prefix = f"# client {flag}{separator}{newline}"
    source = prefix + f'{key} = "prod credential"{newline}print("public")'
    assert_redacted(source, prefix + f'<redacted>{newline}print("public")')


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("key", ["self.password", "config.api_key", 'obj["password"]', "obj['password']"])
def test_prefixed_assignments_with_inline_and_next_line_values(newline, separator, key):
    assert_redacted(f'{key} {separator} "s3cret"', f'{key} {separator} "<redacted>"')
    assert_redacted(f'{key} {separator}{newline}    "s3cret"', f"{key} {separator}{newline}<redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("key", ["password", "self.password", "config.api_key", 'obj["password"]'])
def test_assignment_continuations_keep_prefix_format(newline, separator, key):
    continuation = f" \\{newline}\t"
    source = f'{key}{continuation}{separator}{continuation}"s3cret"; public = 1'
    assert_redacted(source, source.replace("s3cret", "<redacted>"))


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
def test_pending_masks_entire_continued_logical_line(newline, separator):
    prefix = f"password {separator}{newline}"
    source = prefix + f'  "first" \\{newline}  "second"{newline}public = 1'
    assert_redacted(source, prefix + f"<redacted>{newline}public = 1")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ['"""', "'''"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("key", ["password", "client --api-key", 'obj["password"]'])
def test_multiline_triple_values_are_one_redaction(newline, quote, separator, key):
    source = f'{key}{separator}{quote}first{newline}password = "nested"{newline}last{quote}; token="next"'
    expected = f'{key}{separator}{quote}<redacted>{quote}; token="<redacted>"'
    assert_redacted(source, expected, 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ['"""', "'''"])
def test_pending_line_and_unclosed_triple_are_conservatively_masked(newline, quote):
    prefix = f"password ={newline}"
    source = prefix + f'obj["password"] = {quote}first{newline}last{quote}{newline}public = 1'
    assert_redacted(source, prefix + f"<redacted>{newline}public = 1")
    source = f"password = {quote}first{newline}last"
    assert_redacted(source, f"password = {quote}<redacted>{quote}")
    source = prefix + f'{quote}first{newline}last{quote}; public = "also covered"{newline}print(1)'
    assert_redacted(source, prefix + f"<redacted>{newline}print(1)")
    source = prefix + f"{quote}first{newline}last{quote}; other = {quote}third{newline}last{quote}{newline}print(1)"
    assert_redacted(source, prefix + f"<redacted>{newline}print(1)")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_ordinary_newlines_are_hard_inline_boundaries(newline):
    source = f'password{newline}= "public"{newline}client --password{newline}public = 1'
    assert_redacted(source, source, 0)
    source = f"user:credential{newline}@host"
    assert_redacted(source, source, 0)
    assert r"\s" not in SECRET_PATTERN.pattern


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("prefix", ["password=", "PRIVATE_KEY=", "client --password "])
def test_quoted_values_continue_across_ordinary_newlines(newline, quote, prefix):
    source = f"{prefix}{quote}first{newline}second{quote}{newline}public = 1"
    assert_redacted(source, f"{prefix}{quote}<redacted>{quote}{newline}public = 1")
    source = f"{prefix}{quote}header{newline}key-material{newline}footer{quote}; public = 1"
    assert_redacted(source, f"{prefix}{quote}<redacted>{quote}; public = 1")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_unclosed_ordinary_quote_masks_to_end_of_text(newline, quote):
    # 原来要求在换行处截断；现在未闭合的值必须覆盖余下全文。
    source = f"password = {quote}first{newline}public = 1"
    assert_redacted(source, f"password = {quote}<redacted>{quote}")
    source = f"password={quote}first{newline}second\\{quote}"
    assert_redacted(source, f"password={quote}<redacted>{quote}")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_multiline_quotes_respect_escaping_and_resume_on_closing_line(newline, quote):
    source = f"password={quote}a\\{quote}b{newline}c{quote}; token='next'; public = 1"
    expected = f"password={quote}<redacted>{quote}; token='<redacted>'; public = 1"
    assert_redacted(source, expected, 2)
    # 偶数个反斜杠不转义闭合引号；闭合行后面仍能找到其他密钥。
    source = f"password={quote}first{newline}last\\\\{quote}; token=next"
    assert_redacted(source, f"password={quote}<redacted>{quote}; token=<redacted>", 2)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiline_single_quote_can_contain_double_quotes(newline):
    source = f"password='first\"{newline}\"second'; public = 1"
    assert_redacted(source, "password='<redacted>'; public = 1")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_multiple_quoted_values_on_the_same_line(newline):
    source = f"password='one'; token=\"two{newline}three\"; api_key='four'; public = 1"
    expected = "password='<redacted>'; token=\"<redacted>\"; api_key='<redacted>'; public = 1"
    assert_redacted(source, expected, 3)
    assert_redacted("password=\"one\"'two'; public = 1", 'password="<redacted>"; public = 1')


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("blank", ["", " ", "\t", " \t "])
@pytest.mark.parametrize("prefix", ['{"password":', "password=", "client --password="])
def test_pending_redaction_preserves_blank_lines_until_first_nonblank(newline, blank, prefix):
    prefix += newline + (blank + newline) * 3
    source = prefix + f'    "synthetic-secret"{newline}public = 1'
    assert_redacted(source, prefix + f"<redacted>{newline}public = 1")
    assert_redacted(prefix + blank, prefix + blank, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
def test_pending_multiline_quoted_value_after_blank_lines(newline, quote):
    prefix = f'"password":{newline} \t{newline}{newline}'
    source = prefix + f"  {quote}first{newline}second{quote}; public = 1{newline}print(1)"
    assert_redacted(source, prefix + f"<redacted>{newline}print(1)")
    source = prefix + f"  {quote}first{newline}second"
    assert_redacted(source, prefix + "<redacted>")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_empty_cli_value_extends_but_populated_assignments_keep_line_boundaries(newline):
    source = f"client --password \t{newline} \t{newline}public = 1{newline}password_policy = 2"
    assert_redacted(source, f"client --password \t<redacted>{newline}password_policy = 2", 3)
    source = f'password=first{newline}public = "second"{newline}token=third'
    assert_redacted(source, f'password=<redacted>{newline}public = "second"{newline}token=<redacted>', 2)


def test_url_and_standalone_keys_count_syntax_redactions_and_line_fallback():
    source = (
        'jdbc:mysql://user:one@host/db /user:two@host "user:three@host" '
        "AKIA1234567890ABCDEF sk-1234567890abcdefghijklmnop"
    )
    # 第一层命中五次；剩余 URL 的 @ 触发兜底，再按整行计一次。
    assert_redacted(source, "jdbc:<redacted>", 6)


@pytest.mark.parametrize("prefix", ["", ":", "::", "/", "//", "text: ", "=", "("])
def test_url_credentials_keep_prefix_and_mask_the_remaining_line(prefix):
    assert_redacted(prefix + "user:credential@host", prefix + "user:<redacted>", 2)


def test_secret_named_url_user_is_idempotent():
    assert_redacted("password:#credential@host", "password:<redacted>", 2)


@pytest.mark.parametrize(
    "value",
    [
        'foo"bar baz"',
        "foo'bar baz'",
        "word'''password:#credential@host'''",
        "literal#credential",
    ],
)
def test_concatenated_quoted_fragments_are_one_inline_value(value):
    assert_redacted(f"password={value}; public = 1", "password=<redacted>; public = 1")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize(
    "kind",
    [
        "reported",
        "spaces",
        "newlines",
        "continuations",
        "quoted",
        "triple",
        "identifier",
        "url-like",
        "split-key",
        "empty",
        "around-separator",
        "unclosed-quotes",
        "pending-blank-lines",
    ],
)
def test_pathological_inputs_finish_quickly(newline, separator, kind):
    prefix = f"# run with --password{separator}"
    if kind == "reported":
        source = prefix + " " * 12000 + newline + 'password = "secret"'
        expected = prefix + " " * 12000 + newline + "<redacted>"
    elif kind == "spaces":
        source = prefix + " " * 20000 + newline + 'self.password = "secret"'
        expected = prefix + " " * 20000 + newline + "<redacted>"
    elif kind == "newlines":
        source = newline * (20000 // len(newline)) + 'password="secret"'
        expected = source.replace("secret", "<redacted>")
    elif kind == "continuations":
        source = prefix + (f" \\{newline}" * (20000 // (2 + len(newline)))) + ' "secret"'
        expected = source.replace("secret", "<redacted>")
    elif kind == "quoted":
        source = 'password="' + "x" * 20000
        expected = 'password="<redacted>"'
    elif kind == "triple":
        source = 'password="""' + (f"x{newline}" * (20000 // (1 + len(newline))))
        expected = 'password="""<redacted>"""'
    elif kind == "split-key":
        source = "password" + newline * (20000 // len(newline)) + separator + '"public"'
        expected = source
    elif kind == "empty":
        source = prefix + " " * 20000
        expected = source
    elif kind == "around-separator":
        prefix = "client --password" + " " * 10000 + separator + " " * 10000
        source = prefix + newline + 'config.api_key = "secret"'
        expected = prefix + newline + "<redacted>"
    elif kind == "unclosed-quotes":
        source = 'password="' + (f"'{newline}" * 20000)
        expected = 'password="<redacted>"'
    elif kind == "pending-blank-lines":
        prefix += newline + (f" \t{newline}" * 20000)
        source = prefix + '"secret"' + newline + "public = 1"
        expected = prefix + "<redacted>" + newline + "public = 1"
    else:
        source = "x" * 20000 if kind == "identifier" else "u:" * 10000
        expected = source
    (cleaned, count), elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert cleaned == expected
    assert count == (0 if kind in ("identifier", "url-like", "split-key", "empty") else 1)
    assert redact(cleaned) == (cleaned, 0)


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_twenty_thousand_open_quote_lines_finish_quickly(newline):
    source = f'password="first{newline}' * 20000
    # 每隔一行才遇到对应闭合引号；已扫描的部分不能从每行重新扫描。
    expected = f'password="<redacted>"{newline}' * 10000
    (cleaned, count), elapsed = _timed_redact(source)
    assert elapsed < 0.5, f"{len(source)} chars took {elapsed:.6f}s"
    assert (cleaned, count) == (expected, 10000)
    assert redact(cleaned) == (cleaned, 0)
