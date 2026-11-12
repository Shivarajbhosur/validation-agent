"""
File tools (shared, non-step-specific).

Step-specific filesystem helpers live with their operation:
  * Step 1 — ``prompts/skills/copy/scripts/tools/file_tools.py``
    (``copy_baseline``, ``baseline_exists_at_source``).

This module keeps the helpers that are still used outside of any
specific step (e.g. ``baseline_exists_at_dest`` is consulted by status
displays).
"""

import shutil
from pathlib import Path
from typing import Tuple

import config
from utils.logger import get_logger

logger = get_logger("file_tools")


def baseline_exists_at_dest(baseline_name: str) -> bool:
    """Check if baseline exists at destination (C:)."""
    dest = config.get_baseline_path(baseline_name)
    return dest.exists()