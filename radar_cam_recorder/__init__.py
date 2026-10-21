"""Record Raspberry Pi camera video and LD2451 radar data."""

from __future__ import annotations

import logging

__version__ = "0.1.0"

logging.getLogger(__name__).addHandler(logging.NullHandler())
