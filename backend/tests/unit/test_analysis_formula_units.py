"""derive AST 量纲规则：只使用登记元数据，不根据名称推测。"""

import pytest

from avelis.analysis.measures import formula_unit


@pytest.mark.parametrize(
    "formula, units, expected",
    [
        (
            "sep - aug",
            {"sep": {"kind": "level", "unit": "单"}, "aug": {"kind": "level", "unit": "单"}},
            {"kind": "delta", "unit": "单"},
        ),
        ("(a - b) / (c - d)", dict.fromkeys("abcd", {"kind": "level", "unit": "单"}), {"kind": "ratio"}),
        (
            "(aug - sep) / (0 - change)",
            {
                "aug": {"kind": "level", "unit": "单"},
                "sep": {"kind": "level", "unit": "单"},
                "change": {"kind": "delta", "unit": "单"},
            },
            {"kind": "ratio"},
        ),
        ("a / -b", dict.fromkeys("ab", {"kind": "level", "unit": "单"}), {"kind": "ratio"}),
        ("r * x", {"r": {"kind": "ratio"}, "x": {"kind": "level", "unit": "件"}}, {"kind": "level", "unit": "件"}),
        ("x * r", {"r": {"kind": "share"}, "x": {"kind": "delta", "unit": "件"}}, {"kind": "delta", "unit": "件"}),
        ("x / r", {"r": {"kind": "ratio"}, "x": {"kind": "level", "unit": "件"}}, {"kind": "level", "unit": "件"}),
        ("x * 100", {"x": {"kind": "delta", "unit": "件", "scale": "1000"}}, {"kind": "delta", "unit": "件"}),
        ("x / (100 * 100)", {"x": {"kind": "level", "unit": "件"}}, {"kind": "level", "unit": "件"}),
        ("-x", {"x": {"kind": "delta", "unit": "件"}}, {"kind": "delta", "unit": "件"}),
        ("0 - x", {"x": {"kind": "delta", "unit": "件"}}, {"kind": "delta", "unit": "件"}),
        ("x + 0", {"x": {"kind": "level", "unit": "件"}}, {"kind": "level", "unit": "件"}),
        ("0 + x", {"x": {"kind": "level", "unit": "件"}}, {"kind": "level", "unit": "件"}),
        ("x - 0", {"x": {"kind": "level", "unit": "件"}}, {"kind": "level", "unit": "件"}),
        ("x * 100", {"x": {"kind": "share"}}, {"kind": "share"}),
        ("a / b", {"a": {"kind": "level", "unit": "件"}, "b": {"kind": "count", "unit": "件"}}, {"kind": "ratio"}),
        (
            "a - b",
            {"a": {"kind": "level", "unit": "件", "scale": "1000"}, "b": {"kind": "level", "unit": "件"}},
            {"kind": "delta", "unit": "件"},
        ),
        ("a + b", {"a": {"kind": "level", "unit": "件"}, "b": {"kind": "level", "unit": "米"}}, {"kind": "unknown"}),
        ("a * b", dict.fromkeys("ab", {"kind": "level", "unit": "件"}), {"kind": "unknown"}),
        ("a / b", {"a": {"kind": "unknown"}, "b": {"kind": "level", "unit": "件"}}, {"kind": "unknown"}),
        ("x + 1", {"x": {"kind": "level", "unit": "件"}}, {"kind": "unknown"}),
        ("x * 0.3", {"x": {"kind": "level", "unit": "件"}}, {"kind": "unknown"}),
    ],
)
def test_formula_unit_recurses_through_arithmetic(formula, units, expected):
    assert formula_unit(formula, units) == expected


@pytest.mark.parametrize("formula", ["(r - s) * q", "q * (r - s)", "(r - s) / q"])
def test_scaling_percentage_point_differences_keeps_delta_semantics(formula):
    units = {"r": {"kind": "ratio"}, "s": {"kind": "ratio"}, "q": {"kind": "share"}}
    assert formula_unit(formula, units) == {"kind": "delta", "of_ratio": True}


def test_percentage_point_differences_can_form_a_dimensionless_ratio():
    units = dict.fromkeys("rstu", {"kind": "ratio"})
    assert formula_unit("(r - s) / (t - u)", units) == {"kind": "ratio"}


@pytest.mark.parametrize("formula", ["a / b - 1", "(a - b) / b"])
def test_existing_relative_change_forms_keep_their_kind(formula):
    assert formula_unit(formula, dict.fromkeys("ab", {"kind": "level", "unit": "件"})) == {"kind": "rate_change"}
