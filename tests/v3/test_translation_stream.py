import pytest
from easylearn.translation import (
    TranslationUnit,
    _batches,
    _build_translation_prompt,
    _parse_translation_response,
)
from easylearn.errors import DomainError


def _make_unit(block_id: str, node_id: str, text: str) -> TranslationUnit:
    return TranslationUnit(
        block_id=block_id,
        unit_id=f"{block_id}:{node_id}",
        source_text=text,
        kind="text",
    )


def test_batches_grouping_and_sizing():
    # Verify up to 40 units per batch when total length <= 8000
    units = tuple(_make_unit(f"b{i}", "n1", f"Paragraph {i}") for i in range(50))
    batches = _batches(units)
    assert len(batches) == 2
    assert len(batches[0]) == 40
    assert len(batches[1]) == 10

    # Verify splitting when total characters exceed 8000
    long_text = "A" * 5000
    units_long = (
        _make_unit("b1", "n1", long_text),
        _make_unit("b2", "n1", long_text),
    )
    batches_long = _batches(units_long)
    assert len(batches_long) == 2
    assert len(batches_long[0]) == 1
    assert len(batches_long[1]) == 1


def test_build_translation_prompt():
    units = (
        _make_unit("b1", "n1", "First paragraph text."),
        _make_unit("b2", "n1", "Second paragraph text."),
    )
    messages = _build_translation_prompt(units)
    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "[§1]" in messages[0]["content"]
    assert "严禁增加、删除、遗漏或修改任何 unit_id" not in messages[0]["content"]

    assert messages[1]["role"] == "user"
    expected_user = "[§1] First paragraph text.\n\n[§2] Second paragraph text."
    assert messages[1]["content"] == expected_user


def test_parse_translation_response_anchor_stream():
    units = (
        _make_unit("b1", "n1", "First paragraph text."),
        _make_unit("b2", "n1", "Second paragraph text."),
    )
    content = "[§1] 这是第一段译文。\n\n[§2] 这是第二段译文。"
    parsed = _parse_translation_response(content, units)
    assert parsed == {
        "b1:n1": "这是第一段译文。",
        "b2:n1": "这是第二段译文。",
    }


def test_parse_translation_response_with_conversational_chatter_and_code_fence():
    units = (
        _make_unit("b1", "n1", "Hello"),
        _make_unit("b2", "n1", "World"),
    )
    content = (
        "```markdown\n"
        "Here is the translation:\n\n"
        "[§1] 你好\n\n"
        "[§2] 世界\n\n"
        "```"
    )
    parsed = _parse_translation_response(content, units)
    assert parsed == {
        "b1:n1": "你好",
        "b2:n1": "世界",
    }


def test_parse_translation_response_single_unit_fallback_without_anchor():
    units = (_make_unit("b1", "n1", "Hello single paragraph"),)
    content = "你好单个段落，没有锚点标记。"
    parsed = _parse_translation_response(content, units)
    assert parsed == {"b1:n1": "你好单个段落，没有锚点标记。"}


def test_parse_translation_response_rejects_duplicate_anchors():
    units = (
        _make_unit("b1", "n1", "First paragraph."),
        _make_unit("b2", "n1", "Second paragraph."),
    )
    content = "[§1] 第一段第一次\n\n[§1] 第一段第二次"
    with pytest.raises(DomainError) as exc_info:
        _parse_translation_response(content, units)
    assert exc_info.value.code == "TRANSLATION_PROTOCOL_INVALID"


def test_parse_translation_response_json_fallback_with_unit_ids():
    units = (
        _make_unit("b1", "n1", "First paragraph."),
        _make_unit("b2", "n1", "Second paragraph."),
    )
    content = '{"b1:n1": "译：第一段", "b2:n1": "译：第二段"}'
    parsed = _parse_translation_response(content, units)
    assert parsed == {
        "b1:n1": "译：第一段",
        "b2:n1": "译：第二段",
    }


def test_parse_translation_response_json_fallback_with_anchor_keys():
    units = (
        _make_unit("b1", "n1", "First paragraph."),
        _make_unit("b2", "n1", "Second paragraph."),
    )
    content = '{"[§1]": "译：第一段", "[§2]": "译：第二段"}'
    parsed = _parse_translation_response(content, units)
    assert parsed == {
        "b1:n1": "译：第一段",
        "b2:n1": "译：第二段",
    }


def test_parse_translation_response_json_rejects_duplicate_keys():
    units = (
        _make_unit("b1", "n1", "First paragraph."),
        _make_unit("b2", "n1", "Second paragraph."),
    )
    content = '{"b1:n1": "first", "b1:n1": "second"}'
    with pytest.raises(DomainError) as exc_info:
        _parse_translation_response(content, units)
    assert exc_info.value.code == "TRANSLATION_PROTOCOL_INVALID"


def test_parse_translation_response_missing_anchor_enables_partial_recovery():
    units = (
        _make_unit("b1", "n1", "First paragraph."),
        _make_unit("b2", "n1", "Second paragraph."),
    )
    # LLM only returned [§1], [§2] was dropped
    content = "[§1] 仅翻译了第一段"
    parsed = _parse_translation_response(content, units)
    assert "b1:n1" in parsed
    assert "b2:n1" not in parsed
