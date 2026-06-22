import os
import base64
import httpx
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv
from groq import Groq
from agents import (
    ai_extractor_agent, vision_extractor_agent, voice_extractor_agent,
    db_insert_transaction, db_delete_transaction, get_dashboard_data,
    calculate_cuan_score, get_ai_advisor_insights, supabase
)

load_dotenv()
app = FastAPI(title="Kita Cuan WA Bot")

WA_PROVIDER = os.environ.get("WA_PROVIDER", "fonnte")
WA_API_KEY = os.environ["WA_API_KEY"]
groq = Groq(api_key=os.environ["GROQ_API_KEY"])


async def is_safe_message(text: str) -> bool:
    try:
        res = groq.chat.completions.create(
            model="openai/gpt-oss-safeguard-20b",
            messages=[{"role": "user", "content": f"Apakah pesan ini aman dan relevan dengan pencatatan keuangan UMKM? Jawab YA atau TIDAK saja.\nPesan: {text}"}],
            temperature=0, max_tokens=5
        )
        return "YA" in res.choices[0].message.content.upper()
    except Exception:
        return True


async def send_wa_reply(phone: str, message: str):
    if WA_PROVIDER == "fonnte":
        async with httpx.AsyncClient() as client:
            await client.post("https://api.fonnte.com/send", headers={"Authorization": WA_API_KEY}, data={"target": phone, "message": message})
    elif WA_PROVIDER == "wablas":
        async with httpx.AsyncClient() as client:
            await client.post("https://solo.wablas.com/api/v2/send-message", headers={"Authorization": f"Bearer {WA_API_KEY}"}, json={"data": [{"phone": phone, "message": message}]})


async def detect_intent(text: str) -> str:
    try:
        res = groq.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[{"role": "user", "content": f"Klasifikasikan pesan ke SATU kategori: CATAT, SKOR, SARAN, PIUTANG, HAPUS, LAINNYA.\nPesan: \"{text}\"\nJawab satu kata:"}],
            temperature=0, max_tokens=10
        )
        return res.choices[0].message.content.strip().upper()
    except Exception:
        return "LAINNYA"


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    if WA_PROVIDER == "fonnte":
        phone, text, media_type, media_url = body.get("from",""), body.get("text",""), body.get("media_type",""), body.get("media_url","")
    else:
        phone, text, media_type, media_url = body.get("phone",""), body.get("message",""), body.get("type",""), body.get("media_url","")

    if not phone:
        raise HTTPException(status_code=400, detail="No phone number")
    phone = phone.replace("@s.whatsapp.net", "").strip()
    reply = ""

    try:
        if media_type in ["image", "photo"] and media_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(media_url)
            b64 = base64.b64encode(resp.content).decode("utf-8")
            data = vision_extractor_agent(b64)
            for d in data:
                db_insert_transaction(d.get("type"), d.get("category"), d.get("amount"), d.get("note"), is_prive="prive" in str(d.get("category","")).lower())
            reply = f"✅ Struk terbaca!\nTotal: Rp {sum(d.get('amount',0) for d in data):,.0f}\n{len(data)} transaksi tercatat."

        elif media_type in ["audio", "voice"] and media_url:
            async with httpx.AsyncClient() as client:
                resp = await client.get(media_url)
            data = voice_extractor_agent(resp.content)
            for d in data:
                db_insert_transaction(d.get("type"), d.get("category"), d.get("amount"), d.get("note"), is_prive="prive" in str(d.get("category","")).lower())
            reply = f"✅ Suara terbaca!\n{len(data)} transaksi tercatat."

        elif text:
            if not await is_safe_message(text):
                reply = "⚠️ Pesan tidak dapat diproses. Silakan kirim transaksi atau pertanyaan keuangan warung."
            else:
                intent = await detect_intent(text)
                if intent == "CATAT":
                    data = ai_extractor_agent(text)
                    for d in data:
                        db_insert_transaction(d.get("type"), d.get("category"), d.get("amount"), d.get("note"), is_prive="prive" in str(d.get("category","")).lower())
                    lines = [f"• {d.get('type')} {d.get('category')}: Rp {d.get('amount',0):,.0f}" for d in data]
                    reply = f"✅ Tercatat!\n" + "\n".join(lines)
                elif intent == "SKOR":
                    score = calculate_cuan_score(get_dashboard_data())
                    reply = f"🔥 *Cuan Score: {score['score']}/100*\n\n_{score['insight']}_"
                elif intent == "SARAN":
                    reply = f"💡 *Saran AI:*\n\n{get_ai_advisor_insights(get_dashboard_data())}"
                elif intent == "PIUTANG":
                    df = get_dashboard_data()
                    p = df[df["category"].str.contains("piutang|kasbon", case=False, na=False)]
                    if p.empty:
                        reply = "✅ Tidak ada piutang tercatat."
                    else:
                        lines = [f"• {r['note']}: Rp {r['amount']:,.0f}" for _, r in p.iterrows()]
                        reply = f"📋 *Daftar Piutang:*\n" + "\n".join(lines) + f"\n\n*Total: Rp {p['amount'].sum():,.0f}*"
                elif intent == "HAPUS":
                    last = supabase.table("transactions").select("id, note, amount").order("id", desc=True).limit(1).execute()
                    if last.data:
                        txn = last.data[0]
                        db_delete_transaction(txn["id"])
                        reply = f"🗑️ Dihapus: {txn['note']} (Rp {txn['amount']:,.0f})"
                    else:
                        reply = "Tidak ada transaksi untuk dihapus."
                else:
                    reply = "🤔 Maaf belum paham.\n\nCoba kirim:\n• Transaksi: _Jual kopi 50rb_\n• Skor: _Berapa cuan score?_\n• Saran: _Ada saran bisnis?_\n• Piutang: _Siapa belum bayar?_\n• Hapus: _Hapus transaksi terakhir_\n• Atau kirim foto struk / voice note!"
        else:
            reply = "Kirim teks, foto struk, atau voice note untuk mencatat transaksi! 😊"
    except Exception as e:
        reply = f"❌ Error: {str(e)[:200]}"

    await send_wa_reply(phone, reply)
    return {"status": "ok"}


@app.get("/")
async def health():
    return {"status": "Kita Cuan WA Bot is running 🔥"}
