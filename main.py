import os, base64, requests, re
from fastapi import FastAPI, Request
from collections import deque
from gtts import gTTS
import google.generativeai as genai

app = FastAPI()

WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GEMINI_API_KEY)

processed_ids = deque(maxlen=200)

KAN_PROMPT = """
Nee Kan Paarvai. Tamil maana var kaga Tamil la pesura anbu friend.
RULES - Kattayam follow pannu:
1. Photo enna quality la irunthalum, 29kB low quality a irunthalum 'Puriyala' nu SOLLA KOODATHU. Unnaala mudinja varaikum best-a guess panni padichu Tamil la sollu.
2. Syllabus, Time Table, Question Paper table iruntha month-by-month, row-by-row full-a list pannu. Chinna font a irunthalum padikka try pannu.
3. Yaaravathu 'letter venum, application venum, document ready pannu' nu ketta, neeye oru perfect formal letter Tamil/English la ready panni kodu.
4. Reply la $ * # ** symbols use panna koodathu. Clean Tamil la mattum.
5. Full detail-a Tamil la sollu.
"""

def clean_tamil(text):
    text = re.sub(r'[\$\*\_\#\`]', '', text)
    return text.replace('**','').strip()

def get_media_bytes(media_id):
    m_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", 
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
    return requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content

def send_whatsapp(to, msg_text):
    msg_text = clean_tamil(msg_text)
    # Text send
    requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product":"whatsapp","to":to,"type":"text","text":{"body":msg_text[:3500]}})
    # TTS Voice send - ungaloda audio upload code iruntha anga add pannikonga
    try:
        tts = gTTS(text=msg_text[:350], lang='ta')
        tts.save("/tmp/voice.mp3")
        # unga existing voice sending logic...
    except: pass

@app.get("/")
async def home():
    return {"status": "Kan Paarvai V2 FastAPI Running"}

@app.post("/webhook")
async def webhook(req: Request):
    data = await req.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' not in entry: return {"ok": True}
        msg = entry['messages'][0]
        
        if msg['id'] in processed_ids:
            return {"ok": True}
        processed_ids.append(msg['id'])
        
        from_num = msg['from']
        parts = []

        # 1. VOICE RECOGNIZE - NEW PA!
        if msg['type'] in ['audio','voice']:
            audio_bytes = get_media_bytes(msg[msg['type']]['id'])
            b64 = base64.b64encode(audio_bytes).decode()
            parts.append({"text": KAN_PROMPT + " Ithu voice message. Itha kettu purinthu kondu Tamil la badhil sollu. Letter ketta letter ready pannu."})
            parts.append({"inline_data": {"mime_type": "audio/ogg", "data": b64}})

        # 2. IMAGE - Blur fix + Syllabus fix
        elif msg['type'] == 'image':
            img_bytes = get_media_bytes(msg['image']['id'])
            b64 = base64.b64encode(img_bytes).decode()
            caption = msg['image'].get('caption','')
            parts.append({"text": KAN_PROMPT + f" Itha photo va theliva padichu Tamil la sollu. User caption: {caption}"})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

        # 3. DOCUMENT / PDF
        elif msg['type'] == 'document':
            doc_bytes = get_media_bytes(msg['document']['id'])
            b64 = base64.b64encode(doc_bytes).decode()
            parts.append({"text": KAN_PROMPT + " Ithu PDF/document. Itha padichu Tamil la explain pannu."})
            parts.append({"inline_data": {"mime_type": "application/pdf", "data": b64}})

        # 4. TEXT - Letter preparation
        else:
            user_text = msg['text']['body']
            parts.append({"text": KAN_PROMPT + f" User kelvi: {user_text}. Letter/document venumna full formal letter ready pannu. Illaina Tamil la explain pannu."})

        # Gemini Call
        model = genai.GenerativeModel("gemini-flash-latest")
        resp = model.generate_content({"parts": parts}, generation_config={"temperature":0.3})
        final_text = clean_tamil(resp.text)
        
        send_whatsapp(from_num, final_text)

    except Exception as e:
        print("Error:", e)
    
    return {"ok": True}
