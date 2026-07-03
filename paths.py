"""Path helpers untuk kita-cuan-wa-bot (self-contained, standalone repo).

Repo ini berdiri sendiri (bukan monorepo), jadi PROJECT_ROOT =
folder repo ini sendiri.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# PROJECT_ROOT = folder repo ini (yaitu /app di Railway container).
PROJECT_ROOT = Path(__file__).resolve().parent
BOT_DIR = PROJECT_ROOT
STATIC_DIR = PROJECT_ROOT / "static"
SQL_DIR = PROJECT_ROOT / "sql"
STREAMLIT_DIR = PROJECT_ROOT / ".streamlit"
DATA_DIR = PROJECT_ROOT / "data"

_BOOTSTRAPPED = False


def bootstrap_paths() -> Path:
    """Pastikan folder repo ini ada di sys.path (sekali saja)."""
    global _BOOTSTRAPPED
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    _BOOTSTRAPPED = True
    return PROJECT_ROOT


def load_project_dotenv() -> Path | None:
    """Muat .env dari folder repo. Return path yang berhasil atau None."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None

    candidate = PROJECT_ROOT / ".env"
    if candidate.is_file():
        load_dotenv(candidate, override=False)
        return candidate
    return None


def ensure_cwd() -> None:
    """Set working directory ke folder repo."""
    os.chdir(PROJECT_ROOT)
