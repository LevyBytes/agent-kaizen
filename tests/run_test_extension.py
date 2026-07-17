#!/usr/bin/env python3
"""Visible-terminal entry point for Kaizen Test Extension."""

from __future__ import annotations

import sys
from pathlib import Path

sys.dont_write_bytecode = True  # Suppress bytecode for the delegated import chain.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kaizen_components.orchestration.test_extension import main


if __name__ == "__main__":
    raise SystemExit(main())
