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
    get_core,
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

# CS Webhook (BukuWarung AI Multi-Agent) — handle customer conversation
DEFAULT_CSAT_BASE_URL = "https://bukuwarung-ai-larisai.up.railway.app"

app = FastAPI(title=WA_BOT_TITLE)


# Global exception handler — pastikan TIDAK ADA error yang sampai ke user
# sebagai HTTP 500 tanpa body JSON. Fonnte butuh 200 + JSON untuk ACK webhook.
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> dict:
    logger.exception(
        "unhandled exception on %s %s: %s", request.method, request.url.path, exc
    )
    return {
        "status": "error",
        "detail": str(exc)[:500],
        "type": exc.__class__.__name__,
    }

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


def _csat_base_url() -> str:
    """URL bukuwarung-ai CS webhook (AI Multi-Agent)."""
    return (
        os.environ.get("BUKUWARUNG_BASE_URL", "").strip().rstrip("/")
        or DEFAULT_CSAT_BASE_URL
    )


async def _resolve_tenant_by_device(device: str) -> str | None:
    """Resolve tenant (client_id) dari nomor device Fonnte.

    Lookup di tabel `clients` Supabase: cari client yang punya device
    ini di metadata.device atau di owner_phones[0] (Fonnte device utama).

    Returns:
        client_id atau None kalau tidak ketemu.
    """
    if not device:
        return None

    digits = "".join(ch for ch in device if ch.isdigit())
    if digits.startswith("0"):
        digits = "62" + digits[1:]

    try:
        fonnte = get_fonnte()
        if not fonnte or not fonnte._db:
            return None

        def _lookup():
            try:
                return (
                    fonnte._db.table("clients")
                    .select("client_id, owner_phones, metadata")
                    .eq("is_active", True)
                    .execute()
                )
            except Exception:
                return None

        result = await asyncio.to_thread(_lookup)
        if not result or not result.data:
            return None
        for row in result.data:
            # Cek metadata.device
            meta = row.get("metadata") or {}
            if isinstance(meta, dict):
                meta_dev = meta.get("device") or meta.get("fonnte_device")
                if meta_dev and "".join(ch for ch in str(meta_dev) if ch.isdigit()).lstrip("0").lstrip("62").startswith(digits.lstrip("62")):
                    # Prefer metadata.user_id (UUID) untuk CS agent URL
                    if meta.get("user_id"):
                        return meta.get("user_id")
                    return row.get("client_id")
            # Fallback: owner_phones[0] cocok dengan device
            owners = row.get("owner_phones") or []
            for owner in owners:
                owner_norm = "".join(ch for ch in str(owner) if ch.isdigit())
                if owner_norm.startswith("0"):
                    owner_norm = "62" + owner_norm[1:]
                if owner_norm == digits:
                    # Prefer metadata.user_id (UUID) untuk CS agent URL
                    if isinstance(meta, dict) and meta.get("user_id"):
                        return meta.get("user_id")
                    return row.get("client_id")
    except Exception as exc:
        logger.warning("resolve_tenant_by_device gagal: %s", exc)

    return None


