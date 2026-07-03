"""Bridge ke laris_core — satu sumber logika bisnis."""

from __future__ import annotations

import os
from functools import lru_cache

from paths import bootstrap_paths

bootstrap_paths()

from laris_core import LarisCore  # noqa: E402
from log_config import get_logger

logger = get_logger(__name__)

_REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_KEY", "GROQ_API_KEY")


def _validate_env() -> None:
    missing = [k for k in _REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise RuntimeError(
            f"Environment belum lengkap: {', '.join(missing)}. "
            "Salin kita-cuan-wa-bot/.env.example ke .env lalu isi key."
        )


@lru_cache(maxsize=1)
def get_core() -> LarisCore:
    """Inisialisasi LarisCore sekali (lazy, setelah .env dimuat). Pakai service role untuk webhook."""
    _validate_env()
    url = os.environ["SUPABASE_URL"]
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    return LarisCore.from_service_client(url, key, os.environ["GROQ_API_KEY"])


class _CoreProxy:
    """Akses `core.method()` tanpa inisialisasi saat import modul."""

    def __getattr__(self, name: str):
        return getattr(get_core(), name)


core = _CoreProxy()


def resolve_user_id(phone: str) -> str:
    return get_core().resolve_user_id_by_phone(phone)


def get_dashboard_data(user_id: str):
    return get_core().get_dashboard_data(user_id)


def db_insert_transaction(type_txn, category, amount, note, is_prive=False, user_id=None):
    if not user_id:
        raise ValueError("user_id wajib untuk bot WhatsApp")
    get_core().db_insert_transaction(user_id, type_txn, category, amount, note, is_prive=is_prive)


def db_delete_transaction(txn_id, user_id=None):
    if not user_id:
        raise ValueError("user_id wajib untuk bot WhatsApp")
    get_core().db_delete_transaction(user_id, txn_id)


def ai_extractor_agent(text: str):
    return get_core().ai_extractor_agent(text)


def vision_extractor_agent(b64: str):
    return get_core().vision_extractor_agent_from_b64(b64)


def voice_extractor_agent(audio_bytes: bytes):
    return get_core().voice_extractor_agent_from_bytes(audio_bytes)


def calculate_cuan_score(df):
    return LarisCore.calculate_laris_score(df)


def get_ai_advisor_insights(df):
    return get_core().get_ai_advisor_insights(df)


def classify_wa_intent(text: str) -> str:
    return get_core().classify_wa_intent(text)


def get_ai_piutang_answer(df, question: str) -> str:
    return get_core().get_ai_piutang_answer(df, question)
