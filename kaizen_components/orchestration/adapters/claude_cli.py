"""Compatibility import for the retired Claude CLI adapter name.

Driven Claude sessions use :class:`ClaudeSdkAdapter`.  The ``claude_cli`` engine input alias remains
accepted at the adapter-registry boundary, but the former CLI subprocess gate is not available through
this module.
"""

from __future__ import annotations

from .claude_sdk import ClaudeSdkAdapter, ClaudeSdkAdapterError


ClaudeCliAdapter = ClaudeSdkAdapter
ClaudeCliAdapterError = ClaudeSdkAdapterError

__all__ = ["ClaudeCliAdapter", "ClaudeCliAdapterError"]
