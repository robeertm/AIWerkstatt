"""Provider registry. Importing this package registers all bundled providers.

To add a provider: create a module here that calls ``base.register(...)`` and a
matching run-adapter in ``agent-runner/providers``, then import it below.
"""
from . import base  # noqa: F401
from . import mock  # noqa: F401  (demo — zero config, listed first)
from . import claude_code  # noqa: F401
from . import openai_codex  # noqa: F401
from . import gemini_cli  # noqa: F401

from .base import all_specs, get_spec, register, ProviderSpec, ModelChoice  # noqa: F401
