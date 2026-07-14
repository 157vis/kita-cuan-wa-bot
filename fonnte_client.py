"""Fonnte WA client multi-tenant.

Lookup `fonnte_token` per client dari tabel `clients` Supabase, dengan
fallback ke environment variable `WA_API_KEY` (backward compat).

Mengikuti pola yang sama dengan `bukuwarung-ai/core/client_registry.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class FonnteClient:
    """Kirim pesan WA via Fonnte — token di-resolve per nomor dari Supabase.

    Flow:
        1. Pesan masuk -> webhook ambil phone
        2. resolve_user_id_by_phone() -> dapat user_id Supabase
        3. Lookup clients table by metadata->>user_id atau owner_phones
        4. Ambil fonnte_token
        5. Kirim via Fonnte API dengan token tsb
    """

    def __init__(self, supabase_client: Any) -> None:
        self._db = supabase_client
        self._cache: dict[str, str] = {}  # client_id -> fonnte_token
        self._phone_to_client: dict[str, str] = {}  # phone -> client_id
        self._env_token = os.environ.get("WA_API_KEY", "").strip()
        self._provider = (
            "fonnte"
            if os.environ.get("WA_PROVIDER", "fonnte").lower().strip() in ("fonnte", "fonte")
            else os.environ.get("WA_PROVIDER", "fonnte").lower().strip()
        )

    def _normalize_phone(self, phone: str) -> str:
        """Ambil digit saja, ganti awalan 0 -> 62."""
        digits = "".join(ch for ch in phone if ch.isdigit())
        if digits.startswith("0"):
            digits = "62" + digits[1:]
        return digits

    async def _lookup_token_by_phone(self, phone: str) -> str:
        """Cari fonnte_token yang punya nomor phone di owner_phones.

        Returns:
            fonnte_token atau "" kalau tidak ketemu.
        """
        phone_norm = self._normalize_phone(phone)

        # Cek cache
        for cached_phone, token in self._phone_to_client.items():
            if self._normalize_phone(cached_phone) == phone_norm:
                return token

        if not self._db:
            return ""

        # Query: cari client yang punya phone ini di owner_phones (array contains)
        try:
            result = await asyncio.to_thread(
                lambda: (
                    self._db.table("clients")
                    .select("client_id, fonnte_token, owner_phones, metadata")
                    .eq("is_active", True)
                    .execute()
                )
            )
            rows = result.data or []
            for row in rows:
                token = (row.get("fonnte_token") or "").strip()
                if not token:
                    continue
                owners = row.get("owner_phones") or []
                # Cek apakah phone_norm ada di owner_phones
                for owner in owners:
                    if self._normalize_phone(str(owner)) == phone_norm:
                        self._phone_to_client[phone] = token
                        self._cache[row.get("client_id")] = token
                        logger.info(
                            "fonnte token resolved: phone=%s -> client_id=%s",
                            phone_norm,
                            row.get("client_id"),
                        )
                        return token
        except Exception as exc:
            logger.warning("lookup fonnte_token gagal: %s", exc)

        return ""

    async def lookup_token_by_device(self, device: str) -> str:
        """Cari fonnte_token yang punya nomor device di metadata.device
        atau di owner_phones (device = nomor Fonnte toko yang menerima pesan).

        Pakai ini untuk kirim BALASAN ke customer: balasan dikirim via
        device toko, bukan via device customer.

        Returns:
            fonnte_token atau "" kalau tidak ketemu.
        """
        if not device:
            return ""

        device_norm = self._normalize_phone(device)

        if not self._db:
            return self._env_token or ""

        try:
            result = await asyncio.to_thread(
                lambda: (
                    self._db.table("clients")
                    .select("client_id, fonnte_token, owner_phones, metadata")
                    .eq("is_active", True)
                    .execute()
                )
            )
            rows = result.data or []
            for row in rows:
                token = (row.get("fonnte_token") or "").strip()
                if not token:
                    continue
                # Cek metadata.device (kalau ada)
                meta = row.get("metadata") or {}
                if isinstance(meta, dict):
                    meta_device = meta.get("device") or meta.get("fonnte_device")
                    if meta_device and self._normalize_phone(str(meta_device)) == device_norm:
                        self._cache[row.get("client_id")] = token
                        logger.info(
                            "fonnte token resolved by device=%s -> client_id=%s",
                            device_norm, row.get("client_id"),
                        )
                        return token
                # Fallback: kalau tidak ada metadata, pakai owner_phones pertama
                owners = row.get("owner_phones") or []
                if owners and self._normalize_phone(str(owners[0])) == device_norm:
                    self._cache[row.get("client_id")] = token
                    logger.info(
                        "fonnte token resolved by owner_phones=%s -> client_id=%s",
                        device_norm, row.get("client_id"),
                    )
                    return token
        except Exception as exc:
            logger.warning("lookup fonnte_token by device gagal: %s", exc)

        return ""

    async def send_message(
        self, phone: str, message: str, inboxid: str | None = None
    ) -> bool:
        """Kirim pesan WA — token otomatis di-resolve per phone.

        Returns True kalau sukses, False kalau gagal.
        """
        if not phone or not message:
            logger.warning("send_message: phone/message kosong")
            return False

        target = self._normalize_phone(phone)
        token = await self._lookup_token_by_phone(phone)

        # Fallback ke env kalau tidak ketemu di Supabase
        if not token:
            token = self._env_token

        if not token:
            logger.error(
                "send_message: tidak ada fonnte_token untuk phone=%s (Supabase & env kosong)",
                target,
            )
            return False

        try:
            if self._provider == "fonnte":
                payload = {"target": target, "message": message}
                if inboxid:
                    payload["inboxid"] = inboxid
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        "https://api.fonnte.com/send",
                        headers={"Authorization": token},
                        data=payload,
                    )
                logger.debug(
                    "fonnte send -> %s: %s", resp.status_code, resp.text[:300]
                )
                if resp.status_code >= 400:
                    logger.error("fonnte send gagal status=%s", resp.status_code)
                    return False
                return True

            elif self._provider == "wablas":
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        "https://solo.wablas.com/api/v2/send-message",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"data": [{"phone": target, "message": message}]},
                    )
                logger.debug(
                    "wablas send -> %s: %s", resp.status_code, resp.text[:300]
                )
                return resp.status_code < 400
        except httpx.HTTPError as exc:
            logger.exception("send_message HTTP error: %s", exc)
            return False

        return False

    @property
    def provider(self) -> str:
        return self._provider

    def stats(self) -> dict[str, int]:
        """Statistik cache (untuk debugging)."""
        return {
            "cached_tokens": len(self._cache),
            "cached_phones": len(self._phone_to_client),
            "env_token_set": bool(self._env_token),
        }
