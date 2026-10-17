from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi import FastAPI, Request, BackgroundTasks
import requests, base64, os
import openai

app = FastAPI(docs_url=None, redoc_url=None)

API = "https://graph.facebook.com/v22.0"
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)

def get_headers():
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"}

def send_whatsapp(to, text):
    phone_id = os.getenv("PHONE_NUMBER_ID", "").strip()
    if not phone_id:
        print("PHONE_NUMBER_ID missing!")
        return
    headers = {**get_headers(), "Content-Type": "application/json"}
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            r = requests.post(
                f"{API}/{phone_id}/messages",
                headers=headers,
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": chunk}
                },
                timeout=20
            )
            print(f"SEND {r.status_code}: {r.text[:300]}")
        except Exception as e:
            print(f"Send failed: {e}")

def handle_image(from_number, image_id):
    try:
        headers = get_headers()
        info = requests.get(f"{API}/{image_id}", headers=headers, timeout=20).json()
        print("Image info:", info)
        if "url" not in info:
            send_whatsapp(from_number, "Photo URL kidaikkavillai. Meendum anupunga.")
            return
        
        img_bytes = requests.get(info["url"], headers=headers, timeout=30).content
        if len(img_bytes) > 4 * 1024 * 1024:
            send_whatsapp(from_number, "Photo romba perisu. Konjam chinna photo anupunga.")
            return

        b64 = base64.b64encode(img_bytes).decode()

        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "You are Kan Paarvai, a helpful Tamil eye assistant. Explain what you see in this image in simple Tamil (Tanglish ok). Be kind, clear, short."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                    ]
                }
            ]
        )
        answer = resp.choices[0].message.content
        send_whatsapp(from_number, answer)

    except Exception as e:
        print(f"Image handle error: {e}")
        send_whatsapp(from_number, "Mannikkavum, photo padikka mudiyavillai. Meendum anupunga.")

@app.api_route("/", methods=["GET","HEAD"])
async def home():
    return {"status": "Kan Paarvai is live!", "message": "Send WhatsApp image to get help"}

@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return "<html><body><h1>Privacy Policy</h1><p>We do NOT store photos. Deleted after reading.</p><p>Contact: murugesvarisangaranarayanan1@gmail.com</p></body></html>"

@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return "<html><body><h1>Terms</h1><p>For accessibility only. AI may not be 100% accurate.</p></body></html>"

@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion():
    return "<html><body><h1>Data Deletion</h1><p>Email murugesvarisangaranarayanan1@gmail.com to delete data.</p></body></html>"

@app.post("/webhook")
async def incoming(request: Request, bg: BackgroundTasks):
    body = await request.json()
    print("Webhook:", body)
    try:
        value = body["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return {"ok": True}
        msg = value["messages"][0]
        from_number = msg["from"]
        if msg["type"] == "image":
            bg.add_task(handle_image, from_number, msg["image"]["id"])
        else:
            bg.add_task(send_whatsapp, from_number, "Vanakkam! 🙏 Photo anupunga, naan padichu solren.")
    except Exception as e:
        print(f"Webhook error: {e}")
    return {"ok": True}

@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(content=params.get("hub.challenge", "0"), status_code=200)
    return PlainTextResponse("Verification failed", status_code=403)
