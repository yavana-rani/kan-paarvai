from fastapi import FastAPI, Request, BackgroundTasks
import os
import requests
import tempfile
from PIL import Image
from gtts import gTTS
from io import BytesIO

# NEW GOOGLE LIBRARY - 3.8 model
from google import genai

app = FastAPI()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini 3.8 Flash - New model
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

GEMINI_MODEL_NAME = "gemini-3.8-flash"  # <--- Neenga ketta maathiri same

def send_whatsapp(to, text):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        return
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}}
    try:
        requests.post(url, headers=headers, json=payload, timeout=15)
    except Exception as e:
        print(f"Send text error: {e}")

def send_whatsapp_audio(to, audio_path):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        return None
    try:
        url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f, 'audio/mpeg')}
            data = {'type': 'audio/mpeg', 'messaging_product': 'whatsapp'}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            media_id = r.json().get('id')
        if not media_id:
            print(f"Audio upload failed: {r.text}")
            return None
        url2 = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        headers2 = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": media_id}}
        requests.post(url2, headers=headers2, json=payload, timeout=15)
        return True
    except Exception as e:
        print(f"Send audio error: {e}")
        return None

def create_tamil_voice(text):
    try:
        clean = text.replace('*','').replace('#','')[:800]
        tts = gTTS(text=clean, lang='ta', slow=False)
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        tts.save(path)
        return path
    except Exception as e:
        print(f"gTTS error: {e}")
        return None

def handle_image(from_number, image_id):
    try:
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        # 1. Download image from WhatsApp
        media_url = f"https://graph.facebook.com/v20.0/{image_id}"
        r = requests.get(media_url, headers=headers, timeout=15)
        media_info = r.json()
        image_url = media_info.get("url")
        if not image_url:
            send_whatsapp(from_number, "Mannikkavum, photo download mudiyavillai.")
            return
        
        img_data = requests.get(image_url, headers=headers, timeout=20).content
        image = Image.open(BytesIO(img_data))

        # 2. ENGAGE - 0 sec la reply
        send_whatsapp(from_number, "Photo kidaichiduchu pa, Kan Paarvai paarkuthu... rendu nimisham 😊")

        # 3. Gemini 3.8 Flash analysis
        tamil_prompt = """Intha photo-vai paarvai illathavarkalukku Tamil-il thelivaga vilakku. 
        1. Photo-vil enna irukku? 
        2. Text irundhal athai Tamil-il padithu sollu. Numbers irundha row-wise sollu.
        3. Colors, objects, people ellam sollu.
        Simple warm Tamil, English vendaam."""

        if not gemini_client:
            send_whatsapp(from_number, "GEMINI_API_KEY set pannavillai Render-il.")
            return

        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=[tamil_prompt, image]
        )
        answer = response.text

        # 4. Send Text + Voice
        send_whatsapp(from_number, answer)
        voice_path = create_tamil_voice(answer)
        if voice_path:
            send_whatsapp_audio(from_number, voice_path)
            try: os.remove(voice_path)
            except: pass

    except Exception as e:
        print(f"Image handle error: {e}")
        send_whatsapp(from_number, "Mannikkavum, photo padikka mudiyavillai. Meendum anupunga.")

@app.get("/")
async def root():
    return {"status": "Kan Paarvai LIVE Gemini 3.8 Flash"}

@app.get("/webhook")
async def verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        challenge = params.get("hub.challenge", "0")
        return int(challenge)
    return {"error": "invalid token"}

@app.post("/webhook")
async def incoming(request: Request, bg: BackgroundTasks):
    print("=== WEBHOOK POST RECEIVED ===", flush=True)
    body = await request.json()
    try:
        value = body["entry"][0]["changes"][0]["value"]
        if "messages" not in value:
            return {"ok": True}
        msg = value["messages"][0]
        from_number = msg["from"]
        if msg["type"] == "image":
            bg.add_task(handle_image, from_number, msg["image"]["id"])
        else:
            welcome_text = "Vanakkam! Naan Kan Paarvai. Photo anupunga, Tamil-il voice-il vilakkam tharren."
            def welcome_with_voice():
                send_whatsapp(from_number, welcome_text)
                vp = create_tamil_voice(welcome_text)
                if vp:
                    send_whatsapp_audio(from_number, vp)
                    try: os.remove(vp)
                    except: pass
            bg.add_task(welcome_with_voice)
    except Exception as e:
        print(f"Webhook error: {e}")
    return {"ok": True}
