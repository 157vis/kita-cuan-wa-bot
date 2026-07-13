# main.py — Bot WhatsApp laris.AI (satu-satunya entry point bot)
from __future__ import annotations

import asyncio
import base64
import os
import random

import httpx
from fastapi import FastAPI, Request
from groq import Groq

from paths import bootstrap_paths, load_project_dotenv

bootstrap_paths()
load_project_dotenv()

from brand import APP_NAME, SCORE_LABEL, WA_BOT_TITLE  # noqa: E402
from agents import (  # noqa: E402
    ai_extractor_agent,
    calculate_cuan_score,
    classify_wa_intent,
    core,
    db_insert_transaction,
    get_ai_advisor_insights,
    get_ai_piutang_answer,
    get_dashboard_data,
    resolve_user_id,
    vision_extractor_agent,
    voice_extractor_agent,
)
from bot_helpers import (  # noqa: E402
    BOT_NAME,
    bot_header,
    extract_incoming,
    get_greeting,
    is_duplicate_inbound,
    is_likely_record_command,
    is_outgoing_or_bot_echo,
    parse_webhook_body,
    random_confirm,
    random_typing_delay,
    sanitize_intent,
    detect_intent_rules,
)
from config import BOT_LOGIC_VERSION, REORDER_QTY, STOCK_THRESHOLD  # noqa: E402
from fonnte_client import FonnteClient  # noqa: E402
from log_config import get_logger  # noqa: E402
from orchestrator import orchestrate_transaction_created  # noqa: E402

logger = get_logger(__name__)

app = FastAPI(title=WA_BOT_TITLE)

_raw_provider = os.environ.get("WA_PROVIDER", "fonnte").lower().strip()
WA_PROVIDER = "fonnte" if _raw_provider in ("fonnte", "fonte") else _raw_provider
# WA_API_KEY jadi OPSIONAL — fallback saja. Token utama dibaca dari Supabase.
WA_API_KEY = os.environ.get("WA_API_KEY", "").strip()
SAFEGUARD_MODEL = "openai/gpt-oss-safeguard-20b"

# Fonnte client multi-tenant (lazy init setelah supabase siap)
_fonnte: FonnteClient | None = None


def get_fonnte() -> FonnteClient:
    """Lazy init FonnteClient — Supabase service client dari agents.core."""
    global _fonnte
    if _fonnte is None:
        _fonnte = FonnteClient(get_core().supabase)
    return _fonnte

# Lazy Groq client — jangan instantiate saat module-level, supaya
# service tetap bisa start meskipun GROQ_API_KEY belum di-set di env
# (Railway Variables). Validasi dilakukan saat pertama kali dipakai.
_groq_client: Groq | None = None


def get_groq() -> Groq:
    """Ambil (atau buat) Groq client. Raise RuntimeError jika env var hilang."""
    global _groq_client
    if _groq_client is not None:
        return _groq_client
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY belum di-set. Tambahkan di Railway Variables "
            "(tab Variables di service kita-cuan-wa-bot-larisai)."
        )
    _groq_client = Groq(api_key=api_key)
    return _groq_client


@app.get("/health")
def health() -> dict:
    """Healthcheck endpoint — Railway akan panggil path ini berkala.

    Multi-tenant: WA_API_KEY tidak wajib. Token Fonnte per-client
    dibaca dari tabel `clients` di Supabase (kolom `fonnte_token`).
    """
    required = ("SUPABASE_URL", "SUPABASE_KEY", "GROQ_API_KEY")
    missing = [k for k in required if not os.environ.get(k)]
    return {
        "status": "ok" if not missing else "degraded",
        "service": WA_BOT_TITLE,
        "provider": WA_PROVIDER,
        "missing_env": missing,
        "env_token_fallback": bool(WA_API_KEY),
        "token_source": "supabase_per_client",
        "bot_logic_version": BOT_LOGIC_VERSION,
    }


def _normalize_wa_phone(phone: str) -> str:
    return core.normalize_phone(phone or "")


def _resolve_user_id_safe(phone: str) -> str | None:
    try:
        return resolve_user_id(phone)
    except (RuntimeError, ValueError, KeyError) as exc:
        logger.warning("resolve_user_id: %s", exc)
        return os.environ.get("WA_DEFAULT_USER_ID") or None


