"""Logika bisnis bersama — dipakai app Streamlit & bot WhatsApp.

Streamlit: inisialisasi dengan **Supabase Anon Key** + `set_access_token(JWT)` agar RLS aktif.
Backend (main.py webhook): inject `supabase_client` service role lewat parameter konstruktor.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from groq import Groq
from supabase import Client, create_client

from log_config import get_logger

logger = get_logger(__name__)


class TenantScopeError(ValueError):
    """Dipanggil saat operasi DB tanpa user_id tenant yang valid."""


class LarisCore:
    """Akses data bisnis per-tenant. Semua query wajib memfilter `user_id`."""

    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        groq_api_key: str,
        *,
        supabase_client: Client | None = None,
    ):
        """`supabase_key` harus Anon Key untuk Streamlit.

        Untuk webhook/backend, pass `supabase_client` service role dari main.py
        (jangan simpan service key di Streamlit).
        """
        self.supabase_url = supabase_url
        self.supabase_key = supabase_key
        self.groq_client = Groq(api_key=groq_api_key)
        self._service_client = supabase_client is not None
        if supabase_client is not None:
            self.supabase = supabase_client
        else:
            self.supabase = create_client(supabase_url, supabase_key)

    @staticmethod
    def normalize_user_id(user_id: Any) -> str:
        return str(user_id).strip() if user_id else ""

    @classmethod
    def _require_user_id(cls, user_id: Any) -> str:
        uid = cls.normalize_user_id(user_id)
        if not uid:
            raise TenantScopeError("user_id wajib untuk operasi multi-tenant")
        return uid

    def _assert_service_client(self, op_name: str) -> None:
        if not self._service_client:
            raise TenantScopeError(
                f"{op_name} lintas-tenant membutuhkan supabase_client service role "
                "(inject dari main.py, bukan Anon Key Streamlit)"
            )

    @classmethod
    def from_service_client(
        cls,
        supabase_url: str,
        service_key: str,
        groq_api_key: str,
        *,
        supabase_client: Client | None = None,
    ) -> LarisCore:
        """Factory untuk backend/bot — bypass RLS via service role."""
        client = supabase_client or create_client(supabase_url, service_key)
        return cls(supabase_url, service_key, groq_api_key, supabase_client=client)

    def set_access_token(self, token: str):
        """Teruskan JWT user login ke PostgREST agar RLS mengenali auth.uid().

        Tanpa ini, query berjalan sebagai role anon dan akan terblokir RLS.
        Tidak dipakai oleh bot WhatsApp (bot memakai service_role yang bypass RLS).
        """
        if not token:
            return
        try:
            self.supabase.postgrest.auth(token)
        except Exception as exc:
            logger.error("set_access_token: %s", exc)

    def count_transactions(self, user_id: str) -> tuple[int, str | None]:
        """Hitung transaksi user (untuk diagnostik dashboard)."""
        try:
            uid = self._require_user_id(user_id)
        except TenantScopeError as exc:
            return 0, str(exc)
        try:
            resp = (
                self.supabase.table("transactions")
                .select("id", count="exact")
                .eq("user_id", uid)
                .execute()
            )
            return int(resp.count or 0), None
        except Exception as exc:
            return -1, str(exc)[:200]

    def get_dashboard_data(self, user_id: str) -> pd.DataFrame:
        uid = self._require_user_id(user_id)
        response = (
            self.supabase.table("transactions")
            .select("*")
            .eq("user_id", uid)
            .order("id", desc=True)
            .execute()
        )
        return pd.DataFrame(response.data) if response.data else pd.DataFrame()

    def create_client_account(self, email: str, password: str):
        """Buat akun client baru (Supabase Auth). Return (user_id, error_msg)."""
        try:
            # Client terpisah agar sesi admin yang sedang login tidak terganggu.
            tmp = create_client(self.supabase_url, self.supabase_key)
            res = tmp.auth.sign_up({"email": email, "password": password})
            user = getattr(res, "user", None)
            if user and getattr(user, "id", None):
                return user.id, None
            return None, "Gagal membuat akun. Cek format email / kemungkinan email sudah terdaftar."
        except Exception as exc:
            return None, str(exc)[:200]

    def list_wa_numbers(self, user_id: str) -> list[dict] | None:
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("wa_users")
                .select("*")
                .eq("user_id", uid)
                .order("id", desc=True)
                .execute()
            )
            return resp.data or []
        except Exception as exc:
            logger.error("list_wa_numbers user=%s: %s", uid, exc)
            return None

    def list_all_wa_numbers(self, user_id: str) -> list[dict] | None:
        """Backward-compatible alias — selalu scoped per user_id."""
        return self.list_wa_numbers(user_id)

    def admin_list_all_wa_numbers(self) -> list[dict] | None:
        """Lintas-tenant: hanya dengan service_role client (backend / super-admin tooling)."""
        self._assert_service_client("admin_list_all_wa_numbers")
        try:
            resp = self.supabase.table("wa_users").select("*").order("id", desc=True).execute()
            return resp.data or []
        except Exception as exc:
            logger.error("admin_list_all_wa_numbers: %s", exc)
            return None

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Normalisasi nomor WA: buang +, spasi, suffix WA; awalan 0 -> 62."""
        normalized = phone.replace("@s.whatsapp.net", "").strip().lstrip("+")
        normalized = "".join(ch for ch in normalized if ch.isdigit())
        if normalized.startswith("0"):
            normalized = "62" + normalized[1:]
        return normalized

    def link_wa_number(self, user_id: str, phone: str, label: str | None = None):
        """Hubungkan nomor WA ke seorang client (user_id). Upsert berdasarkan phone."""
        uid = self._require_user_id(user_id)
        normalized = self.normalize_phone(phone)
        if not normalized:
            raise ValueError("Nomor WA tidak valid.")
        data = {"phone": normalized, "user_id": uid, "label": label}
        return self.supabase.table("wa_users").upsert(data, on_conflict="phone").execute()

    def get_client_settings(self, user_id: str) -> dict | None:
        """Baca client_settings tenant (RLS: hanya baris milik user login)."""
        try:
            uid = self._require_user_id(user_id)
        except TenantScopeError:
            return None
        try:
            resp = (
                self.supabase.table("client_settings")
                .select("*")
                .eq("user_id", uid)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else None
        except Exception as exc:
            logger.error("get_client_settings user=%s: %s", uid, exc)
            return None

    def upsert_client_settings(self, user_id: str, row: dict):
        """Simpan / perbarui client_settings (on_conflict=user_id)."""
        return self.update_client_settings(user_id, row)

    def update_client_settings(self, user_id: str, settings_dict: dict):
        """Update pengaturan tenant di tabel client_settings."""
        uid = self._require_user_id(user_id)
        payload = dict(settings_dict)
        payload["user_id"] = uid
        payload["updated_at"] = datetime.now().isoformat()
        return self.supabase.table("client_settings").upsert(payload, on_conflict="user_id").execute()

    # ============================================================
    # Plan / Tier — Free / Pro / Bisnis / Kemitraan
    # Strategi 2026-07-16: Free = catat saja (AI Catat), Pro+ = unlock CS Agent
    # Pakai client_id (PK asli) bukan user_id (tidak ada di real schema)
    # ============================================================
    PLAN_TIERS = ("free", "pro", "bisnis", "kemitraan")
    # customer_chat = -1 artinya CS Agent DINONAKTIFKAN untuk tier ini
    PLAN_LIMITS = {
        "free":      {"tx": 100,  "customer_chat": -1,   "warehouses": 1,   "cs_agent": False},
        "pro":       {"tx": 1000, "customer_chat": 500,  "warehouses": 5,   "cs_agent": True},
        "bisnis":    {"tx": 10000,"customer_chat": 5000, "warehouses": 20,  "cs_agent": True},
        "kemitraan": {"tx": 999999, "customer_chat": 999999, "warehouses": 999, "cs_agent": True},
    }

    def get_plan_tier(self, client_id: str) -> str:
        """Ambil plan_tier dari tabel clients by client_id.

        Returns salah satu dari: 'free', 'pro', 'bisnis', 'kemitraan'.
        Default 'free' kalau row tidak ada / plan_expires_at sudah lewat.
        """
        if not client_id:
            return "free"
        try:
            resp = (
                self.supabase.table("clients")
                .select("plan_tier, plan_expires_at, is_active")
                .eq("client_id", client_id)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                return "free"
            tier = str(rows[0].get("plan_tier") or "free").strip().lower()
            if tier not in self.PLAN_TIERS:
                tier = "free"
            # Auto-downgrade ke free kalau plan_expires_at < NOW()
            expires_at = rows[0].get("plan_expires_at")
            if expires_at and tier != "free" and tier != "kemitraan":
                try:
                    exp_str = expires_at.replace("Z", "+00:00")
                    exp_dt = datetime.fromisoformat(exp_str)
                    now = datetime.now(datetime.timezone.utc)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=datetime.timezone.utc)
                    if exp_dt < now:
                        logger.info(
                            "get_plan_tier: plan %s sudah expire untuk client_id=%s, auto-free",
                            tier, client_id,
                        )
                        tier = "free"
                except Exception as exc:
                    logger.debug("get_plan_tier: parse expires_at gagal: %s", exc)
            # Kalau client non-aktif, treat sebagai free (kecuali kemitraan)
            if rows[0].get("is_active") is False and tier != "kemitraan":
                tier = "free"
            return tier
        except Exception as exc:
            logger.error("get_plan_tier client_id=%s: %s", client_id, exc)
            return "free"

    def get_plan_limits(self, client_id: str) -> dict:
        """Ambil limit tier user (untuk UI display & rate-limit check)."""
        tier = self.get_plan_tier(client_id)
        return {
            "tier": tier,
            **self.PLAN_LIMITS.get(tier, self.PLAN_LIMITS["free"]),
        }

    def has_cs_agent(self, client_id: str) -> bool:
        """Apakah tenant ini punya akses ke CS Agent (AI handle customer chat masuk)?

        Free: False (CS Agent nonaktif, hanya AI Catat)
        Pro/Bisnis/Kemitraan: True (CS Agent aktif)
        """
        try:
            limits = self.get_plan_limits(client_id)
            return bool(limits.get("cs_agent", False))
        except Exception:
            return False

    def upgrade_plan(
        self,
        client_id: str,
        new_tier: str,
        duration_days: int = 30,
    ) -> tuple[bool, str]:
        """Upgrade client ke tier baru (dipanggil manual admin setelah bukti transfer)."""
        if new_tier not in self.PLAN_TIERS:
            return False, f"tier tidak valid: {new_tier}"
        if not client_id:
            return False, "client_id kosong"
        try:
            payload = {
                "plan_tier": new_tier,
                "plan_started_at": datetime.now(datetime.timezone.utc).isoformat(),
                "plan_expires_at": (
                    datetime.now(datetime.timezone.utc) + timedelta(days=duration_days)
                ).isoformat(),
            }
            self.supabase.table("clients").update(payload).eq("client_id", client_id).execute()
            logger.info("upgrade_plan: client_id=%s -> %s (%d hari)", client_id, new_tier, duration_days)
            return True, f"Berhasil upgrade ke {new_tier} selama {duration_days} hari"
        except Exception as exc:
            logger.error("upgrade_plan client_id=%s: %s", client_id, exc)
            return False, str(exc)[:200]

    def check_tx_quota(self, client_id: str) -> dict:
        """Cek apakah client boleh catat transaksi bulan ini."""
        try:
            resp = (
                self.supabase.table("clients")
                .select("plan_tier, tx_count_this_month")
                .eq("client_id", client_id)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            tier = rows[0].get("plan_tier", "free") if rows else "free"
            count = rows[0].get("tx_count_this_month", 0) if rows else 0
        except Exception:
            tier, count = "free", 0
        limit = self.PLAN_LIMITS.get(tier, self.PLAN_LIMITS["free"])["tx"]
        return {
            "tier": tier,
            "current": count,
            "limit": limit,
            "allowed": count < limit,
            "remaining": max(0, limit - count),
        }

    def increment_tx_count(self, client_id: str) -> None:
        """Increment counter transaksi (called setelah catat sukses)."""
        if not client_id:
            return
        try:
            # Pakai raw query via PostgREST RPC kalau ada, fallback ke Python
            self.supabase.rpc("increment_tx_count", {"p_client_id": client_id}).execute()
        except Exception:
            # Fallback: select + update
            try:
                resp = (
                    self.supabase.table("clients")
                    .select("tx_count_this_month")
                    .eq("client_id", client_id)
                    .limit(1)
                    .execute()
                )
                rows = resp.data or []
                current = (rows[0].get("tx_count_this_month", 0) if rows else 0) + 1
                self.supabase.table("clients").update(
                    {"tx_count_this_month": current}
                ).eq("client_id", client_id).execute()
            except Exception as exc:
                logger.debug("increment_tx_count fallback gagal: %s", exc)

    @staticmethod
    def slugify_client_id(label: str, email: str = "") -> str:
        """Buat client_id aman untuk BukuWarung dari nama usaha atau email."""
        raw = (label or email.split("@")[0] or "toko").lower()
        slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        return (slug[:40] or "toko_client")

    def upsert_bukuwarung_client(
        self,
        *,
        client_id: str,
        name: str,
        wa_cs: str,
        wa_catat: str,
        user_id: str,
        bukuwarung_base_url: str,
        catat_bot_base_url: str,
    ) -> tuple[bool, str | None]:
        """Daftarkan / update client di tabel BukuWarung-AI (clients)."""
        if not self.table_exists("clients"):
            return False, "Tabel clients belum ada. Jalankan bukuwarung-ai/sql/create_clients.sql"
        cs = self.normalize_phone(wa_cs)
        catat = self.normalize_phone(wa_catat)
        bw_base = (bukuwarung_base_url or "").rstrip("/")
        catat_base = (catat_bot_base_url or "").rstrip("/")
        metadata = {
            "user_id": str(user_id),
            "wa_cs": cs,
            "wa_catat": catat,
            "whatsapp_cs_display": wa_cs.strip(),
            "whatsapp_catat_display": wa_catat.strip(),
            "webhook_cs": f"{bw_base}/webhook-whatsapp/{client_id}" if bw_base else "",
            "webhook_catat": f"{catat_base}/webhook" if catat_base else "",
            "pattern": "dual_number_3",
        }
        row = {
            "client_id": client_id,
            "name": name or client_id,
            "fonnte_token": "",
            "owner_phones": [catat],
            "profile_key": "ramah_warm",
            "products": [],
            "payment_methods": [],
            "is_active": True,
            "metadata": metadata,
        }
        try:
            self.supabase.table("clients").upsert(row, on_conflict="client_id").execute()
            if self.table_exists("brand_voices"):
                self.supabase.table("brand_voices").upsert(
                    {
                        "client_id": client_id,
                        "profile_key": "ramah_warm",
                        "greeting_style": "hangat",
                        "emoji_usage": 2,
                        "formality_level": 1,
                        "language_mix": "id",
                    },
                    on_conflict="client_id",
                ).execute()
            return True, None
        except Exception as exc:
            logger.error("upsert_bukuwarung_client: %s", exc)
            return False, str(exc)[:200]

    def setup_dual_wa_client(
        self,
        user_id: str,
        *,
        wa_cs: str,
        wa_catat: str,
        label: str,
        client_id: str | None = None,
        email: str = "",
        bukuwarung_base_url: str = "",
        catat_bot_base_url: str = "",
    ) -> dict:
        """Pola 3: nomor CS (pelanggan) + nomor Catat (owner) dalam satu langkah."""
        cid = (client_id or "").strip() or self.slugify_client_id(label, email)
        name = (label or cid).strip()
        catat_norm = self.normalize_phone(wa_catat)
        self.link_wa_number(user_id, wa_catat, f"{name} | AI Catat")
        ok, err = self.upsert_bukuwarung_client(
            client_id=cid,
            name=name,
            wa_cs=wa_cs,
            wa_catat=wa_catat,
            user_id=user_id,
            bukuwarung_base_url=bukuwarung_base_url,
            catat_bot_base_url=catat_bot_base_url,
        )
        bw_base = (bukuwarung_base_url or "").rstrip("/")
        catat_base = (catat_bot_base_url or "").rstrip("/")
        return {
            "client_id": cid,
            "user_id": user_id,
            "wa_cs": self.normalize_phone(wa_cs),
            "wa_catat": catat_norm,
            "webhook_cs": f"{bw_base}/webhook-whatsapp/{cid}" if bw_base else "",
            "webhook_catat": f"{catat_base}/webhook" if catat_base else "",
            "bukuwarung_ok": ok,
            "bukuwarung_error": err,
        }

    def list_bukuwarung_clients(self, user_id: str) -> list[dict] | None:
        """Daftar client BukuWarung milik satu tenant (metadata.user_id)."""
        uid = self._require_user_id(user_id)
        if not self.table_exists("clients"):
            return []
        try:
            resp = (
                self.supabase.table("clients")
                .select("client_id,name,owner_phones,metadata,is_active")
                .eq("metadata->>user_id", uid)
                .order("client_id")
                .execute()
            )
            return resp.data or []
        except Exception as exc:
            logger.error("list_bukuwarung_clients user=%s: %s", uid, exc)
            return None

    def admin_list_bukuwarung_clients(self) -> list[dict] | None:
        """Lintas-tenant: hanya dengan service_role client."""
        self._assert_service_client("admin_list_bukuwarung_clients")
        if not self.table_exists("clients"):
            return []
        try:
            resp = (
                self.supabase.table("clients")
                .select("client_id,name,owner_phones,metadata,is_active")
                .order("client_id")
                .execute()
            )
            return resp.data or []
        except Exception as exc:
            logger.error("admin_list_bukuwarung_clients: %s", exc)
            return None

    def unlink_wa_number(self, user_id: str, phone: str):
        uid = self._require_user_id(user_id)
        normalized = self.normalize_phone(phone)
        return (
            self.supabase.table("wa_users")
            .delete()
            .eq("phone", normalized)
            .eq("user_id", uid)
            .execute()
        )

    def resolve_user_id_by_phone(self, phone: str) -> str:
        """Petakan nomor WA ke user_id Supabase (routing webhook — butuh service role).

        Behavior:
            - Nomor ADA di wa_users → return user_id-nya.
            - Nomor TIDAK ADA di wa_users → raise ValueError (customer, bukan owner).
              JANGAN fallback ke WA_DEFAULT_USER_ID, karena itu akan membuat
              pesan customer diproses sebagai owner -> balasan "Admin AI"
              alih-alih di-forward ke CS Multi-Agent.
        """
        self._assert_service_client("resolve_user_id_by_phone")
        normalized = self.normalize_phone(phone)

        for candidate in {phone, normalized, f"+{normalized}"}:
            resp = (
                self.supabase.table("wa_users")
                .select("user_id")
                .eq("phone", candidate)
                .limit(1)
                .execute()
            )
            if resp.data:
                return resp.data[0]["user_id"]

        # Nomor tidak ditemukan di wa_users. Ini CUSTOMER, bukan owner.
        # Caller (webhook handler) akan route pesan ke CS Multi-Agent.
        # JANGAN fallback ke WA_DEFAULT_USER_ID — itu untuk development/testing saja
        # dan akan salah route customer sebagai owner.
        raise ValueError(
            f"Nomor {phone} belum terdaftar di wa_users. "
            "Pesan akan di-forward ke CS Multi-Agent sebagai customer."
        )

    def probe_table(self, table_name: str) -> str:
        """Status tabel untuk UI: ok | missing | stale_cache | denied | error."""
        try:
            self.supabase.table(table_name).select("*").limit(1).execute()
            return "ok"
        except BaseException as exc:
            err = str(exc).lower()
            name = table_name.lower()
            if "pgrst205" in err or "schema cache" in err:
                return "stale_cache"
            if f"'{name}'" in err or f'"{name}"' in err:
                if any(x in err for x in ("pgrst205", "could not find", "42p01", "does not exist")):
                    return "missing"
            if "column" in err and "does not exist" in err:
                return "ok"
            if any(x in err for x in ("permission denied", "42501", "jwt", "401", "403")):
                return "denied"
            logger.error("probe_table(%s): %s", table_name, exc)
            # Jangan blokir UI jika error tidak jelas — tabel mungkin sudah ada.
            return "ok"

    def table_exists(self, table_name: str) -> bool:
        """Cek apakah tabel ada — toleran RLS / cache schema."""
        return self.probe_table(table_name) in ("ok", "denied", "error", "stale_cache")

    def db_insert_transaction(
        self, user_id: str, type_txn, category, amount, note, is_prive=False
    ):
        uid = self._require_user_id(user_id)
        prev = (
            self.supabase.table("transactions")
            .select("running_balance")
            .eq("user_id", uid)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        last_balance = prev.data[0]["running_balance"] if prev.data else 0
        new_balance = last_balance + amount if type_txn == "Pemasukan" else last_balance - amount
        prefix = "PRV" if is_prive else ("KM" if type_txn == "Pemasukan" else "KK")
        today = datetime.now().strftime("%y%m%d")
        count_resp = (
            self.supabase.table("transactions")
            .select("id", count="exact")
            .eq("user_id", uid)
            .like("date", f"{today}%")
            .execute()
        )
        seq = (count_resp.count or 0) + 1
        receipt_no = f"{prefix}-{today}-{seq:03d}"
        data = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "type": type_txn,
            "category": category,
            "amount": amount,
            "note": note,
            "receipt_no": receipt_no,
            "running_balance": new_balance,
            "is_prive": is_prive,
            "user_id": uid,
        }
        return self.supabase.table("transactions").insert(data).execute()

    def save_transaction(
        self, user_id: str, type_txn, category, amount, note, is_prive=False
    ):
        """Alias `db_insert_transaction` — selalu scoped per user_id."""
        return self.db_insert_transaction(user_id, type_txn, category, amount, note, is_prive=is_prive)

    def get_balance(self, user_id: str) -> float:
        """Saldo berjalan terakhir dari buku kas tenant."""
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("transactions")
                .select("running_balance")
                .eq("user_id", uid)
                .order("id", desc=True)
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return float(rows[0].get("running_balance") or 0) if rows else 0.0
        except Exception as exc:
            logger.error("get_balance user=%s: %s", uid, exc)
            return 0.0

    def recalculate_running_balance(self, user_id: str):
        """Hitung ulang running_balance semua transaksi user (urut kronologis/id asc)."""
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("transactions")
                .select("id, type, amount")
                .eq("user_id", uid)
                .order("id", desc=False)
                .execute()
            )
            rows = resp.data or []
            balance = 0
            for r in rows:
                amt = r.get("amount") or 0
                balance = balance + amt if r.get("type") == "Pemasukan" else balance - amt
                (
                    self.supabase.table("transactions")
                    .update({"running_balance": balance})
                    .eq("id", r["id"])
                    .eq("user_id", uid)
                    .execute()
                )
            return balance
        except Exception as exc:
            logger.error("recalculate_running_balance user=%s: %s", uid, exc)
            return None

    def db_update_transaction(self, user_id, txn_id, type_txn, category, amount, note):
        uid = self._require_user_id(user_id)
        (
            self.supabase.table("transactions")
            .update({"type": type_txn, "category": category, "amount": amount, "note": note})
            .eq("id", txn_id)
            .eq("user_id", uid)
            .execute()
        )
        self.recalculate_running_balance(uid)

    def db_delete_transaction(self, user_id, txn_id):
        uid = self._require_user_id(user_id)
        (
            self.supabase.table("transactions")
            .delete()
            .eq("id", txn_id)
            .eq("user_id", uid)
            .execute()
        )
        self.recalculate_running_balance(uid)

    # --------------------
    # Warehouses / Inventory
    # --------------------
    def create_warehouse(self, user_id: str, name: str, location: str | None = None, notes: str | None = None):
        uid = self._require_user_id(user_id)
        data = {
            "user_id": uid,
            "name": name,
            "location": location,
            "notes": notes,
            "created_at": datetime.now().isoformat(),
        }
        return self.supabase.table("warehouses").insert(data).execute()

    def list_warehouses(self, user_id: str):
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("warehouses")
                .select("*")
                .eq("user_id", uid)
                .order("id", desc=False)
                .execute()
            )
            return resp.data or []
        except BaseException as exc:
            logger.error("list_warehouses user=%s: %s", uid, exc)
            return None

    def update_warehouse(self, user_id: str, warehouse_id: int, **fields):
        uid = self._require_user_id(user_id)
        try:
            return (
                self.supabase.table("warehouses")
                .update(fields)
                .eq("id", warehouse_id)
                .eq("user_id", uid)
                .execute()
            )
        except BaseException as exc:
            logger.error("update_warehouse user=%s: %s", uid, exc)
            return None

    def delete_warehouse(self, user_id: str, warehouse_id: int):
        uid = self._require_user_id(user_id)
        try:
            return (
                self.supabase.table("warehouses")
                .delete()
                .eq("id", warehouse_id)
                .eq("user_id", uid)
                .execute()
            )
        except BaseException as exc:
            logger.error("delete_warehouse user=%s: %s", uid, exc)
            return None

    def add_inventory_entry(
        self, user_id: str, warehouse_id: int, barang: str, qty_in: int = 0, qty_out: int = 0, note: str | None = None
    ):
        uid = self._require_user_id(user_id)
        data = {
            "user_id": uid,
            "warehouse_id": warehouse_id,
            "barang": barang,
            "qty_in": int(qty_in or 0),
            "qty_out": int(qty_out or 0),
            "note": note,
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        try:
            res = self.supabase.table("inventory_entries").insert(data).execute()
            self.sync_product_from_inventory(uid, barang, int(qty_in or 0), int(qty_out or 0))
            return res
        except Exception as exc:
            logger.error("add_inventory_entry user=%s: %s", uid, exc)
            return None

    def list_inventory(self, user_id: str, warehouse_id: int | None = None):
        uid = self._require_user_id(user_id)
        try:
            q = self.supabase.table("inventory_entries").select("*").eq("user_id", uid)
            if warehouse_id is not None:
                q = q.eq("warehouse_id", warehouse_id)
            resp = q.order("id", desc=True).execute()
            return resp.data or []
        except Exception as exc:
            logger.error("list_inventory user=%s: %s", uid, exc)
            return None

    def sync_product_from_inventory(self, user_id: str, barang: str, qty_in: int, qty_out: int) -> int | None:
        """Update/insert stok produk berdasarkan entri gudang."""
        name = (barang or "").strip()
        if not name:
            return None
        delta = int(qty_in or 0) - int(qty_out or 0)
        uid = self._require_user_id(user_id)
        try:
            current = (
                self.supabase.table("products")
                .select("id, name, stock")
                .eq("user_id", uid)
                .ilike("name", name)
                .limit(1)
                .execute()
            )
            rows = current.data or []
            if rows:
                row = rows[0]
                new_stock = max(0, int(row.get("stock") or 0) + delta)
                (
                    self.supabase.table("products")
                    .update({"stock": new_stock})
                    .eq("id", row["id"])
                    .eq("user_id", uid)
                    .execute()
                )
                return new_stock

            init_stock = max(0, delta)
            (
                self.supabase.table("products")
                .insert({"user_id": uid, "name": name, "stock": init_stock})
                .execute()
            )
            return init_stock
        except Exception as exc:
            logger.error("sync_product_from_inventory user=%s: %s", uid, exc)
            return None

    def list_products(self, user_id: str, active_only: bool = True):
        """Daftar produk + stok terkini milik tenant.

        Args:
            user_id: UUID tenant pemilik produk.
            active_only: kalau True, hanya tampilkan produk dengan is_active=True.
        """
        uid = self._require_user_id(user_id)
        try:
            # Select field selengkap mungkin: price, category, is_active dipakai
            # untuk render di WA bot (/stok, /produk) dan dashboard Streamlit.
            # Fallback: kalau kolom is_active tidak ada / query gagal,
            # ambil semua row tanpa filter (lebih baik tampil dari pada kosong).
            try:
                query = (
                    self.supabase.table("products")
                    .select("id, name, price, stock, category, is_active, created_at")
                    .eq("user_id", uid)
                )
                if active_only:
                    query = query.eq("is_active", True)
                resp = query.order("name", desc=False).execute()
                return resp.data or []
            except Exception:
                # Fallback path: tanpa filter is_active (mis. kolom belum ada / always-null)
                resp = (
                    self.supabase.table("products")
                    .select("id, name, price, stock, category, is_active, created_at")
                    .eq("user_id", uid)
                    .order("name", desc=False)
                    .execute()
                )
                return resp.data or []
        except Exception as exc:
            logger.error("list_products user=%s: %s", uid, exc)
            return None

    def get_products(self, user_id: str):
        """Alias `list_products`."""
        return self.list_products(user_id)

    # --------------------
    # Approvals (Ruang Komando / Proactive UI)
    # --------------------
    def create_approval(self, user_id: str, agent_id: str, action_type: str, summary: str, payload: dict | None = None):
        uid = self._require_user_id(user_id)
        data = {
            "user_id": uid,
            "agent_id": agent_id,
            "action_type": action_type,
            "summary": summary,
            "payload": payload or {},
            "status": "PENDING",
        }
        try:
            return self.supabase.table("approvals").insert(data).execute()
        except Exception as exc:
            logger.error("create_approval user=%s: %s", uid, exc)
            return None

    def list_pending_approvals(self, user_id: str):
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("approvals")
                .select("*")
                .eq("user_id", uid)
                .eq("status", "PENDING")
                .order("id", desc=True)
                .execute()
            )
            return resp.data or []
        except Exception as exc:
            logger.error("list_pending_approvals user=%s: %s", uid, exc)
            return None

    def update_approval_status(self, user_id: str, approval_id, status: str):
        uid = self._require_user_id(user_id)
        if status not in ("APPROVED", "REJECTED"):
            raise ValueError("status harus APPROVED atau REJECTED")
        try:
            return (
                self.supabase.table("approvals")
                .update({"status": status, "updated_at": datetime.now().isoformat()})
                .eq("id", approval_id)
                .eq("user_id", uid)
                .execute()
            )
        except Exception as exc:
            logger.error("update_approval_status user=%s: %s", uid, exc)
            return None

    # --------------------
    # Logistik (cek stok & saran restock)
    # --------------------
    def find_product_row(self, user_id: str, name_hint: str):
        """Cari baris produk terdekat (nama fuzzy). Return dict atau None."""
        hint = (name_hint or "").strip()
        if not hint:
            return None
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("products")
                .select("id, name, price, stock")
                .eq("user_id", uid)
                .ilike("name", f"%{hint}%")
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            return rows[0] if rows else None
        except Exception as exc:
            logger.error("find_product_row user=%s: %s", uid, exc)
            return None

    def get_product_stock(self, user_id: str, product: str):
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("products")
                .select("stock")
                .eq("user_id", uid)
                .ilike("name", f"%{product}%")
                .execute()
            )
            rows = resp.data or []
            stock = sum((r.get("stock") or 0) for r in rows)
            return stock, len(rows)
        except Exception as exc:
            logger.error("get_product_stock user=%s: %s", uid, exc)
            return 0, 0

    def adjust_product_stock(self, user_id: str, product: str, delta: int):
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("products")
                .select("id, stock")
                .eq("user_id", uid)
                .ilike("name", f"%{product}%")
                .limit(1)
                .execute()
            )
            rows = resp.data or []
            if not rows:
                return None
            row = rows[0]
            new_stock = max(0, (row.get("stock") or 0) + delta)
            (
                self.supabase.table("products")
                .update({"stock": new_stock})
                .eq("id", row["id"])
                .eq("user_id", uid)
                .execute()
            )
            return new_stock
        except Exception as exc:
            logger.error("adjust_product_stock user=%s: %s", uid, exc)
            return None

    def resolve_sale_quantity(
        self, user_id: str, product_name: str, raw_text: str, amount: int, unit_price: int | None
    ) -> int:
        """Hitung unit terjual: dari teks (jual kopi 5) atau nominal / harga (35000 / 3500 = 10)."""
        parsed = self.ai_logistik_parse(raw_text) or {}
        parsed_name = (parsed.get("product") or "").strip().lower()
        target = (product_name or "").strip().lower()
        qty = max(0, int(parsed.get("qty") or 0))

        # Pakai qty dari AI hanya jika masuk akal (bukan nominal rupiah).
        if qty > 0 and qty <= 500:
            if not parsed_name or parsed_name in target or target in parsed_name:
                return qty

        price = unit_price or 0
        if not price and target:
            row = self.find_product_row(user_id, target)
            price = int(row.get("price") or 0) if row else 0

        if price > 0 and amount > 0:
            inferred = int(round(amount / price))
            return max(1, inferred) if inferred > 0 else 0
        return qty if 0 < qty <= 500 else 0

    def resolve_product_for_sale(self, user_id: str, category: str, raw_text: str):
        """Cocokkan transaksi ke baris products (nama fuzzy / katalog / teks asli)."""
        hints = [category]
        parsed = self.ai_logistik_parse(raw_text) or {}
        pname = str(parsed.get("product") or "").strip()
        if pname:
            hints.append(pname)

        for hint in hints:
            if not hint:
                continue
            row = self.find_product_row(user_id, hint)
            if row:
                return row

        uid = self._require_user_id(user_id)
        haystack = f"{category} {raw_text}".lower()
        try:
            resp = (
                self.supabase.table("products")
                .select("id, name, price, stock")
                .eq("user_id", uid)
                .execute()
            )
            for row in resp.data or []:
                name = (row.get("name") or "").strip()
                if name and name.lower() in haystack:
                    return row
        except Exception as exc:
            logger.error("resolve_product_for_sale: %s", exc)
        return None

    def run_logistik_after_sale(
        self,
        user_id: str,
        transaction: dict,
        raw_text: str,
        *,
        stock_threshold: int = 10,
        reorder_qty: int = 20,
    ) -> dict | None:
        """Logistik AI: kurangi stok gudang setelah penjualan; buat approval jika kritis."""
        category = str(transaction.get("category") or transaction.get("note") or "").strip()
        amount = int(transaction.get("amount") or 0)
        if not category:
            parsed = self.ai_logistik_parse(raw_text) or {}
            category = str(parsed.get("product") or "").strip()
        if not category:
            return None

        row = self.resolve_product_for_sale(user_id, category, raw_text)
        if not row:
            return {
                "message": (
                    f"📦 *Logistik AI:* Produk `{category}` belum ada di katalog gudang. "
                    f"Tambahkan di Supabase/products agar stok terpantau."
                ),
                "stock_updated": False,
            }

        product = row["name"]
        old_stock = int(row.get("stock") or 0)
        unit_price = int(row.get("price") or 0)
        qty = self.resolve_sale_quantity(user_id, product, raw_text, amount, unit_price)
        if qty <= 0:
            return {
                "message": (
                    f"📦 *Logistik AI:* Penjualan {product} tercatat, "
                    f"tapi jumlah unit tidak terbaca. Coba: _jual {product} 5_ "
                    f"atau nominal kelipatan harga Rp {unit_price:,}."
                ),
                "stock_updated": False,
            }

        new_stock = self.adjust_product_stock(user_id, product, -qty)
        if new_stock is None:
            return None

        price_txt = f" @ Rp {unit_price:,}" if unit_price else ""
        msg = (
            f"📦 *Logistik AI:* Stok *{product}*: {old_stock} → {new_stock} "
            f"(-{qty} unit{price_txt})"
        )

        approval_msg = ""
        if new_stock < stock_threshold:
            summary = (
                f"Stok {product} tinggal {new_stock} (ambang {stock_threshold}). "
                f"Saran pesan {reorder_qty} unit ke supplier."
            )
            self.create_approval(
                user_id,
                agent_id="logistik",
                action_type="create_po",
                summary=summary,
                payload={
                    "product": product,
                    "current_stock": new_stock,
                    "reorder_qty": reorder_qty,
                    "unit_price": unit_price,
                },
            )
            approval_msg = "\n⚠️ Stok menipis — buka *Ruang Komando* untuk Setujui/Tolak PO."

        return {
            "message": msg + approval_msg,
            "stock_updated": True,
            "product": product,
            "qty_sold": qty,
            "old_stock": old_stock,
            "new_stock": new_stock,
        }

    def ai_logistik_parse(self, text: str) -> dict:
        """Ekstrak {product, qty} dari teks penjualan, mis. 'jual indomie 5'. None jika gagal."""
        prompt = (
            f'Dari teks penjualan warung "{text}", ekstrak nama produk dan jumlah UNIT (bukan rupiah). '
            'Contoh: "jual kopi 5" → qty 5. "jual kopi 35000" jika 35000 nominal → qty 0. '
            'Balas HANYA JSON: {"product": "nama", "qty": angka_unit}. '
            'Jika tidak jelas: {"product": null, "qty": 0}.'
        )
        try:
            res = self.groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="openai/gpt-oss-120b",
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            data = json.loads(res.choices[0].message.content)
            if not data.get("product"):
                return None
            return {"product": str(data["product"]).strip(), "qty": int(data.get("qty") or 0)}
        except Exception:
            return None

    # --------------------
    # Log percakapan WhatsApp (opsional, untuk Chat History)
    # --------------------
    def log_wa_message(
        self, user_id: str, role: str, content: str, phone: str | None = None, agent_id: str | None = None
    ):
        uid = self._require_user_id(user_id)
        data = {"user_id": uid, "role": role, "content": content, "phone": phone, "agent_id": agent_id}
        try:
            result = self.supabase.table("wa_messages").insert(data).execute()
            if not getattr(result, "data", None):
                logger.warning("log_wa_message user=%s: insert tanpa data balik", uid)
            return result
        except Exception as exc:
            logger.error(
                "log_wa_message user=%s: %s | payload: %s",
                uid,
                exc,
                {**data, "content": (content or "")[:80]},
            )
            return None

    def list_wa_messages(self, user_id: str, limit: int = 30):
        uid = self._require_user_id(user_id)
        try:
            resp = (
                self.supabase.table("wa_messages")
                .select("*")
                .eq("user_id", uid)
                .order("id", desc=True)
                .limit(limit)
                .execute()
            )
            rows = resp.data or []
            return list(reversed(rows))
        except Exception as exc:
            logger.error("list_wa_messages user=%s: %s", uid, exc)
            return []

    def delete_last_transaction(self, user_id: str):
        uid = self._require_user_id(user_id)
        last = (
            self.supabase.table("transactions")
            .select("id, note, amount")
            .eq("user_id", uid)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        if not last.data:
            return None
        txn = last.data[0]
        self.db_delete_transaction(uid, txn["id"])
        return txn

    @staticmethod
    def clean_json_response(text: str) -> str:
        match = re.search(r"```(?:json)?\n(.*?)\n```", text, re.DOTALL)
        if match:
            return match.group(1)
        if not text.strip().startswith("["):
            return f"[{text}]"
        return text

    @staticmethod
    def _parse_transactions(content: str) -> list:
        """Parse respons AI jadi list transaksi, tahan terhadap bentuk objek/array."""
        try:
            data = json.loads(content)
        except Exception as exc:
            logger.error("parse transaksi (json): %s | raw: %s", exc, str(content)[:160])
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("transactions", "data", "items", "result", "transaksi"):
                if isinstance(data.get(key), list):
                    return [d for d in data[key] if isinstance(d, dict)]
            # Objek transaksi tunggal
            if "amount" in data and "type" in data:
                return [data]
        return []

    def ai_extractor_agent(self, text: str) -> list:
        prompt = (
            f'Anda akuntan warung Indonesia. Teks user: "{text}"\n\n'
            "Jika ini PERTANYAAN (skor, saran, siapa belum bayar, dll.) — balas {\"transactions\": []}.\n"
            "Hanya ekstrak jika user jelas MENCATAT transaksi jual/beli/bayar/piutang dengan nominal.\n"
            'Balas HANYA JSON: {"transactions":[...]} tiap item '
            '{"type":"Pemasukan|Pengeluaran","amount":angka,"category":"...","note":"..."}.'
        )
        try:
            res = self.groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="openai/gpt-oss-120b",
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            return self._parse_transactions(res.choices[0].message.content)
        except Exception as exc:
            logger.error("ai_extractor_agent: %s", exc)
            return []

    def vision_extractor_agent_from_b64(self, b64: str) -> list:
        prompt = (
            "Baca struk belanja warung Indonesia. Ambil HANYA nilai GRAND TOTAL / TOTAL akhir "
            "(jangan rincian per item agar tidak dobel). "
            'Balas HANYA objek JSON dengan key "transactions" berisi TEPAT SATU item: '
            '{"transactions":[{"type":"Pengeluaran","amount":<grand total>,"category":"Bahan Baku","note":"ringkasan belanja"}]}. '
            'Jika total tidak terbaca, balas {"transactions": []}.'
        )
        # Deteksi tipe gambar dari header base64 (PNG vs JPEG) agar mime cocok.
        mime = "image/png" if b64.startswith("iVBOR") else "image/jpeg"
        try:
            res = self.groq_client.chat.completions.create(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ],
                    }
                ],
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            return self._parse_transactions(res.choices[0].message.content)
        except Exception as exc:
            logger.error("vision_extractor_agent: %s", exc)
            return []

    def vision_extractor_agent_from_upload(self, uploaded_file) -> list:
        b64 = base64.b64encode(uploaded_file.getvalue()).decode("utf-8")
        return self.vision_extractor_agent_from_b64(b64)

    def voice_extractor_agent_from_bytes(self, audio_bytes: bytes, filename="rec.wav") -> list:
        try:
            trans = self.groq_client.audio.transcriptions.create(
                file=(filename, audio_bytes),
                model="whisper-large-v3-turbo",
                language="id",
                response_format="text",
            )
            return self.ai_extractor_agent(trans)
        except Exception:
            return []

    def voice_extractor_agent_from_file(self, audio_file) -> list:
        return self.voice_extractor_agent_from_bytes(audio_file.read(), audio_file.name)

    @staticmethod
    def calculate_laris_score(df: pd.DataFrame) -> dict:
        if df.empty:
            return {"score": 0, "insight": "Mulai catat transaksi pertama Anda!", "level": "low"}
        income = df[df["type"] == "Pemasukan"]["amount"].sum()
        expense = df[df["type"] == "Pengeluaran"]["amount"].sum()
        profit = income - expense
        margin_score = min(40, max(0, (profit / income) * 80)) if income > 0 else 0
        df_c = df.copy()
        df_c["date"] = pd.to_datetime(df_c["date"])
        last_30 = df_c[df_c["date"] >= (datetime.now() - timedelta(days=30))]
        consistency_score = min(30, (last_30["date"].dt.date.nunique() / 30) * 30)
        utang = df[df["category"].str.contains("utang|kasbon|piutang", case=False, na=False)][
            "amount"
        ].sum()
        debt_score = max(0, 20 - ((utang / income) * 40)) if income > 0 else 10
        volume_score = min(10, len(last_30) * 0.5)
        total = int(min(100, max(0, margin_score + consistency_score + debt_score + volume_score)))
        if total >= 75:
            lv, ins = "high", ["Warung sangat sehat! 🔥", "Margin konsisten 💪", "Siap ekspansi? 🚀"]
        elif total >= 45:
            lv, ins = "mid", ["Tingkatkan pencatatan 📝", "Evaluasi harga 💡", "Perhatikan bocoran 🔍"]
        else:
            lv, ins = "low", ["Evaluasi biaya ⚠️", "Rapikan pencatatan 💪", "Kurangi stok mati 📉"]
        return {"score": total, "insight": random.choice(ins), "level": lv}

    # --------------------
    # AI Memory — incremental learning untuk CS & Catat agents
    # --------------------
    # Schema tabel otak_memories (Supabase):
    #   id              uuid
    #   user_id         text
    #   content         text          — Q&A atau memory content (gabungan)
    #   embedding       jsonb         — vector embedding (untuk similarity)
    #   timestamp       timestamptz
    #   feedback_score  double        — rating (-1.0 s/d 1.0)
    #   weight          double        — bobot relevancy
    #   status          text          — 'active' / 'archived' / 'invalid'
    #   metadata        jsonb         — flexible: {agent_type, question, hit_count, ...}
    @staticmethod
    def _normalize_question(q: str) -> str:
        return " ".join((q or "").lower().strip().split())

    def recall_memory(
        self, user_id: str, agent_type: str, question: str
    ) -> str | None:
        """Cari jawaban dari otak_memories.

        Returns:
            String jawaban kalau ditemukan di memory, else None.

        Logic:
            1. Query otak_memories WHERE user_id=uid AND status='active'.
            2. Filter di Python dengan metadata.agent_type + normalized question
               exact match (sederhana, tanpa embedding similarity).
            3. Kalau ketemu: bump weight (×1.05) + increment hit_count.
        """
        uid = self._require_user_id(user_id)
        if not question:
            return None
        norm_q = self._normalize_question(question)
        if not norm_q:
            return None
        try:
            res = (
                self.supabase.table("otak_memories")
                .select("id, content, metadata, weight, timestamp")
                .eq("user_id", uid)
                .eq("status", "active")
                .execute()
            )
            rows = res.data or []
            # Cari entri dengan agent_type match + question normalized sama
            for row in rows:
                meta = row.get("metadata") or {}
                if not isinstance(meta, dict):
                    continue
                if meta.get("agent_type") != agent_type:
                    continue
                if self._normalize_question(meta.get("question", "")) != norm_q:
                    continue
                # HIT! Increment weight + hit_count (learning)
                try:
                    new_weight = (row.get("weight") or 1.0) * 1.05
                    new_hits = int(meta.get("hit_count") or 0) + 1
                    new_meta = dict(meta)
                    new_meta["hit_count"] = new_hits
                    new_meta["last_used_at"] = datetime.now().isoformat()
                    self.supabase.table("otak_memories").update({
                        "weight": new_weight,
                        "metadata": new_meta,
                        "timestamp": datetime.now().isoformat(),
                    }).eq("id", row["id"]).execute()
                except Exception as exc:
                    logger.debug("recall_memory update weight: %s", exc)
                # Extract answer dari content (format: "Q: ... | A: ...")
                content = row.get("content", "")
                if " | A: " in content:
                    return content.split(" | A: ", 1)[1].strip()
                return content
            return None
        except Exception as exc:
            logger.error("recall_memory user=%s: %s", uid, exc)
            return None

    def remember_answer(
        self,
        user_id: str,
        agent_type: str,
        question: str,
        answer: str,
    ) -> bool:
        """Simpan Q&A ke otak_memories untuk pembelajaran agent.

        Content = 'Q: {question} | A: {answer}'
        Metadata = {agent_type, question (original), hit_count: 1, created_at}

        Args:
            user_id: UUID tenant
            agent_type: 'cs' | 'catat' | 'admin'
            question: pertanyaan user
            answer: jawaban dari LLM
        """
        uid = self._require_user_id(user_id)
        if not agent_type or not question or not answer:
            return False
        norm_q = self._normalize_question(question)
        if not norm_q or not (answer or "").strip():
            return False
        try:
            # Cek apakah sudah ada (exact match)
            existing = self.recall_memory(uid, agent_type, question)
            if existing is not None:
                logger.debug("remember_answer: already in memory")
                return False
            data = {
                "user_id": uid,
                "content": f"Q: {norm_q} | A: {answer.strip()}",
                "embedding": [],  # placeholder; diisi kemudian via embedding service
                "timestamp": datetime.now().isoformat(),
                "feedback_score": 0.0,
                "weight": 1.0,
                "status": "active",
                "metadata": {
                    "agent_type": agent_type,
                    "question": question,
                    "hit_count": 1,
                    "created_at": datetime.now().isoformat(),
                },
            }
            self.supabase.table("otak_memories").insert(data).execute()
            logger.info(
                "remember_answer: stored user=%s agent=%s q='%s' (ans_len=%d)",
                uid, agent_type, norm_q[:50], len(answer),
            )
            return True
        except Exception as exc:
            logger.error("remember_answer user=%s: %s", uid, exc)
            return False

    def get_memory_stats(self, user_id: str) -> dict:
        """Statistik otak_memories per agent_type.

        Returns: dict seperti
            {
              "cs":    {"count": 5, "total_weight": 5.4, "avg_score": 0.1},
              "catat": {"count": 3, "total_weight": 3.0, "avg_score": 0.0},
              "admin": {"count": 0, "total_weight": 0.0, "avg_score": 0.0},
              "total": 8,
            }
        """
        uid = self._require_user_id(user_id)
        try:
            res = (
                self.supabase.table("otak_memories")
                .select("metadata, weight, feedback_score, status")
                .eq("user_id", uid)
                .eq("status", "active")
                .execute()
            )
            rows = res.data or []
            stats: dict = {
                "cs": {"count": 0, "total_weight": 0.0, "avg_score": 0.0},
                "catat": {"count": 0, "total_weight": 0.0, "avg_score": 0.0},
                "admin": {"count": 0, "total_weight": 0.0, "avg_score": 0.0},
                "total": 0,
            }
            for r in rows:
                meta = r.get("metadata") or {}
                if not isinstance(meta, dict):
                    continue
                ag = meta.get("agent_type", "cs")
                if ag not in stats or not isinstance(stats[ag], dict):
                    stats[ag] = {"count": 0, "total_weight": 0.0, "avg_score": 0.0}
                stats[ag]["count"] += 1
                stats[ag]["total_weight"] += float(r.get("weight") or 0)
                stats[ag]["avg_score"] += float(r.get("feedback_score") or 0)
                stats["total"] += 1
            # Hitung rata-rata score
            for ag_key, ag_stat in stats.items():
                if isinstance(ag_stat, dict) and ag_stat["count"] > 0:
                    ag_stat["avg_score"] = round(ag_stat["avg_score"] / ag_stat["count"], 3)
                    ag_stat["total_weight"] = round(ag_stat["total_weight"], 3)
            return stats
        except Exception as exc:
            logger.error("get_memory_stats: %s", exc)
            return {}

    def classify_wa_intent(self, text: str) -> str:
        """Klasifikasi intent pesan WA via Groq (AI utama)."""
        t = (text or "").strip()
        if not t:
            return "LAINNYA"
        system = (
            "Anda router intent untuk asisten WhatsApp UMKM Indonesia. "
            'Balas HANYA JSON: {"intent":"..."} dengan intent salah satu dari: '
            "CATAT, SKOR, SARAN, PIUTANG, HAPUS, STOK, PRODUK, LAPORAN, LAINNYA.\n\n"
            "CATAT = mencatat transaksi baru (jual/beli/bayar dengan nominal, atau catat piutang DENGAN nominal).\n"
            "SKOR = tanya skor/kesehatan bisnis.\n"
            "SARAN = minta saran/tips/evaluasi bisnis.\n"
            "PIUTANG = BERTANYA siapa yang belum bayar, daftar utang/piutang, cek outstanding — BUKAN mencatat.\n"
            "HAPUS = hapus transaksi terakhir.\n"
            "STOK = tanya stok produk / inventory (mis. 'stok kopi berapa', 'gula masih ada?').\n"
            "PRODUK = minta daftar produk / list barang yang dijual.\n"
            "LAPORAN = minta laporan/rangkuman periode (mingguan/bulanan).\n"
            "LAINNYA = di luar kategori.\n\n"
            'Contoh PIUTANG: "siapa belum bayar utang", "siapa yang ngutang", "daftar piutang".\n'
            'Contoh CATAT: "jual kopi 5", "piutang pak budi 50000".\n'
            'Contoh STOK: "stok indomie", "gula masih ada?", "berapa stok minyak".\n'
            'Contoh PRODUK: "ada produk apa saja", "list barang", "menu lengkap".\n'
            'Contoh LAPORAN: "laporan minggu ini", "rangkuman", "rekap penjualan".\n'
            "Pertanyaan utang/piutang TANPA nominal = PIUTANG, bukan CATAT."
        )
        try:
            res = self.groq_client.chat.completions.create(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": t},
                ],
                model="openai/gpt-oss-120b",
                temperature=0,
                max_tokens=40,
                response_format={"type": "json_object"},
            )
            data = json.loads(res.choices[0].message.content or "{}")
            intent = str(data.get("intent", "LAINNYA")).strip().upper()
            valid = {"CATAT", "SKOR", "SARAN", "PIUTANG", "HAPUS", "STOK", "PRODUK", "LAPORAN", "LAINNYA"}
            return intent if intent in valid else "LAINNYA"
        except Exception as exc:
            logger.error("classify_wa_intent: %s", exc)
            return "LAINNYA"

    def get_ai_piutang_answer(self, df: pd.DataFrame, question: str) -> str:
        """Jawab pertanyaan piutang/utang dengan AI + data buku kas."""
        if df.empty:
            return (
                "Belum ada data transaksi. Catat dulu piutang pelanggan, "
                "misalnya: _piutang pak budi 50000_"
            )
        piutang = df[df["category"].str.contains("piutang|kasbon", case=False, na=False)]
        utang = df[
            df["category"].str.contains("utang|hutang", case=False, na=False)
            & ~df["category"].str.contains("piutang|kasbon", case=False, na=False)
        ]
        if piutang.empty and utang.empty:
            return (
                "Belum ada piutang/utang tercatat. "
                "Catat dulu, misalnya: _piutang pak budi 50000_"
            )

        def _rows_to_text(frame, label):
            if frame.empty:
                return f"{label}: (kosong)"
            lines = [
                f"- {row.get('note') or row.get('category')}: Rp {int(row.get('amount') or 0):,}"
                for _, row in frame.iterrows()
            ]
            total = int(frame["amount"].sum())
            return f"{label} (total Rp {total:,}):\n" + "\n".join(lines)

        data_block = _rows_to_text(piutang, "Piutang (pelanggan berutang ke toko)")
        data_block += "\n\n" + _rows_to_text(utang, "Utang toko (toko berutang)")

        prompt = (
            f"Pemilik warung bertanya via WhatsApp:\n\"{question}\"\n\n"
            f"Data buku kas:\n{data_block}\n\n"
            "Jawab singkat, jelas, Bahasa Indonesia santai UMKM. "
            "Sebut nama jika ada. Jangan mengarang di luar data di atas."
        )
        try:
            res = self.groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="openai/gpt-oss-120b",
                temperature=0.4,
                max_tokens=350,
            )
            return res.choices[0].message.content.strip()
        except Exception as exc:
            logger.error("get_ai_piutang_answer: %s", exc)
            if not piutang.empty:
                lines = [f"• {row['note']}: Rp {row['amount']:,.0f}" for _, row in piutang.iterrows()]
                return "📋 *Daftar Piutang:*\n" + "\n".join(lines)
            return "Gagal mengambil jawaban AI. Coba lagi."

    def get_ai_advisor_insights(self, df: pd.DataFrame) -> str:
        if df.empty or len(df) < 5:
            return "Belum cukup data. Catat minimal 5 transaksi dulu ya! 📝"
        income = df[df["type"] == "Pemasukan"]["amount"].sum()
        expense = df[df["type"] == "Pengeluaran"]["amount"].sum()
        profit = income - expense
        margin = round((profit / income) * 100, 1) if income > 0 else 0
        top_exp = df[df["type"] == "Pengeluaran"].groupby("category")["amount"].sum().nlargest(3)
        top_str = ", ".join([f"{c}: Rp {a:,.0f}" for c, a in top_exp.items()])
        piutang = df[df["category"].str.contains("piutang|kasbon", case=False, na=False)][
            "amount"
        ].sum()
        utang = df[
            df["category"].str.contains("utang", case=False, na=False)
            & ~df["category"].str.contains("piutang|kasbon", case=False, na=False)
        ]["amount"].sum()
        prompt = (
            f"Anda konsultan UMKM Indonesia. Data: Pendapatan Rp {income:,.0f}, "
            f"Pengeluaran Rp {expense:,.0f}, Laba Rp {profit:,.0f} (Margin {margin}%), "
            f"Top pengeluaran: {top_str}, Piutang Rp {piutang:,.0f}, Utang Rp {utang:,.0f}. "
            "Beri 2-3 saran singkat actionable. Bahasa santai warung, emoji secukupnya."
        )
        try:
            res = self.groq_client.chat.completions.create(
                messages=[{"role": "user", "content": prompt}],
                model="openai/gpt-oss-120b",
                temperature=0.7,
                max_tokens=300,
            )
            return res.choices[0].message.content.strip()
        except Exception:
            return "Gagal mengambil saran AI."
            
class TenantManager:
    """Handle multi-tenant session (tabel opsional)."""

    def __init__(self, sb: Client):
        self.sb = sb

    def get_active_tenant(self, user_id: str):
        uid = LarisCore._require_user_id(user_id)
        try:
            r = (
                self.sb.table("active_tenant_session")
                .select("tenant_id")
                .eq("user_id", uid)
                .limit(1)
                .execute()
            )
            return str(r.data[0]["tenant_id"]) if r.data else None
        except Exception:
            return None

    def set_active_tenant(self, user_id: str, tenant_id: str):
        uid = LarisCore._require_user_id(user_id)
        try:
            exp = (datetime.now() + timedelta(days=7)).isoformat()
            self.sb.table("active_tenant_session").upsert(
                {"user_id": uid, "tenant_id": tenant_id, "source": "manual", "expires_at": exp},
                on_conflict="user_id",
            ).execute()
        except Exception:
            pass

    def get_user_tenants(self, user_id: str):
        uid = LarisCore._require_user_id(user_id)
        try:
            r = (
                self.sb.table("user_tenants")
                .select("tenant_id, is_default, label")
                .eq("user_id", uid)
                .execute()
            )
            return r.data or []
        except Exception:
            return []
