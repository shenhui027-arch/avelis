"""理解问题阶段：没有匹配口径不必追问，回答过的追问不再重复。

2026-11-28 真实部署联调：新接入的数据源没有已发布口径，用户确认“直接查表”后，
理解阶段仍反复写 clarification，两次后以“需要澄清”结束，推理阶段的探索查询走不到。
"""

from avelis.analysis.agent import UNDERSTAND_PROMPT


def test_empty_metric_codes_is_not_a_reason_to_clarify():
    assert "metric_codes 留空即可，不要因此追问" in UNDERSTAND_PROMPT
    assert "推理阶段可以直接查询允许探索的数据源" in UNDERSTAND_PROMPT


def test_answered_clarification_counts_as_confirmed():
    assert "回答过的点视为已确认，不要就同一点再追问" in UNDERSTAND_PROMPT