def _persist_wa_log(phone: str, text: str, reply: str, user_id: str | None = None) -> bool:
    uid = core.normalize_user_id(user_id) if user_id else None
    if not uid:
        uid = _resolve_user_id_safe(phone)
    if not uid:
        logger.warning("wa_messages skip: nomor %s belum di wa_users", phone)
        return False
    ok = True
    if text:
        ok = core.log_wa_message(uid, "user", text, phone=phone) is not None and ok
    if reply:
        ok = core.log_wa_message(uid, "assistant", reply, phone=phone, agent_id="admin") is not None and ok
    return ok


def _orchestrate(user_id: str, raw_text: str, data: list[dict]) -> str:
    return orchestrate_transaction_created(
        user_id,
        raw_text,
        data,
        stock_threshold=STOCK_THRESHOLD,
        reorder_qty=REORDER_QTY,
    )


async def send_wa_reply(phone: str, message: str, inboxid: str | None = None) -> None:
    """Kirim balasan WA — multi-tenant.

    Token Fonnte di-resolve otomatis per-nomor dari tabel `clients` Supabase.
    Fallback ke env `WA_API_KEY` kalau ada (backward compat untuk legacy).
    """
    if not phone or not message:
        logger.warning("send_wa_reply: phone/message kosong")
        return
    try:
        client = get_fonnte()
        await client.send_message(phone, message, inboxid=inboxid)
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        logger.exception("send_wa_reply: %s", exc)


async def detect_intent(text: str) -> str:
    ruled = detect_intent_rules(text)
    if ruled:
        return ruled
    intent = classify_wa_intent(text)
    logger.debug("AI intent=%r", intent)
    return intent


