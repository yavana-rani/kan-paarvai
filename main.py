from fastapi.responses import HTMLResponse
from fastapi import FastAPI, Request, BackgroundTasks
import requests, base64, os
import openai

app = FastAPI()

API = "https://graph.facebook.com/v21.0"
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = openai.OpenAI(api_key=OPENAI_API_KEY)

H = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

def send_whatsapp(to, text):
    # WhatsApp max 4096 chars
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            r = requests.post(
                f"{API}/{PHONE_NUMBER_ID}/messages",
                headers={**H, "Content-Type": "application/json"},
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": chunk}
                },
                timeout=20
            )
            print(f"SEND {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"Send failed: {e}")

def handle_image(from_number, image_id):
    try:
        # 1. Get image URL from Meta
        info = requests.get(f"{API}/{image_id}", headers=H, timeout=20).json()
        print("Image info:", info)
        
        # 2. Download image
        img_bytes = requests.get(info["url"], headers=H, timeout=30).content
        
        # 3. Limit size - compress if > 4MB
        if len(img_bytes) > 4 * 1024 * 1024:
            print("Image too big, skipping")
            send_whatsapp(from_number, "Photo romba perisu. Konjam chinna photo anupunga.")
            return

        b64 = base64.b64encode(img_bytes).decode()

        # 4. Vision call - Kan Paarvai prompt
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            timeout=60,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "You are Kan Paarvai, a helpful Tamil eye assistant. Explain what you see in this image in simple Tamil (Tanglish ok). Be kind, clear, short."
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
                        }
                    ]
                }
            ]
        )
        answer = resp.choices[0].message.content
        send_whatsapp(from_number, answer)

    except Exception as e:
        print(f"Image handle error: {e}")
        send_whatsapp(from_number, "Mannikkavum, photo padikka mudiyavillai. Meendum anupunga.")

@app.get("/")
async def home():
    return {"status": "Kan Paarvai Live"}

@app.get("/privacy", response_class=HTMLResponse)
async def privacy():
    return """
    <html><head><title>Privacy Policy - Kan Paarvai</title></head>
    <body>
    <h1>Privacy Policy for Kan Paarvai</h1>
    <p>Last updated: 2026</p>
    <p>Kan Paarvai is a WhatsApp assistant to help visually impaired users read images.</p>
    <p><b>We do NOT store photos.</b> Images are processed temporarily to generate a text description and then deleted immediately. We do not save, sell, or share user data.</p>
    <p>Contact: murugesvarisangaranarayanan1@gmail.com</p>
    </body></html>
    """

@app.get("/terms", response_class=HTMLResponse)
async def terms():
    return """
    <html><head><title>Terms - Kan Paarvai</title></head>
    <body>
    <h1>Terms of Service - Kan Paarvai</h1>
    <p>Kan Paarvai provides image reading assistance for accessibility purposes only. The AI description may not be 100% accurate. Users should verify critical information.</p>
    <p>By using this service, you agree not to upload illegal or sensitive personal content.</p>
    <p>Contact: murugesvarisangaranarayanan1@gmail.com</p>
    </body></html>
    """

@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion():
    return """
    <html><body>
    <h1>Data Deletion Instructions</h1>
    <p>To request deletion of your data, email us at murugesvarisangaranarayanan1@gmail.com with your WhatsApp number. We will delete all associated data within 7 days.</p>
    <p>Since we do not store photos, there is no photo data to delete by default.</p>
    </body></html>
    """

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
            image_id = msg["image"]["id"]
            bg.add_task(handle_image, from_number, image_id)
        else:
            bg.add_task(send_whatsapp, from_number, "Vanakkam! 🙏 Photo anupunga, naan padichu solren. Kan Paarvai ungalukkaga.")

    except Exception as e:
        print(f"Webhook error: {e}")
    
    # MUST return fast within 2 sec, else Facebook retries
    return {"ok": True}

@app.get("/webhook")
async def verify(request: Request):
    # For Meta verification
    params = request.query_params
    if params.get("hub.verify_token") == os.getenv("VERIFY_TOKEN", "kanpaarvai123"):
        return int(params.get("hub.challenge", 0))
    return {"error": "Verification failed"}
