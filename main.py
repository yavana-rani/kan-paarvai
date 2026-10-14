from fastapi import FastAPI, Request
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

@app.get("/webhook")
def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return "Failed"

@app.post("/webhook")
async def incoming(request: Request):
    body = await request.json()
    try:
        entry = body['entry'][0]['changes'][0]['value']
        if 'messages' not in entry: return {"ok": True}
        msg = entry['messages'][0]
        from_number = msg['from']

        # Get image
        if msg['type'] == 'image':
            image_id = msg['image']['id']
            # Get image URL from WhatsApp
            url_info = requests.get(f"https://graph.facebook.com/v19.0/{image_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
            img_url = url_info['url']
            img_data = requests.get(img_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content

            # Analyze with OpenAI Vision
            b64 = base64.b64encode(img_data).decode()
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role":"user","content":[
                    {"type":"text","text":"You are Kan Paarvai, helper for blind Tamil users. Read this document clearly. Extract all text, then explain in simple Tamil + English what it is, what to do. Be kind, clear, short."},
                    {"type":"image_url","image_url":{"url": f"data:image/jpeg;base64,{b64}"}}
                ]}]
            )
            answer = resp.choices[0].message.content

            # Send back on WhatsApp
            requests.post(f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"},
                json={"messaging_product":"whatsapp","to":from_number,"type":"text","text":{"body":answer}}
            )
    except Exception as e:
        print("Error:", e)
    return {"ok": True}
