"""The single source of truth for the installed package version.

``pyproject.toml`` reads this file dynamically via ``[tool.hatch.version]``
(Hatchling's default regex version source, which looks for exactly this
``__version__ = "..."`` assignment), and ``macos_harness.__version__``
re-exports the same constant directly -- so there is exactly one place to
bump a release, and no runtime path ever falls back to
``importlib.metadata`` to rediscover it.
"""

from __future__ import annotations

__version__ = "0.2.0"
