from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from google import genai
from google.genai import types
import requests, os
from PIL import Image
import io

app = FastAPI()

# Puthu client
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")

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
        tamil_reply = "Photo anupunga pa!"

        if msg['type'] == 'image':
            media_id = msg['image']['id']
            media_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
            img_bytes = requests.get(media_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            image = Image.open(io.BytesIO(img_bytes))

            # Gemini 1.5 Flash - Puthu method
            response = client.models.generate_content(
                model="gemini-1.5-flash",
                contents=[KAN_PROMPT, image]
            )
            tamil_reply = response.text[:500]

        requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": from_number, "text": {"body": tamil_reply}}
        )
    except Exception as e:
        print(f"Error: {e}")
    return {"ok": True}
