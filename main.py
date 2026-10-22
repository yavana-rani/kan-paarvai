# main.py - FINAL FIX for PDF + Image + Voice
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import requests, os, base64, uuid, re
from gtts import gTTS

app = FastAPI()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
processed_ids = set()

def clean_tamil(text: str) -> str:
    if not text: return "Puriyala pa"
    text = text.replace('$','').replace('*','').replace('`','').replace('#','').replace('_','')
    text = text.replace('^2',' square').replace('^3',' cube')
    text = re.sub(r'\s+',' ', text)
    return text.strip()

KAN_PROMPT = """
Nee Kan Paarvai. Tamil la pesu. 
RULES:
1. Dollar $, Star *, Hash #, markdown, LaTeX use panna koodathu. Plain Tamil mattum.
2. "Puriyala" "Theliva anupunga" nu eppovume solla koodathu. Low quality-a iruntha kooda enna theriyutho atha Tamil la sollanum.
3. PDF, Syllabus, Circular iruntha Month/Date/Who/What nu point-by-point Tamil la sollu.
4. Letter ketta neeye ready panni kodu.
"""

def send_text(to, txt):
    txt = clean_tamil(txt)[:3500]
    if not txt: txt = "Konjam thirumba anupunga pa"
    requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "text": {"body": txt}})

def send_voice(to, txt):
    try:
        file_name = f"/tmp/{uuid.uuid4()}.mp3"
        gTTS(text=clean_tamil(txt)[:700], lang='ta', slow=False).save(file_name)
        with open(file_name, 'rb') as f:
            up = requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                files={"file": ("voice.mp3", f, "audio/mpeg")},
                data={"type": "audio/mpeg", "messaging_product": "whatsapp"}).json()
        if 'id' in up:
            requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": up['id']}})
    except: pass

def get_media_url(media_id):
    meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
    mime = meta.get('mime_type','')
    if any(x in mime for x in ['exe','apk','x-msdownload','x-sh']):
        return None, "malware"
    return meta.get('url'), "safe"

@app.get("/")
def home(): return {"status":"Final Fixed"}
@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)

def do_work(from_num, msg, msg_id):
    if msg_id in processed_ids: return
    processed_ids.add(msg_id)
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_KEY}"
    payload = None
    try:
        if msg['type'] == 'image':
            m_url, st = get_media_url(msg['image']['id'])
            if st == "malware": 
                send_text(from_num, "⚠️ Malware Alert pa!"); return
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64 = base64.b64encode(b).decode()
            prompt = KAN_PROMPT + "\nIthu oru image (syllabus/photo). Enna irukku nu Tamil la month wise / detail-a sollu. Puriyala nu sollathe."
            payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}]}

        elif msg['type'] == 'document':
            m_url, st = get_media_url(msg['document']['id'])
            if st == "malware":
                send_text(from_num, "⚠️ Malware file pa, open pannatheenga!"); return
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64 = base64.b64encode(b).decode()
            # IMPORTANT FIX: PDF-a apdiye anuppurom, text extract pannala
            prompt = KAN_PROMPT + "\nIthu oru PDF circular/document. Scanned-a iruntha kooda OCR panni Tamil la full summary kodu. Date, yaarukku, enna action nu sollu."
            payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "application/pdf", "data": b64}}]}]}

        elif msg['type'] in ['audio','voice']:
            m_url, st = get_media_url(msg[msg['type']]['id'])
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64 = base64.b64encode(b).decode()
            prompt = KAN_PROMPT + "\nIthu oru voice message. Kettu Tamil la badhil sollu."
            payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "audio/ogg", "data": b64}}]}]}

        elif msg['type'] == 'text':
            txt = msg['text']['body']
            if any(x in txt.lower() for x in ['letter','application','resume','certificate']):
                prompt = KAN_PROMPT + f"\nLetter request: {txt}. Full formal letter ready pannu."
            else:
                prompt = KAN_PROMPT + f"\nUser kelvi: {txt}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

        if payload:
            g_res = requests.post(gemini_url, json=payload).json()
            if 'candidates' in g_res:
                raw = g_res['candidates'][0]['content']['parts'][0]['text']
                reply = clean_tamil(raw)
                send_text(from_num, reply)
                send_voice(from_num, reply)
            else:
                print(g_res)
                send_text(from_num, "Pa konjam time edukkuthu, antha image-a original quality la anupunga pa, naan padichu solluren pa!")

    except Exception as e:
        print(f"Error: {e}")
        send_text(from_num, "Pa oru chinna error pa, thirumba oru vaati anupunga pa!")

@app.post("/webhook")
async def receive(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        msg = data['entry'][0]['changes'][0]['value']['messages'][0]
        background_tasks.add_task(do_work, msg['from'], msg, msg.get('id',''))
    except: pass
    return {"ok": True}
