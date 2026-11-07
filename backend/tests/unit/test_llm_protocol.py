"""工具调用标记只用于识别正文泄漏，不解析提供方协议。"""

import pytest

from avelis.llm_protocol import has_leaked_tool_markup


@pytest.mark.parametrize(
    "text",
    [
        "｜DSML｜",
        '<｜DSML｜function_calls><｜DSML｜invoke name="chart">',
        '<tool_call>{"name": "chart"}</tool_call>',
        "结果说明</tool_call>",
        '<function_calls><invoke name="chart">',
        '<invoke name="chart">',
    ],
)
def test_has_leaked_tool_markup_recognizes_tool_protocol_markers(text):
    assert has_leaked_tool_markup(text)


@pytest.mark.parametrize("text", ["", " \n ", "华东成交额最高。", "chart(kind='bar')", "DSML"])
def test_has_leaked_tool_markup_accepts_plain_explanations(text):
    assert not has_leaked_tool_markup(text)
