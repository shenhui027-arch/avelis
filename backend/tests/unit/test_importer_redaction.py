"""逻辑行脱敏：跨行泄露、计数、幂等与病态输入的回归。"""

from time import perf_counter

import pytest

from avelis.importer.candidates import SECRET_PATTERN, redact


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
    assert_redacted(f'{key} {separator}{newline}    "s3cret"', f'{key} {separator}{newline}<redacted>')


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
    source = prefix + f'{quote}first{newline}last{quote}; other = {quote}third{newline}last{quote}{newline}print(1)'
    assert_redacted(source, prefix + f"<redacted>{newline}print(1)")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_ordinary_newlines_are_hard_inline_boundaries(newline):
    source = f'password{newline}= "public"{newline}client --password{newline}public = 1'
    assert_redacted(source, source, 0)
    # 单引号/双引号值也不能靠否定字符类或转义分支跨普通换行。
    source = f'password = "first{newline}public = 1'
    assert_redacted(source, f'password = "<redacted>"{newline}public = 1')
    source = f'user:credential{newline}@host'
    assert_redacted(source, source, 0)
    assert r"\s" not in SECRET_PATTERN.pattern


def test_url_and_standalone_keys_keep_existing_rules_and_counts():
    source = ('jdbc:mysql://user:one@host/db /user:two@host "user:three@host" '
              'AKIA1234567890ABCDEF sk-1234567890abcdefghijklmnop')
    expected = ('jdbc:<redacted>@host/db /user:<redacted>@host "user:<redacted>@host" '
                '<redacted> <redacted>')
    assert_redacted(source, expected, 5)


@pytest.mark.parametrize("prefix", ["", ":", "::", "/", "//", "text: ", "=", "("])
def test_url_credentials_keep_arbitrary_surrounding_text(prefix):
    assert_redacted(prefix + "user:credential@host", prefix + "user:<redacted>@host")


def test_secret_named_url_user_is_idempotent():
    assert_redacted("password:#credential@host", "password:<redacted>@host")


@pytest.mark.parametrize("value", [
    'foo"bar baz"', "foo'bar baz'", "word'''password:#credential@host'''", "literal#credential",
])
def test_concatenated_quoted_fragments_are_one_inline_value(value):
    assert_redacted(f"password={value}; public = 1", "password=<redacted>; public = 1")


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("kind", [
    "reported", "spaces", "newlines", "continuations", "quoted", "triple", "identifier", "url-like",
    "split-key", "empty", "around-separator",
])
def test_pathological_inputs_finish_within_200ms(newline, separator, kind):
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
    else:
        source = "x" * 20000 if kind == "identifier" else "u:" * 10000
        expected = source
    started = perf_counter()
    cleaned, count = redact(source)
    elapsed = perf_counter() - started
    assert elapsed < 0.2, f"{kind}: {len(source)} chars took {elapsed:.6f}s"
    assert cleaned == expected
    assert count == (0 if kind in ("identifier", "url-like", "split-key", "empty") else 1)
    assert redact(cleaned) == (cleaned, 0)
