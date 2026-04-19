"""Shared utilities for medical bots.

This package bundles reusable modules across all bots:
- pubmed:          PubMed E-utilities wrapper with Paper dataclass
- claude_client:   Claude API wrapper with retry + JSON parsing
- notify:          Discord Webhook posting helpers
- history:         Artifact and history persistence
- logging_config:  Standard logger setup
"""

from . import pubmed, claude_client, notify, history, logging_config

__all__ = ["pubmed", "claude_client", "notify", "history", "logging_config"]
