import os
import re
import json
import base64
import io
import random
import pandas as pd
from datetime import datetime, timedelta
from supabase import create_client
from groq import Groq

supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])
groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])


def clean_json_response(response_text: str) -> str:
    match = re.search(r'```(?:json)?\n(.*?)\n```', response_text, re.DOTALL)
    if match:
        return match.group(1)
    if not response_text.strip().startswith('['):
        return f"[{response_text}]"
    return response_text


def ai_extractor_agent(text: str) -> list[dict]:
    prompt = f"""Anda adalah akuntan warung Indonesia. Ekstrak teks berikut menjadi array JSON transaksi.
Teks: "{text}"
Aturan:
- 'type': "Pemasukan" atau "Pengeluaran"
- 'amount': angka tanpa titik/koma
- 'category': kategori singkat (Penjualan, Bahan Baku, Utang, Piutang, Operasional, Prive)
- 'note': ringkasan singkat
- Jika ada kata "prive", "ambil uang pribadi", "keperluan rumah", set category ke "Prive"
HANYA kembalikan JSON array yang valid."""
    try:
        res = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="openai/gpt-oss-120b",
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        return json.loads(clean_json_response(res.choices[0].message.content))
    except Exception as e:
        print(f"Extractor error: {e}")
        return []


def vision_extractor_agent(base64_image: str) -> list[dict]:
    prompt = "Baca struk belanja warung Indonesia. Cari GRAND TOTAL. Output JSON: [{'type':'Pengeluaran','amount':angka,'category':'Bahan Baku','note':'ringkasan barang'}]. Hanya JSON."
    try:
        res = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
            ]}],
            model="openai/gpt-oss-120b",
            temperature=0.1,
            response_format={"type": "json_object"}
        )
        return json.loads(clean_json_response(res.choices[0].message.content))
    except Exception as e:
        print(f"Vision error: {e}")
        return []


def voice_extractor_agent(audio_bytes: bytes) -> list[dict]:
    try:
        trans = groq_client.audio.transcriptions.create(
            file=("recording.wav", io.BytesIO(audio_bytes)),
            model="whisper-large-v3-turbo",
            language="id",
            response_format="text"
        )
        return ai_extractor_agent(trans)
    except Exception as e:
        print(f"Voice error: {e}")
        return []


def db_insert_transaction(type_txn, category, amount, note, is_prive=False):
    prev = supabase.table("transactions").select("running_balance").order("id", desc=True).limit(1).execute()
    last_balance = prev.data[0]["running_balance"] if prev.data else 0
    new_balance = last_balance + amount if type_txn == "Pemasukan" else last_balance - amount

    prefix = "PRV" if is_prive else ("KM" if type_txn == "Pemasukan" else "KK")
    today = datetime.now().strftime("%y%m%d")
    count_resp = supabase.table("transactions").select("id", count="exact").like("date", f"{today}%").execute()
    seq = (count_resp.count or 0) + 1
    receipt_no = f"{prefix}-{today}-{seq:03d}"

    data = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "type": type_txn, "category": category, "amount": amount, "note": note,
        "receipt_no": receipt_no, "running_balance": new_balance, "is_prive": is_prive
    }
    supabase.table("transactions").insert(data).execute()


def db_delete_transaction(txn_id):
    supabase.table("transactions").delete().eq("id", txn_id).execute()


def get_dashboard_data():
    response = supabase.table("transactions").select("*").order("id", desc=True).execute()
    return pd.DataFrame(response.data) if response.data else pd.DataFrame()


def calculate_cuan_score(_df):
    if _df.empty:
        return {"score": 0, "insight": "Mulai catat transaksi pertama Anda!", "level": "low"}
    income = _df[_df['type'] == 'Pemasukan']['amount'].sum()
    expense = _df[_df['type'] == 'Pengeluaran']['amount'].sum()
    profit = income - expense
    margin_score = min(40, max(0, (profit / income) * 80)) if income > 0 else 0
    _df_copy = _df.copy()
    _df_copy['date'] = pd.to_datetime(_df_copy['date'])
    last_30 = _df_copy[_df_copy['date'] >= (datetime.now() - timedelta(days=30))]
    active_days = last_30['date'].dt.date.nunique()
    consistency_score = min(30, (active_days / 30) * 30)
    utang = _df[_df['category'].str.contains('utang|kasbon|piutang', case=False, na=False)]['amount'].sum()
    debt_score = max(0, 20 - ((utang / income) * 40)) if income > 0 else 10
    volume_score = min(10, len(last_30) * 0.5)
    total_score = int(min(100, max(0, margin_score + consistency_score + debt_score + volume_score)))
    if total_score >= 75:
        level, insights = "high", ["Warung sangat sehat! Pertahankan 🔥", "Luar biasa! Margin konsisten 💪", "Cuan mengalir deras! Siap ekspansi? 🚀"]
    elif total_score >= 45:
        level, insights = "mid", ["Tingkatkan konsistensi pencatatan 📝", "Margin bisa ditingkatkan. Evaluasi harga 💡", "Perhatikan pengeluaran kecil yang bocor 🔍"]
    else:
        level, insights = "low", ["Segera evaluasi biaya operasional ⚠️", "Jangan menyerah! Rapikan pencatatan 💪", "Margin tipis. Kurangi stok jarang laku 📉"]
    return {"score": total_score, "insight": random.choice(insights), "level": level}


def get_ai_advisor_insights(_df):
    if _df.empty or len(_df) < 5:
        return "Belum cukup data untuk analisis. Catat minimal 5 transaksi dulu ya! 📝"
    income = _df[_df['type'] == 'Pemasukan']['amount'].sum()
    expense = _df[_df['type'] == 'Pengeluaran']['amount'].sum()
    profit = income - expense
    margin = round((profit / income) * 100, 1) if income > 0 else 0
    top_expense = _df[_df['type'] == 'Pengeluaran'].groupby('category')['amount'].sum().nlargest(3)
    top_expense_str = ", ".join([f"{cat}: Rp {amt:,.0f}" for cat, amt in top_expense.items()])
    piutang = _df[_df['category'].str.contains('piutang|kasbon', case=False, na=False)]['amount'].sum()
    utang = _df[_df['category'].str.contains('utang', case=False, na=False) & ~_df['category'].str.contains('piutang|kasbon', case=False, na=False)]['amount'].sum()
    prompt = f"""Anda adalah konsultan bisnis UMKM Indonesia yang ramah dan praktis.
Berdasarkan data keuangan warung berikut, berikan 2-3 saran singkat yang LANGSUNG BISA DILAKUKAN pemilik warung.
DATA WARUNG:
- Pendapatan: Rp {income:,.0f} | Pengeluaran: Rp {expense:,.0f} | Laba: Rp {profit:,.0f} (Margin: {margin}%)
- Top 3 Pengeluaran: {top_expense_str}
- Piutang: Rp {piutang:,.0f} | Utang: Rp {utang:,.0f}
ATURAN: Bahasa santai warung, fokus tindakan konkret, maksimal 3 poin, gunakan emoji secukupnya."""
    try:
        res = groq_client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model="openai/gpt-oss-120b", temperature=0.7, max_tokens=300
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        return f"Gagal mengambil saran AI: {e}"
