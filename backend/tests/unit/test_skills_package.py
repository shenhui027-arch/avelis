"""技能包（Agent Skills 规范 zip）校验：合规包通过，越界路径、命名与 frontmatter 违规被拒，脚本只告警。"""

import io
import zipfile

from avelis.skills.package import validate_zip

SKILL_MD = """---
name: demo-skill
description: 演示用技能：说明何时使用与步骤。
license: MIT
---

# 步骤

1. 先核对口径。
2. 再拆解贡献。
"""


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    return buf.getvalue()


def _levels(result) -> list[str]:
    return [check["level"] for check in result.checks]


def test_valid_package_in_top_level_directory_passes():
    result = validate_zip(_zip({"demo-skill/SKILL.md": SKILL_MD, "demo-skill/references/guide.md": "参考"}))
    assert result.ok
    assert result.name == "demo-skill"
    assert result.description.startswith("演示用技能")
    assert "error" not in _levels(result)
    kinds = {f["path"]: f["kind"] for f in result.files}
    assert kinds["SKILL.md"] == "skill_md"
    assert kinds["references/guide.md"] == "reference"


def test_scripts_are_kept_but_only_warned():
    result = validate_zip(_zip({"demo-skill/SKILL.md": SKILL_MD, "demo-skill/scripts/run.py": "print(1)"}))
    assert result.ok
    assert result.has_scripts
    assert "warn" in _levels(result)


def test_name_must_match_directory():
    result = validate_zip(_zip({"other-dir/SKILL.md": SKILL_MD}))
    assert not result.ok
    assert "error" in _levels(result)


def test_invalid_name_and_missing_description_are_errors():
    bad = "---\nname: Bad_Name\n---\n正文\n"
    result = validate_zip(_zip({"SKILL.md": bad}))
    assert not result.ok
    assert _levels(result).count("error") >= 2


def test_path_traversal_and_absolute_paths_are_rejected():
    for evil in ("../escape.md", "/etc/evil.md"):
        result = validate_zip(_zip({"demo-skill/SKILL.md": SKILL_MD, evil: "x"}))
        assert not result.ok, evil
        assert "error" in _levels(result)


def test_package_without_skill_md_is_rejected():
    result = validate_zip(_zip({"demo-skill/README.md": "no skill"}))
    assert not result.ok


def test_not_a_zip_is_rejected():
    result = validate_zip(b"definitely not a zip")
    assert not result.ok
    assert "error" in _levels(result)


def test_oversized_frontmatter_is_rejected_before_yaml_parsing():
    entries = "".join(f"  k{i}: '{'x' * 40}'\n" for i in range(600))
    huge = "---\nname: demo-skill\ndescription: d\nmetadata:\n" + entries + "---\n正文\n"
    result = validate_zip(_zip({"demo-skill/SKILL.md": huge}))
    assert not result.ok


def test_yaml_scalars_like_booleans_and_dates_stay_strings():
    md = "---\nname: on\ndescription: d\nmetadata:\n  updated: 2026-11-19\n---\n正文\n"
    result = validate_zip(_zip({"on/SKILL.md": md}))
    assert result.ok
    assert result.name == "on"
    assert result.frontmatter["metadata"] == {"updated": "2026-11-19"}