@app.api_route("/webhook", methods=["GET", "POST"])
async def webhook(request: Request):
    if request.method == "GET":
        return {
            "status": "webhook_ready",
            "provider": WA_PROVIDER,
            "bot_name": BOT_NAME,
            "bot_logic_version": BOT_LOGIC_VERSION,
            "hint": "POST dari Fonnte ke URL ini",
        }

    body = await parse_webhook_body(request)
    logger.debug("webhook keys: %s", list(body.keys()))

    phone, text, media_type, media_url, inboxid = extract_incoming(
        body, WA_PROVIDER, _normalize_wa_phone
    )

    if not phone:
        logger.error("webhook: nomor tidak ditemukan. body=%s", str(body)[:500])
        return {"status": "error", "detail": "No phone number"}

    if is_outgoing_or_bot_echo(body, phone, text, _normalize_wa_phone):
        logger.debug("ignored echo/outgoing: %s", phone)
        return {"status": "ignored", "reason": "outgoing_or_bot_echo"}

    if text and is_duplicate_inbound(body, phone, text, _normalize_wa_phone):
        logger.debug("ignored duplicate: %s", phone)
        return {"status": "ignored", "reason": "duplicate"}

    if text.lower() in ("test", "ping", "tes", "halo", "hi"):
        greeting = get_greeting()
        reply = (
            f"{greeting}! 👋\n\n"
            f"Aku {BOT_NAME}, asisten pembukuan tokomu~\n\n"
            f"Mau catat apa hari ini?\n"
            f"• _jual kopi 15rb_\n"
            f"• _beli bensin 50rb_\n"
            f"• _utang budi 100rb_\n\n"
            f"Atau tanya: _{BOT_NAME.lower()}, gimana bisnis aku?_"
        )
        await asyncio.sleep(random.uniform(0.5, 1.5))
        await send_wa_reply(phone, reply, inboxid=inboxid)
        logged = _persist_wa_log(phone, text, reply)
        return {"status": "ok", "mode": "ping", "wa_logged": logged}

    reply = ""
    user_id = None

    try:
        user_id = resolve_user_id(phone)

        if media_type in ("image", "photo") and media_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(media_url)
                b64 = base64.b64encode(resp.content).decode("utf-8")
            data = vision_extractor_agent(b64)
            for row in data:
                is_prv = "prive" in str(row.get("category", "")).lower()
                db_insert_transaction(
                    row.get("type"), row.get("category"), row.get("amount"), row.get("note"),
                    is_prive=is_prv, user_id=user_id,
                )
            total = sum(row.get("amount", 0) for row in data)
            templates = [
                f"📸 Struk kebaca!\nTotal: Rp {total:,.0f}\n{len(data)} transaksi masuk buku~",
                f"✅ Sip, struk terbaca!\nTotal: Rp {total:,.0f}\n{len(data)} transaksi tercatat.",
            ]
            reply = f"{bot_header()}\n\n{random.choice(templates)}"
            reply += _orchestrate(user_id, text or "struk", data)

        elif media_type in ("audio", "voice") and media_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(media_url)
            data = voice_extractor_agent(resp.content)
            for row in data:
                is_prv = "prive" in str(row.get("category", "")).lower()
                db_insert_transaction(
                    row.get("type"), row.get("category"), row.get("amount"), row.get("note"),
                    is_prive=is_prv, user_id=user_id,
                )
            templates = [
                f"🎤 Suara kebaca!\n{len(data)} transaksi masuk buku~",
                f"✅ Sip, suaramu aku dengerin!\n{len(data)} transaksi tercatat.",
            ]
            reply = f"{bot_header()}\n\n{random.choice(templates)}"
            reply += _orchestrate(user_id, text or "voice", data)

        elif text:
            intent = sanitize_intent(text, await detect_intent(text), classify_wa_intent)
            logger.debug("intent=%r text=%r", intent, text[:80])

            if intent == "CATAT" and is_likely_record_command(text):
                data = ai_extractor_agent(text)
                if not data:
                    reply = (
                        f"{bot_header()}\n\n"
                        f"Hmm, aku belum nangkep transaksinya 🤔\n"
                        f"Coba: _jual kopi 50rb_ atau _beli minyak 18000_"
                    )
                else:
                    for row in data:
                        is_prv = "prive" in str(row.get("category", "")).lower()
                        db_insert_transaction(
                            row.get("type"), row.get("category"), row.get("amount"), row.get("note"),
                            is_prive=is_prv, user_id=user_id,
                        )
                    reply = f"{bot_header()}\n\n{random_confirm(data)}"
                    reply += _orchestrate(user_id, text, data)

            elif intent == "SKOR":
                df = get_dashboard_data(user_id)
                score = calculate_cuan_score(df)
                s = score["score"]
                emoji = "🔥" if s >= 80 else "👍" if s >= 60 else "💪" if s >= 40 else "📈"
                reply = (
                    f"{bot_header()}\n\n{emoji} *{SCORE_LABEL}: {s}/100*\n\n_{score['insight']}_"
                )

            elif intent == "SARAN":
                df = get_dashboard_data(user_id)
                advice = get_ai_advisor_insights(df)
                reply = f"{bot_header()}\n\n💡 Saran buat tokomu:\n\n{advice}"

            elif intent == "PIUTANG":
                df = get_dashboard_data(user_id)
                answer = get_ai_piutang_answer(df, text)
                reply = f"{bot_header()}\n\n{answer}"

            elif intent == "HAPUS":
                txn = core.delete_last_transaction(user_id)
                if txn:
                    reply = (
                        f"{bot_header()}\n\n🗑️ Oke, udah dihapus ya~\n"
                        f"• {txn['note']} — Rp {txn['amount']:,.0f}"
                    )
                else:
                    reply = f"{bot_header()}\n\nHmm, nggak ada transaksi yg bisa dihapus nih 🤔"

            else:
                reply = (
                    f"{bot_header()}\n\n"
                    f"Hmm, {BOT_NAME} belum paham maksudmu 🤔\n\n"
                    f"Coba:\n• _jual kopi 50rb_\n• _berapa skor_\n• _saran bisnis_\n• _hapus_"
                )
        else:
            reply = f"{bot_header()}\n\nKirim teks, foto struk, atau voice note ya~ 😊"

    except (RuntimeError, ValueError, KeyError, httpx.HTTPError) as exc:
        logger.exception("webhook error: %s", exc)
        reply = f"{bot_header()}\n\n😅 Waduh, ada gangguan sebentar.\nCoba kirim lagi ya~"

    await asyncio.sleep(random_typing_delay())
    await send_wa_reply(phone, reply, inboxid=inboxid)
    _persist_wa_log(phone, text, reply, user_id=user_id)
    return {"status": "ok"}


@app.get("/")
async def health():
    return {
        "status": f"{APP_NAME} WA Bot is running",
        "bot_name": BOT_NAME,
        "provider": WA_PROVIDER,
        "bot_logic_version": BOT_LOGIC_VERSION,
        "wa_key_set": bool(WA_API_KEY),  # fallback env token (backward compat)
        "token_source": "supabase_per_client",
        "env_token_fallback": bool(WA_API_KEY),
        "supabase_set": bool(os.environ.get("SUPABASE_URL")),
        "groq_set": bool(os.environ.get("GROQ_API_KEY")),
        "webhook": "/webhook",
    }
