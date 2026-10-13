import os, tempfile, base64, requests
from fastapi import FastAPI, Request
from fpdf import FPDF
import easyocr
from openai import OpenAI

app = FastAPI()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
ocr_reader = easyocr.Reader(['ta', 'en'])

@app.get("/")
async def home():
    return {"status": "Kan Paarvai Live"}

@app.get("/webhook")
async def verify(request: Request):
    p = request.query_params
    if p.get("hub.verify_token") == VERIFY_TOKEN:
        return int(p.get("hub.challenge", 0))
    return "Failed"

@app.post("/webhook")
async def handle(request: Request):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' not in entry: return {"ok": True}
        msg = entry['messages'][0]
        from_number = msg['from']
        if msg['type'] == 'image':
            image_id = msg['image']['id']
            image_path = download_media(image_id)
            desc, ocr_text = process_image(image_path)
            send_text(from_number, f"Vanakkam! Padathil: {desc}. Ezhuthu: {ocr_text}")
    except Exception as e:
        print(e)
    return {"ok": True}

def download_media(media_id):
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    info = requests.get(url, headers=headers).json()
    media_url = info['url']
    content = requests.get(media_url, headers=headers).content
    path = tempfile.mktemp(suffix=".jpg")
    open(path, 'wb').write(content)
    return path

def process_image(path):
    try:
        ocr = ocr_reader.readtext(path, detail=0)
        ocr_text = " ".join(ocr) if ocr else "Ezhuthu illai"
    except:
        ocr_text = "OCR mudiyavillai"
    try:
        if client:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
                resp = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role":"user","content":[{"type":"text","text":"Describe in simple Tamil for blind person"},{"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}]}])
                desc = resp.choices[0].message.content
        else:
            desc = "Worksheet padam"
    except:
        desc = "Padam worksheet pola"
    return desc, ocr_text

def send_text(to, text):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    payload = {"messaging_product":"whatsapp","to":to,"text":{"body":text[:1000]}}
    requests.post(url, json=payload, headers={"Authorization":f"Bearer {WHATSAPP_TOKEN}","Content-Type":"application/json"})
