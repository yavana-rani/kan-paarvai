from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import requests, os, base64
from gtts import gTTS
import uuid

app = FastAPI()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
KAN_PROMPT = "Nee Kan Paarvai. Tamil la 2-4 varila short-a sollu. Maths iruntha x square, veru nu pesara maathiri sollu."

@app.get("/")
def home(): return {"status":"Live with Voice pa"}
@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)
@app.head("/")
def head(): return {"ok": True}

@app.post("/webhook")
async def receive(request: Request):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' not in entry: return {"ok": True}
        msg = entry['messages'][0]
        from_num = msg['from']
        tamil_reply = "Photo anupunga pa!"

        if msg['type'] == 'image':
            media_id = msg['image']['id']
            m_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
            img_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64_img = base64.b64encode(img_bytes).decode('utf-8')
            
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_KEY}"
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}]}]}
            g_res = requests.post(gemini_url, json=payload).json()
            
            if 'candidates' in g_res:
                tamil_reply = g_res['candidates'][0]['content']['parts'][0]['text'][:600]
            else:
                tamil_reply = "Photo clear-a illa pa"

        # 1. TEXT ANUPPU
        requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": from_num, "text": {"body": tamil_reply}})

        # 2. VOICE ANUPPU - Tamil Voice
        try:
            file_name = f"/tmp/{uuid.uuid4()}.mp3"
            gTTS(text=tamil_reply, lang='ta', slow=False).save(file_name)
            
            # Voice-a WhatsApp-ku upload pannu
            with open(file_name, 'rb') as f:
                up = requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                    files={"file": ("voice.mp3", f, "audio/mpeg")},
                    data={"type": "audio/mpeg", "messaging_product": "whatsapp"}
                ).json()
            
            if 'id' in up:
                audio_id = up['id']
                requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                    json={"messaging_product": "whatsapp", "to": from_num, "type": "audio", "audio": {"id": audio_id}}
                )
        except Exception as ve:
            print(f"Voice Error: {ve}")

    except Exception as e:
        print(f"Error: {e}")
    return {"ok": True}
