"""Branding terpusat untuk laris.AI."""

from __future__ import annotations

import os

APP_NAME = "laris.AI"
APP_TAGLINE = "AI Multi-Agent untuk UMKM Indonesia"
APP_TAGLINE_DASHBOARD = "Partner Bisnis UMKM"
PAGE_TITLE = f"{APP_NAME} — {APP_TAGLINE}"
DASHBOARD_TITLE = f"{APP_NAME} — {APP_TAGLINE_DASHBOARD}"
PAGE_ICON = "🚀"

BRAND_HTML = 'laris<span>.AI</span>'
LOGIN_BRAND_HTML = 'laris<span style="color:#C8382E;">.AI</span>'
LANDING_LOGO_HTML = 'laris<span style="color:#7c3aed;font-weight:900">.AI</span>'

SCORE_LABEL = "Laris Score"
EXPORT_PREFIX = "LarisAI"
WA_BOT_TITLE = f"{APP_NAME} WhatsApp Bot"

WA_NUMBER = os.environ.get("WA_NUMBER", "6282112826851")
WA_BASE_URL = f"https://wa.me/{WA_NUMBER}"
LOGIN_QUERY = "?login=1"
DEMO_QUERY = "?demo=1"