async def _ask_csat_agent(user_id: str, sender: str, text: str, name: str) -> str | None:
    """Forward customer message ke AI Multi-Agent (CS / Sales agent).

    Returns the agent's reply text, atau None kalau gagal.
    user_id di sini = `metadata.user_id` (UUID) dari tabel `clients`,
    BUKAN `client_id` string. CS agent route expects UUID.
    """
    url = f"{_csat_base_url()}/webhook/csat/{user_id}"
    payload = {
        "message": text or "",
        "sender": sender,
        "name": name or "",
        "channel": "wa",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
        if resp.status_code >= 400:
            logger.warning("csat webhook HTTP %s: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        # Beberapa versi return {reply: "..."} atau {intent: ...}
        reply = (
            data.get("reply")
            or data.get("response")
            or data.get("message")
            or data.get("text")
        )
        if not reply:
            # Kalau agent hanya detect intent, generate fallback berdasarkan intent
            intent = (data.get("intent") or "").lower()
            agent = (data.get("agent") or "").lower()
            if intent == "greeting":
                reply = (
                    "Halo! Selamat datang di Toko Rafih 👋\n"
                    "Ada yang bisa saya bantu? Silakan tanya produk, "
                    "harga, atau stok ya~"
                )
            elif intent in ("sales", "product_inquiry"):
                reply = (
                    "Tertarik dengan produk kami? Boleh tau barang yang "
                    "Anda cari? Saya bantu cek stok dan harganya ya 😊"
                )
            elif intent == "order":
                # Customer mau order — minta detail
                reply = (
                    "Siap! Boleh info detail ordernya ya:\n"
                    "• Nama barang\n"
                    "• Jumlah\n"
                    "• Alamat kirim (kalau perlu)\n\n"
                    "Nanti saya proses secepatnya 🙏"
                )
            elif intent == "cs" or agent in ("cs", "support"):
                reply = (
                    "Terima kasih sudah menghubungi kami 🙏\n"
                    "Boleh ceritakan keluhan atau pertanyaanmu?\n"
                    "Admin kami akan segera membantu 😊"
                )
            elif agent:
                reply = (
                    f"Terima kasih sudah menghubungi kami 🙏\n"
                    f"Silakan tunggu, admin kami akan segera membantu."
                )
        return reply
    except Exception as exc:  # noqa: BLE001
        logger.exception("csat forward failed: %s", exc)
        return None


async def send_wa_reply(
    phone: str,
    message: str,
    inboxid: str | None = None,
    device: str | None = None,
) -> None:
    """Kirim balasan WA — multi-tenant.

    Token Fonnte di-resolve otomatis per-nomor dari tabel `clients` Supabase.
    Fallback ke env `WA_API_KEY` kalau ada (backward compat untuk legacy).

    Untuk CUSTOMER (bukan owner), pass `device` = nomor Fonnte toko
    yang menerima pesan masuk, supaya balasan dikirim via device toko.
    """
    if not phone or not message:
        logger.warning("send_wa_reply: phone/message kosong")
        return
    try:
        client = get_fonnte()
        if device:
            # Customer (bukan owner) — pakai token device toko, bukan by customer phone
            token = await client.lookup_token_by_device(device)
            if not token:
                logger.error(
                    "send_wa_reply: tidak ada fonnte_token untuk device=%s", device
                )
                return
            # Kirim langsung via Fonnte API
            target = client._normalize_phone(phone)
            payload = {"target": target, "message": message}
            if inboxid:
                payload["inboxid"] = inboxid
            async with httpx.AsyncClient(timeout=30.0) as client_http:
                resp = await client_http.post(
                    "https://api.fonnte.com/send",
                    headers={"Authorization": token},
                    data=payload,
                )
            if resp.status_code >= 400:
                logger.error(
                    "send_wa_reply (by device=%s) gagal status=%s: %s",
                    device, resp.status_code, resp.text[:200],
                )
            else:
                logger.info("send_wa_reply: sent to %s via device=%s", target, device)
        else:
            await client.send_message(phone, message, inboxid=inboxid)
    except Exception as exc:  # noqa: BLE001 - tangkap SEMUA error supaya webhook tidak crash
        logger.exception("send_wa_reply failed: %s", exc)


async def detect_intent(text: str) -> str:
    # === Rule-based fast path untuk intent sederhana ===
    # (hemat LLM call kalau sudah jelas dari keyword)
    norm = (text or "").strip().lower()
    if norm in ("stok", "cek stok", "lihat stok", "stock"):
        return "STOK"
    if norm in ("produk", "list produk", "daftar produk", "semua produk", "apa saja yang dijual", "barang apa saja"):
        return "PRODUK"
    if norm in ("laporan", "laporan minggu ini", "laporan minggu", "rangkuman", "rekap"):
        return "LAPORAN"
    if norm.startswith("stok ") or norm.startswith("cek stok "):
        return "STOK"

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

    # Device Fonnte yang menerima pesan (untuk multi-tenant: kirim balasan
    # via device toko, bukan via device customer)
    device = body.get("device") or body.get("sender_device")

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
        await send_wa_reply(phone, reply, inboxid=inboxid, device=device)
        logged = _persist_wa_log(phone, text, reply)
        return {"status": "ok", "mode": "ping", "wa_logged": logged}

    reply = ""
    user_id = None

    try:
        try:
            user_id = resolve_user_id(phone)
        except ValueError:
            # Nomor tidak ada di wa_users → customer (bukan owner)
            user_id = None

        # === CUSTOMER (bukan owner) → forward ke CS AI Multi-Agent ===
        if not user_id and text:
            logger.info("customer message from %s (device=%s) — forwarding to CSAT", phone, device)
            # Resolve client_id dari device Fonnte (nomor toko yang menerima pesan)
            csat_tenant = None
            if device:
                try:
                    client_obj = get_fonnte()
                    # Lookup client yang punya device di metadata atau owner_phones
                    csat_tenant = await _resolve_tenant_by_device(device)
                except Exception as exc:
                    logger.warning("resolve_tenant_by_device gagal: %s", exc)
            if not csat_tenant:
                csat_tenant = "toko_rafih"  # fallback default tenant
            csat_reply = await _ask_csat_agent(
                user_id=csat_tenant,
                sender=phone,
                text=text,
                name=body.get("name", ""),
            )
            if csat_reply:
                reply = f"{bot_header()}\n\n{csat_reply}"
            else:
                reply = (
                    f"{bot_header()}\n\n"
                    f"Halo! 👋 Selamat datang di toko kami.\n"
                    f"Silakan tanya produk, harga, atau stok ya~\n"
                    f"Admin kami akan segera membantu 😊"
                )
            await asyncio.sleep(random_typing_delay())
            await send_wa_reply(phone, reply, inboxid=inboxid, device=device)
            return {"status": "ok", "mode": "cs_customer", "wa_logged": True}

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

            elif intent == "STOK":
                # Tampilkan produk dengan stok terendah / habis (sesuai design STOK)
                products = core.list_products(user_id, active_only=True) if hasattr(core, "list_products") else []
                if not products:
                    reply = (
                        f"{bot_header()}\n\n📦 Belum ada produk terdaftar.\n\n"
                        f"Tambah produk di dashboard dulu ya~"
                    )
                else:
                    lines = ["📦 *Stok Produk*", ""]
                    # Sort by stock ASC: yang paling kritis di atas
                    sorted_p = sorted(products, key=lambda p: (p.get("stock", 0) or 0, p.get("name", "").lower()))
                    for p in sorted_p[:15]:  # max 15 baris
                        name = p.get("name", "?")
                        stock = p.get("stock", 0) or 0
                        if stock <= 0:
                            emoji = "🔴"
                            label = "HABIS"
                        elif stock <= 5:
                            emoji = "🟡"
                            label = f"{stock} (tipis)"
                        else:
                            emoji = "🟢"
                            label = str(stock)
                        lines.append(f"{emoji} {name} — {label}")
                    reply = f"{bot_header()}\n\n" + "\n".join(lines)

            elif intent == "PRODUK":
                # List semua produk (aktif)
                products = core.list_products(user_id, active_only=False) if hasattr(core, "list_products") else []
                if not products:
                    reply = (
                        f"{bot_header()}\n\n📋 Belum ada produk.\n\n"
                        f"Tambah produk di dashboard dulu ya~"
                    )
                else:
                    lines = ["📋 *Daftar Produk*", ""]
                    for i, p in enumerate(products[:20], start=1):
                        name = p.get("name", "?")
                        price = p.get("price", 0) or 0
                        stock = p.get("stock", 0) or 0
                        is_active = p.get("is_active", True)
                        marker = "" if is_active else " _(non-aktif)_"
                        price_str = f"Rp {price:,.0f}".replace(",", ".")
                        lines.append(f"{i}. {name} — {price_str} (stok: {stock}){marker}")
                    reply = f"{bot_header()}\n\n" + "\n".join(lines)

            elif intent == "LAPORAN":
                # Rangkuman 7 hari terakhir
                try:
                    df = get_dashboard_data(user_id)
                    summary_lines = ["📊 *Laporan 7 Hari Terakhir*", ""]
                    if df is None or df.empty:
                        summary_lines.append("Belum ada transaksi 7 hari terakhir nih.")
                    else:
                        # Hitung total in/out dari kolom 'type' atau 'amount' (sesuai schema)
                        try:
                            if "type" in df.columns and "amount" in df.columns:
                                in_df = df[df["type"].astype(str).str.lower().isin(["in", "income", "masuk", "pemasukan"])]
                                out_df = df[df["type"].astype(str).str.lower().isin(["out", "expense", "keluar", "pengeluaran"])]
                                total_in = float(in_df["amount"].sum()) if not in_df.empty else 0.0
                                total_out = float(out_df["amount"].sum()) if not out_df.empty else 0.0
                            else:
                                total_in = 0.0
                                total_out = 0.0
                        except Exception:
                            total_in = 0.0
                            total_out = 0.0
                        profit = total_in - total_out
                        summary_lines.append(f"💰 Pemasukan: Rp {total_in:,.0f}".replace(",", "."))
                        summary_lines.append(f"💸 Pengeluaran: Rp {total_out:,.0f}".replace(",", "."))
                        summary_lines.append(f"📈 Margin: Rp {profit:,.0f}".replace(",", "."))
                        summary_lines.append("")
                        summary_lines.append(f"📝 Total transaksi: {len(df)}")
                    reply = f"{bot_header()}\n\n" + "\n".join(summary_lines)
                except Exception as exc:
                    logger.exception("LAPORAN error: %s", exc)
                    reply = (
                        f"{bot_header()}\n\n😅 Waduh, gagal ambil data laporan.\n"
                        f"Coba lagi nanti ya~"
                    )

            else:
                reply = (
                    f"{bot_header()}\n\n"
                    f"Hmm, {BOT_NAME} belum paham maksudmu 🤔\n\n"
                    f"Coba:\n• _jual kopi 50rb_\n• _stok_ — cek stok produk\n• _produk_ — list semua produk\n• _berapa skor_\n• _saran bisnis_\n• _hapus_"
                )
        else:
            reply = f"{bot_header()}\n\nKirim teks, foto struk, atau voice note ya~ 😊"

    except (RuntimeError, ValueError, KeyError, NameError, AttributeError, httpx.HTTPError) as exc:
        logger.exception("webhook error (known): %s", exc)
        reply = f"{bot_header()}\n\n😅 Waduh, ada gangguan sebentar.\nCoba kirim lagi ya~"
    except Exception as exc:  # noqa: BLE001 - last-resort safety net supaya tidak return HTTP 500
        logger.exception("webhook error (unexpected): %s", exc)
        reply = f"{bot_header()}\n\n😅 Waduh, ada error tak terduga.\nCoba kirim lagi ya~"

    await asyncio.sleep(random_typing_delay())
    await send_wa_reply(phone, reply, inboxid=inboxid, device=device)
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
