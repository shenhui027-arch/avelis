"""按引擎声明的客户端分隔符与 SQL token 切分脚本，保留原文位置。"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field

import sqlglot
from sqlglot.errors import TokenError
from sqlglot.tokens import TokenType

PLACEHOLDER = re.compile(r"\$\{(\w+)\}|\{(\w+)\}|%\((\w+)\)s")
_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_]\w*)?\$")


@dataclass
class Segment:
    line_start: int
    line_end: int
    method: str  # sql | python_ast | shell | unrecognized
    text: str
    placeholders: list[str] = field(default_factory=list)


def placeholders_of(text: str) -> list[str]:
    names = []
    for match in PLACEHOLDER.finditer(text):
        name = next(group for group in match.groups() if group)
        if name not in names:
            names.append(name)
    return names


@dataclass
class _ClientState:
    quote: str = ""
    dollar: str = ""
    comments: int = 0

    @property
    def neutral(self) -> bool:
        return not (self.quote or self.dollar or self.comments)


def _client_marks(line: str, state: _ClientState, delimiter: str) -> list[int]:
    """只识别客户端标记；字符串和注释中的同名文本仍交给 SQL 分词器。"""
    marks: list[int] = []
    index = 0
    while index < len(line):
        char, pair = line[index], line[index : index + 2]
        if state.comments:
            if pair == "/*":
                state.comments += 1
                index += 2
            elif pair == "*/":
                state.comments -= 1
                index += 2
            else:
                index += 1
        elif state.dollar:
            if line.startswith(state.dollar, index):
                index += len(state.dollar)
                state.dollar = ""
            else:
                index += 1
        elif state.quote:
            if char == "\\" and state.quote != "`":
                index += 2
            elif char == state.quote:
                if index + 1 < len(line) and line[index + 1] == state.quote:
                    index += 2
                else:
                    state.quote = ""
                    index += 1
            else:
                index += 1
        elif delimiter and line.startswith(delimiter, index):
            marks.append(index)
            index += len(delimiter)
        elif pair == "--" or char == "#":
            break
        elif pair == "/*":
            state.comments = 1
            index += 2
        elif char in ("'", '"', "`", "["):
            state.quote = "]" if char == "[" else char
            index += 1
        elif char == "$" and (match := _DOLLAR_QUOTE.match(line, index)):
            state.dollar = match.group(0)
            index = match.end()
        else:
            index += 1
    return marks


def _client_chunks(text: str, separators: Sequence[str]) -> list[tuple[str, int, bool]]:
    declared = {separator.upper() for separator in separators}
    state, delimiter = _ClientState(), ""
    chunks: list[tuple[str, int, bool]] = []
    start, offset = 0, 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        directive = stripped.split(maxsplit=1)
        if state.neutral and "DELIMITER" in declared and len(directive) == 2 and directive[0].upper() == "DELIMITER":
            chunks.append((text[start:offset], start, bool(delimiter) or "GO" in declared))
            delimiter = directive[1] if directive[1] != ";" else ""
            start = offset + len(line)
        elif state.neutral and stripped.upper() in declared - {"DELIMITER"}:
            chunks.append((text[start:offset], start, True))
            start = offset + len(line)
        else:
            for mark in _client_marks(line, state, delimiter):
                end = offset + mark
                chunks.append((text[start:end], start, True))
                start = end + len(delimiter)
        offset += len(line)
    chunks.append((text[start:], start, bool(delimiter) or "GO" in declared))
    return chunks


def _token_segments(text: str, dialect_name: str, line_offset: int, routine_batch: bool) -> list[Segment]:
    tokens = sqlglot.tokenize(text, read=dialect_name)
    line_starts = [0, *(match.end() for match in re.finditer("\n", text))]
    segments: list[Segment] = []
    blocks: list[TokenType] = []
    parentheses = 0
    first = None

    def emit(end: int) -> None:
        if first is not None:
            statement = text[first:end].rstrip()
            if statement:
                segments.append(
                    Segment(
                        line_offset + bisect_right(line_starts, first),
                        line_offset + bisect_right(line_starts, first + len(statement) - 1),
                        "sql",
                        statement,
                        placeholders_of(statement),
                    )
                )

    # 客户端已给出过程定义的边界，过程可以没有 BEGIN，或在 BEGIN 前有声明。
    routine_types = {TokenType.PROCEDURE, TokenType.FUNCTION, TokenType.TRIGGER}
    if (
        routine_batch
        and tokens
        and tokens[0].token_type == TokenType.CREATE
        and any(token.token_type in routine_types for token in tokens[1:5])
    ):
        first = tokens[0].start
        last = len(tokens) - 1
        while last >= 0 and tokens[last].token_type == TokenType.SEMICOLON:
            last -= 1
        emit(tokens[last].end + 1)
        return segments

    for index, token in enumerate(tokens):
        kind = token.token_type
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if kind == TokenType.SEMICOLON and not blocks and not parentheses:
            emit(token.start)
            first = None
            continue
        if first is None:
            first = token.start
        if kind == TokenType.L_PAREN:
            parentheses += 1
        elif kind == TokenType.R_PAREN:
            parentheses = max(0, parentheses - 1)
        elif kind == TokenType.CASE and (index == 0 or tokens[index - 1].token_type != TokenType.END):
            blocks.append(kind)
        elif kind == TokenType.BEGIN:
            # BEGIN; 与 BEGIN TRAN[SACTION] / WORK 是事务，不是过程块。
            if (
                following
                and following.token_type != TokenType.SEMICOLON
                and following.text.upper()
                not in {
                    "TRANSACTION",
                    "TRAN",
                    "WORK",
                    "DISTRIBUTED",
                }
            ):
                blocks.append(kind)
        elif kind == TokenType.END and blocks:
            # END IF / LOOP 等结束控制流，不能提前关闭外围 BEGIN。
            if not following or following.text.upper() not in {"IF", "LOOP", "WHILE", "REPEAT"}:
                blocks.pop()
    emit(len(text))
    return segments


def _character_segments(text: str, line_offset: int) -> list[Segment]:
    """分词失败时沿用原来的引号、注释与分号切分。"""
    segments, buf = [], []
    line, start_line = 1, None
    index, size = 0, len(text)
    quote = None
    while index < size:
        char = text[index]
        following = text[index + 1] if index + 1 < size else ""
        if quote:
            buf.append(char)
            if char == "\\" and quote != "`":
                buf.append(following)
                index += 2
                continue
            if char == quote:
                quote = None
        elif char == "-" and following == "-" or char == "#":
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        elif char == "/" and following == "*":
            end = text.find("*/", index + 2)
            end = size if end < 0 else end + 2
            line += text.count("\n", index, end)
            index = end
            continue
        elif char in ("'", '"', "`"):
            quote = char
            buf.append(char)
            start_line = start_line or line
        elif char == ";":
            statement = "".join(buf).strip()
            if statement:
                segments.append(
                    Segment(start_line + line_offset, line + line_offset, "sql", statement, placeholders_of(statement))
                )
            buf, start_line = [], None
        else:
            if not char.isspace() and start_line is None:
                start_line = line
            buf.append(char)
        if char == "\n":
            line += 1
        index += 1
    statement = "".join(buf).strip()
    if statement:
        segments.append(
            Segment(start_line + line_offset, line + line_offset, "sql", statement, placeholders_of(statement))
        )
    return segments


def split_sql(
    text: str,
    dialect_name: str = "starrocks",
    separators: Sequence[str] = (),
    *,
    line_offset: int = 0,
    notes: list[str] | None = None,
) -> list[Segment]:
    """先处理客户端分隔符，再按方言 token 的顶层分号切分；说明写入 notes。"""
    segments = []
    line_starts = [0, *(match.end() for match in re.finditer("\n", text))]
    for chunk, offset, routine_batch in _client_chunks(text, separators):
        if not chunk.strip():
            continue
        chunk_offset = line_offset + bisect_right(line_starts, offset) - 1
        try:
            segments.extend(_token_segments(chunk, dialect_name, chunk_offset, routine_batch))
        except TokenError as exc:
            if notes is not None:
                notes.append(f"第 {chunk_offset + 1} 行起 SQL 分词失败，按分号回退切分：{exc}")
            segments.extend(_character_segments(chunk, chunk_offset))
    return segments
