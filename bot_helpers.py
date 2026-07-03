"""Helper bot WhatsApp — greeting, intent, anti-loop echo."""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable
from urllib.parse import parse_qs

from log_config import get_logger

if TYPE_CHECKING:
    from groq import Groq

logger = get_logger(__name__)

BOT_NAME = "Laris"
BOT_EMOJI = "🛒"

BOT_TEXT_MARKERS = (
    "tercatat!",
    "webhook ok",
    "bot aktif",
    "belum paham",
    "logistik ai",
    "ruang komando",
    "saran ai",
    "tidak ada piutang",
    "daftar piutang",
    "struk terbaca",
    "suara terbaca",
    "belum terdaftar",
    "udah dicatat",
    "masuk buku",
    "noted!",
    "skor tokomu",
    "belum nangkep",
    "gangguan sebentar",
    BOT_NAME.lower(),
)

_recent_inbound: dict[str, float] = {}


def bot_header() -> str:
    """Header pesan bot dengan nama manusiawi."""
    return f"{BOT_EMOJI} *{BOT_NAME}*"


def get_greeting() -> str:
    """Sapaan berdasarkan jam lokal."""
    hour = datetime.now().hour
    if 5 <= hour < 11:
        return "Selamat pagi"
    if 11 <= hour < 15:
        return "Selamat siang"
    if 15 <= hour < 18:
        return "Selamat sore"
    return "Selamat malam"


def random_confirm(data: list[dict]) -> str:
    """Variasi konfirmasi transaksi."""
    lines = []
    for row in data:
        tipe = row.get("type", "")
        kat = row.get("category", "")
        amt = row.get("amount", 0)
        emoji = "💰" if tipe == "Pemasukan" else "💸"
        lines.append(f"{emoji} {kat}: Rp {amt:,.0f}")
    detail = "\n".join(lines)
    total = sum(row.get("amount", 0) for row in data)
    n = len(data)
    templates = [
        f"✅ Tercatat!\n{detail}",
        f"📝 Sip, udah dicatat!\n{detail}",
        f"👌 Masuk buku!\n{detail}",
        f"✅ Oke, tercatat ya~\n{detail}",
        f"📝 Noted!\n{detail}",
    ]
    base = random.choice(templates)
    if n > 1:
        base += f"\n\nTotal: Rp {total:,.0f} ({n} transaksi)"
    return base


def random_typing_delay() -> float:
    """Delay acak meniru ketikan manusia."""
    return random.uniform(0.8, 2.5)


def is_debt_inquiry(text: str) -> bool:
    """Deteksi pertanyaan piutang — jangan salah masuk CATAT."""
    t = (text or "").strip().lower()
    if not t or re.search(r"\d", t):
        return False
    debt_phrases = (
        "belum bayar",
        "belum lunas",
        "siapa belum",
        "siapa yang belum",
        "daftar piutang",
        "daftar utang",
        "yang ngutang",
        "yang hutang",
        "siapa ngutang",
        "siapa hutang",
    )
    if any(p in t for p in debt_phrases):
        return True
    if any(w in t for w in ("utang", "piutang", "kasbon", "hutang", "ngutang")):
        if any(w in t for w in ("siapa", "berapa", "daftar", "list", "tunjuk", "cek", "belum")):
            return True
    return False


def is_skor_inquiry(text: str) -> bool:
    t = (text or "").strip().lower()
    return bool(
        re.search(r"\b(skor|score)\b", t)
        or "laris score" in t
        or "kesehatan bisnis" in t
        or "sehat tidak" in t
    )


def is_saran_inquiry(text: str) -> bool:
    t = (text or "").strip().lower()
    keywords = ("saran", "tips", "rekomendasi", "evaluasi bisnis", "masukan bisnis", "minta saran")
    return any(w in t for w in keywords)


def is_hapus_command(text: str) -> bool:
    t = (text or "").strip().lower()
    return t.startswith("hapus") or t in ("hapus", "batal", "undo") or "hapus transaksi" in t


def is_likely_record_command(text: str) -> bool:
    """True jika pesan benar-benar perintah catat transaksi."""
    if is_debt_inquiry(text) or is_skor_inquiry(text) or is_saran_inquiry(text) or is_hapus_command(text):
        return False
    t = (text or "").strip().lower()
    if re.search(r"\b(jual|beli)\b", t):
        return True
    if ("piutang" in t or "utang" in t or "prive" in t or "bayar" in t) and re.search(r"\d", t):
        return "belum bayar" not in t
    return False


def detect_intent_rules(text: str) -> str | None:
    """Jalur cepat untuk perintah sangat jelas."""
    t = (text or "").strip().lower()
    if not t:
        return None
    if is_debt_inquiry(t):
        return "PIUTANG"
    if is_skor_inquiry(t):
        return "SKOR"
    if is_saran_inquiry(t):
        return "SARAN"
    if is_hapus_command(t):
        return "HAPUS"
    if any(kw in t for kw in ("hapus transaksi", "hapus terakhir", "batal transaksi", "undo")):
        return "HAPUS"
    if re.search(r"\b(jual|beli)\b", t) and re.search(r"\d", t):
        return "CATAT"
    return None


