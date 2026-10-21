from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import google.generativeai as genai
import requests, os
from PIL import Image
import io

app = FastAPI()

genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID") # 1464188770101945

KAN_PROMPT = "Nee Kan Paarvai. Tamil la 2-4 varila short-a sollu. Maths iruntha x square, veru nu pesara maathiri sollu."

@app.get("/")
def home(): return {"status":"Live"}

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)

@app.post("/webhook")
async def receive(request: Request):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' not in entry: return {"ok": True}
        
        msg = entry['messages'][0]
        from_number = msg['from']
        
        if msg['type'] == 'image':
            # 1. Image URL edukka
            media_id = msg['image']['id']
            media_url_res = requests.get(
                f"https://graph.facebook.com/v20.0/{media_id}",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
            ).json()
            media_url = media_url_res['url']
            
            # 2. Download
            img_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            image = Image.open(io.BytesIO(img_bytes))
            
            # 3. Gemini ku anuppa
            res = model.generate_content([KAN_PROMPT, image])
            tamil_reply = res.text[:500]
        else:
            tamil_reply = "Photo anupunga pa, naan Tamil la solluren!"

        # 4. Thumba WhatsApp la reply
        requests.post(
            f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": from_number, "text": {"body": tamil_reply}}
        )
    except Exception as e:
        print(f"Error: {e}")
    return {"ok": True}
