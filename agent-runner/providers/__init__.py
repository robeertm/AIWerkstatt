"""Run-adapter registry. Importing this package registers all bundled adapters."""
from . import base  # noqa: F401
from . import claude_code  # noqa: F401
from . import mock  # noqa: F401  (demo — depends on claude_code)
from . import openai_codex  # noqa: F401
from . import gemini_cli  # noqa: F401

from .base import AgentAdapter, RunContext, get_adapter, register  # noqa: F401
