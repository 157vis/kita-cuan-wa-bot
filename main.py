@app.post("/webhook")
async def webhook(request: Request):
    body = await request.json()
    
    # === DEBUG LOGGING ===
    print(f"📨 WEBHOOK RECEIVED: {body}")
    print(f"📱 Provider: {WA_PROVIDER}")
    # ====================
    
    if WA_PROVIDER == "fonnte":
        phone = body.get("from", "")
        text = body.get("text", "")
        media_type = body.get("media_type", "")
        media_url = body.get("media_url", "")
    else:
        phone = body.get("phone", "")
        text = body.get("message", "")
        media_type = body.get("type", "")
        media_url = body.get("media_url", "")

    print(f"📞 Phone: {phone} | 📝 Text: {text[:50]} | 🖼️ Media: {media_type}")
    
    if not phone:
        raise HTTPException(status_code=400, detail="No phone number")
    phone = phone.replace("@s.whatsapp.net", "").strip()
    reply = ""
