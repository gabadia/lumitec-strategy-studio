import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "anthropic" not in sys.modules:
    anthropic_stub = types.ModuleType("anthropic")

    class AsyncAnthropic:
        def __init__(self, *args, **kwargs):
            pass

    anthropic_stub.AsyncAnthropic = AsyncAnthropic
    sys.modules["anthropic"] = anthropic_stub

from agent import _extract_code_from_text, _generation_scaffold_issues


def test_extracts_code_when_plain_text_has_trailing_prose() -> None:
    text = """
Here is the strategy implementation.

from lumitec import LumitecBaseStrategy

class MomentumStrategy(LumitecBaseStrategy):
    def on_start(self):
        self.log("starting")

This note should not be treated as code.
"""

    code = _extract_code_from_text(text)

    assert code is not None
    assert "class MomentumStrategy(LumitecBaseStrategy):" in code
    assert "This note should not be treated as code." not in code


def test_generation_scaffold_issues_flags_missing_required_structure() -> None:
    code = '''"""
Strategy created using Lumitec's Strategy Studio version X.
"""

class NotStrategy:
    pass
'''

    issues = _generation_scaffold_issues(code)

    assert any("top-level Config class" in issue for issue in issues)
    assert any("ConfigParams dataclass" in issue for issue in issues)
    assert any("missing required risk field max_position" in issue for issue in issues)
