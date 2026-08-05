"""Demo run adapter — runs the built-in demo builder (no CLI, no key, no network).

Reuses the Claude stream-json parsing since demo_agent.py emits the same shapes.
"""
from .claude_code import ClaudeAdapter
from .base import RunContext, register


class MockAdapter(ClaudeAdapter):
    id = "demo"

    def __init__(self):
        self.model = "demo"

    def build_argv(self, ctx: RunContext) -> list[str]:
        self.model = ctx.model or "demo"
        return ["python3", "/runner/demo_agent.py"]

    def default_context_window(self, model: str) -> int:
        return 200_000


register(MockAdapter())
