from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
import os, requests, base64
from openai import OpenAI

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

@app.get("/")
def home():
    return {"status": "Kan Paarvai Live - Ready for blind users"}

# === ADD THIS FOR META LIVE APPROVAL ===
@app.get("/privacy", response_class=HTMLResponse)
def privacy():
    return """
    <html><body style="font-family:Arial; padding:20px; max-width:800px;">
    <h1>Privacy Policy - Kan Paarvai</h1>
    <p><b>Updated: 16 Oct 2026</b></p>
    <p>Kan Paarvai helps blind users in Tamil Nadu describe photos via WhatsApp.</p>
    <p><b>Data:</b> WhatsApp messages & photos sent for description.</p>
    <p><b>Use:</b> AI describes image in Tamil/English and replies back.</p>
    <p><b>Storage:</b> NO photo storage. Deleted after processing. No third-party share.</p>
    <p><b>Contact:</b> murrugeswarisangaranarayanan1@gmail.com, Virudhunagar, TN</p>
    <p><a href="/terms">Terms</a></p>
    </body></html>
    """

@app.get("/terms", response_class=HTMLResponse)
def terms():
    return """
    <html><body style="font-family:Arial; padding:20px; max-width:800px;">
    <h1>Terms and Conditions - Kan Paarvai</h1>
    <p><b>Updated: 16 Oct 2026</b></p>
    <p>1. Free service for visually impaired to read documents.</p>
    <p>2. AI may be wrong. Don't use for road crossing, medicine, driving.</p>
    <p>3. Don't send illegal/private photos of others.</p>
    <p>4. No data stored, photos deleted instantly.</p>
    <p>5. Free server, may have delays.</p>
    <p>Contact: murrugeswarisangaranarayanan1@gmail.com</p>
    <p><a href="/privacy">Privacy Policy</a></p>
    </body></html>
    """

@app.get("/webhook")
def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge) # Fixed: was int()
    return PlainTextResponse("Failed", status_code=403)

def send_whatsapp(to, text):
    requests.post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"},
        json={"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":text}}
    )

@app.post("/webhook")
async def incoming(request: Request):
    body = await request.json()
    print("INCOMING:", body)
    try:
        entry = body['entry'][0]['changes'][0]['value']
        if 'messages' not in entry:
            return {"ok": True}
        msg = entry['messages'][0]
        from_number = msg['from']

        # 1. If user sends Hi / text
        if msg['type'] == 'text':
            text = msg['text']['body'].lower()
            welcome = "Vanakkam! 🙏 Naan Kan Paarvai. Kangal theriyatha ungalukku uthavi seiyya vanthen. Oru photo anupunga, athil enna irukku endru Tamilil solluven. / Hello! I am Kan Paarvai. Send me a photo/document, I will read it for you in Tamil."
            send_whatsapp(from_number, welcome)
            return {"ok": True}

        # 2. If user sends image
        if msg['type'] == 'image':
            image_id = msg['image']['id']
            url_info = requests.get(f"https://graph.facebook.com/v19.0/{image_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
            img_url = url_info['url']
            img_data = requests.get(img_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content

            b64 = base64.b64encode(img_data).decode()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":[
                    {"type":"text","text":"You are Kan Paarvai, helper for blind Tamil users. Read this document clearly. Extract all text, then explain in simple Tamil + English what it is, what to do. Be kind, clear, short."},
                    {"type":"image_url","image_url":{"url": f"data:image/jpeg;base64,{b64}"}}
                ]}]
            )
            answer = resp.choices[0].message.content
            send_whatsapp(from_number, answer)
    except Exception as e:
        print("Error:", e)
    return {"ok": True}