def sanitize_intent(text: str, intent: str, classify_fn: Callable[[str], str]) -> str:
    """Paksa intent benar — pertanyaan tidak boleh CATAT."""
    if is_debt_inquiry(text):
        return "PIUTANG"
    if is_skor_inquiry(text):
        return "SKOR"
    if is_saran_inquiry(text):
        return "SARAN"
    if is_hapus_command(text):
        return "HAPUS"
    if intent == "CATAT" and not is_likely_record_command(text):
        alt = classify_fn(text)
        return alt if alt != "CATAT" else "LAINNYA"
    return intent


def is_outgoing_or_bot_echo(body: dict, phone: str, text: str, normalize_phone: Callable[[str], str]) -> bool:
    """Abaikan pesan keluar / echo balasan bot."""
    sender = normalize_phone(phone)
    device = normalize_phone(str(body.get("device") or ""))
    if device and sender and device == sender:
        return True
    for key in ("fromMe", "from_me", "isme", "is_me", "outgoing", "isOutgoing"):
        val = str(body.get(key) or "").lower()
        if val in ("1", "true", "yes", "outgoing"):
            return True
    t = (text or "").strip()
    if not t:
        return False
    if t[0] in "✅❌🤔💡🔥📋🗑️📦🛒📝👌📊📈💪💰💸🎉":
        return True
    lower = t.lower()
    if lower.startswith("• pemasukan") or lower.startswith("• pengeluaran"):
        return True
    return any(marker in lower for marker in BOT_TEXT_MARKERS)


def is_duplicate_inbound(
    body: dict, phone: str, text: str, normalize_phone: Callable[[str], str], window_sec: int = 30
) -> bool:
    """Debounce pesan identik dalam beberapa detik."""
    ts = str(body.get("timestamp") or body.get("id") or "")
    key = f"{normalize_phone(phone)}:{ts}:{text.strip().lower()[:160]}"
    now = time.time()
    stale = [k for k, t0 in _recent_inbound.items() if now - t0 > window_sec]
    for k in stale:
        _recent_inbound.pop(k, None)
    if key in _recent_inbound:
        return True
    _recent_inbound[key] = now
    return False


async def parse_webhook_body(request) -> dict:
    """Parse body webhook Fonnte (JSON, form, atau multipart)."""
    raw = await request.body()
    logger.debug("webhook raw (%d bytes): %r", len(raw), raw[:500])
    ctype = (request.headers.get("content-type") or "").lower()

    if raw:
        if "application/json" in ctype or raw[:1] in (b"{", b"["):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("json parse: %s", exc)
        if "application/x-www-form-urlencoded" in ctype or (b"=" in raw and b"&" in raw):
            try:
                parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
                return {k: (v[0] if isinstance(v, list) and v else v) for k, v in parsed.items()}
            except (UnicodeDecodeError, ValueError) as exc:
                logger.warning("urlencoded parse: %s", exc)

    if "multipart/form-data" in ctype:
        try:
            form = await request.form()
            if form:
                return {
                    k: (v if isinstance(v, str) else getattr(v, "filename", str(v)))
                    for k, v in form.items()
                }
        except (RuntimeError, ValueError) as exc:
            logger.warning("multipart parse: %s", exc)

    return {}


def extract_incoming(body: dict, wa_provider: str, normalize_phone: Callable[[str], str]) -> tuple[str, str, str, str, str | None]:
    """Ambil phone, text, media_type, media_url, inboxid dari payload."""
    if wa_provider == "fonnte":
        phone = body.get("member") or body.get("sender") or body.get("from") or body.get("phone") or ""
        text = (body.get("message") or body.get("text") or "").strip()
        media_url = body.get("url") or body.get("media_url") or ""
        ext = str(body.get("extension") or "").lower()
        inboxid = body.get("inboxid")
        media_type = ""
        if media_url:
            if ext in ("jpg", "jpeg", "png", "webp", "gif", "image"):
                media_type = "image"
            elif ext in ("ogg", "opus", "mp3", "m4a", "wav", "audio", "ptt"):
                media_type = "audio"
            else:
                media_type = "image"
    else:
        phone = body.get("phone") or body.get("sender") or ""
        text = body.get("message") or body.get("text") or ""
        media_type = body.get("type") or body.get("media_type") or ""
        media_url = body.get("media_url") or body.get("url") or ""
        inboxid = None
    return normalize_phone(phone), text, media_type, media_url, inboxid


async def is_safe_message(groq_client: Groq, text: str, model: str) -> bool:
    """Cek safeguard Groq — default izinkan jika API gagal."""
    try:
        res = groq_client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": (
                    "Apakah pesan ini aman dan relevan dengan pencatatan keuangan UMKM? "
                    f"Jawab YA atau TIDAK saja.\nPesan: {text}"
                ),
            }],
            temperature=0,
            max_tokens=5,
        )
        return "YA" in res.choices[0].message.content.upper()
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        logger.warning("is_safe_message fallback allow: %s", exc)
        return True
