from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import requests, os, base64

app = FastAPI()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
KAN_PROMPT = "Nee Kan Paarvai. Tamil la 2-4 varila short-a sollu. Maths iruntha x square, veru nu pesara maathiri sollu."

@app.get("/")
def home(): return {"status":"Live pa"}
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

            payload = {"contents": [{"parts": [{"text": KAN_PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}]}]}
            
            # Unga key-ku ethu work aagutho atha auto-pidikkum
            MODELS_TO_TRY = [
                "gemini-2.0-flash",
                "gemini-2.5-flash",
                "gemini-1.5-flash",
                "gemini-1.5-flash-001",
                "gemini-1.5-flash-8b",
                "gemini-flash-latest"
            ]
            
            g_res = {}
            for model_name in MODELS_TO_TRY:
                for ver in ["v1beta", "v1"]:
                    url = f"https://generativelanguage.googleapis.com/{ver}/models/{model_name}:generateContent?key={GEMINI_KEY}"
                    r = requests.post(url, json=payload).json()
                    print(f"Trying {ver}/{model_name} -> {r.get('error', {}).get('message', 'OK')[:100]}")
                    if 'candidates' in r:
                        g_res = r
                        break
                if 'candidates' in g_res:
                    break
            
            if 'candidates' in g_res:
                tamil_reply = g_res['candidates'][0]['content']['parts'][0]['text'][:600]
            else:
                print(f"ALL FAILED: {g_res}")
                tamil_reply = "Gemini key-la problem pa. aistudio.google.com la puthu key eduthu Render la maathunga pa!"

        requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": from_num, "text": {"body": tamil_reply}})
    except Exception as e:
        print(f"Error: {e}")
    return {"ok": True}
