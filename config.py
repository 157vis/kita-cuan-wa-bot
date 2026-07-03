"""Konfigurasi bot — satu sumber untuk ambang stok & reorder."""

from __future__ import annotations

import os

STOCK_THRESHOLD = int(os.environ.get("STOCK_THRESHOLD", "10"))
REORDER_QTY = int(os.environ.get("REORDER_QTY", "20"))
BOT_LOGIC_VERSION = "2026-06-28-portable-v6"
