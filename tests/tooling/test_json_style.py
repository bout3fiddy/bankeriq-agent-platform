"""Repository JSON line limits include exact boundary and invalid-input checks."""

from scripts.linting.check_json import findings


def test_json_line_length_boundary() -> None:
    assert findings('"' + "x" * 98 + '"') == []
    assert findings('"' + "x" * 99 + '"') == [(1, "JSON line exceeds 100 characters (101)")]


def test_json_syntax_and_line_numbers() -> None:
    problems = findings('{\n  "value": "' + "x" * 100 + '"\n}')
    assert problems[0][0] == 2
    assert "invalid JSON" in findings('{"missing":}')[0][1]
