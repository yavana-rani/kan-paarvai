from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import requests, os, base64, fitz, uuid, re
from gtts import gTTS

app = FastAPI()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

processed_ids = set()

def clean_tamil(text: str) -> str:
    if not text: return "Puriyala pa"
    text = text.replace('$', '').replace('*', '').replace('`', '').replace('#', '').replace('_', '')
    text = text.replace('^2', ' square').replace('^3', ' cube')
    text = re.sub(r'x2', 'x square', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

KAN_PROMPT = """
Nee Kan Paarvai. Tamil la mattum pesu.
MUKKIYAM: Dollar ($), Star (*), Hash (#), LaTeX, ^ symbol, markdown ellam use panna koodathu. Plain Tamil mattum.
Maths iruntha 'x square', 'kooda', 'kazhithal', 'samakkuRi' nu Tamil vaartha la sollu.
Letter/document ketta neeye formal letter ready panni tharanum.
"""

def send_text(to, txt):
    txt = clean_tamil(txt)[:3500]
    requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "text": {"body": txt}})

def send_voice(to, txt):
    try:
        txt = clean_tamil(txt)[:700]
        file_name = f"/tmp/{uuid.uuid4()}.mp3"
        gTTS(text=txt, lang='ta', slow=False).save(file_name)
        with open(file_name, 'rb') as f:
            up = requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                files={"file": ("voice.mp3", f, "audio/mpeg")},
                data={"type": "audio/mpeg", "messaging_product": "whatsapp"}).json()
        if 'id' in up:
            requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                json={"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": up['id']}})
    except Exception as ve:
        print(f"Voice Error: {ve}")

def get_media_url(media_id):
    try:
        meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
        # MALWARE ALERT CHECK
        mime = meta.get('mime_type','')
        fname = str(meta.get('file_name','')).lower()
        if any(x in mime or x in fname for x in ['.exe','.bat','.sh','.apk','.dll','application/x-msdownload']):
            return None, "malware"
        return meta['url'], "safe"
    except:
        return None, "error"

@app.get("/")
def home(): return {"status":"Live clean Tamil pa - Final"}
@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)
@app.head("/")
def head(): return {"ok": True}

def do_work(from_num: str, msg: dict, msg_id: str):
    if msg_id in processed_ids: return
    processed_ids.add(msg_id)
    if len(processed_ids) > 100: processed_ids.clear()
    
    tamil_reply = ""
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_KEY}"
    payload = None

    try:
        # 1. IMAGE
        if msg['type'] == 'image':
            m_url, status = get_media_url(msg['image']['id'])
            if status == "malware":
                send_text(from_num, "Pa, intha file la virus maathiri irukku pa! Open panna venda pa! ⚠️ Malware Alert!")
                return
            img_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64_img = base64.b64encode(img_bytes).decode('utf-8')
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}]}]}

        # 2. PDF READ + MALWARE
        elif msg['type'] == 'document':
            m_url, status = get_media_url(msg['document']['id'])
            if status == "malware":
                send_text(from_num, "Pa, intha document dangerous-a irukku pa! .exe/.apk file maathiri irukku, download panna venda pa! ⚠️ Malware Alert!")
                return
            pdf_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            tmp_path = f"/tmp/{uuid.uuid4()}.pdf"
            with open(tmp_path, 'wb') as f: f.write(pdf_bytes)
            doc = fitz.open(tmp_path)
            pdf_text = "".join([page.get_text()[:4000] for page in doc[:5]])
            doc.close()
            if not pdf_text.strip(): pdf_text = "Scanned PDF, photo maathiri irukku"
            prompt = f"{KAN_PROMPT}\nIthu PDF text: {pdf_text[:8000]}. Itha Tamil la summary sollu."
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

        # 3. VOICE KU RESPONSE - NEW
        elif msg['type'] in ['audio','voice']:
            media_id = msg[msg['type']]['id']
            m_url, status = get_media_url(media_id)
            audio_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64_audio = base64.b64encode(audio_bytes).decode('utf-8')
            prompt = KAN_PROMPT + " Ithu oru voice message. Enna pesanraanga nu purinju Tamil la badhil sollu."
            payload = {"contents": [{"parts": [{"text": prompt}, {"inline_data": {"mime_type": "audio/ogg", "data": b64_audio}}]}]}

        # 4. TEXT + DOCUMENT CREATION - NEW
        elif msg['type'] == 'text':
            user_text = msg['text']['body']
            # Check for malware links
            if any(x in user_text.lower() for x in ['.exe download','.apk download','free money hack']):
                send_text(from_num, "Pa, intha link/message suspicious-a irukku pa! Click panna venda pa! ⚠️ Malware Alert!")
                return

            if any(w in user_text.lower() for w in ['letter','application','resume','petition','certificate','document create','leave letter']):
                prompt = f"{KAN_PROMPT}\nUser request: {user_text}. Full formal letter/document-a ready panni tharanum. Plain Tamil/English la, no $ * symbols."
            else:
                prompt = f"{KAN_PROMPT}\nUser kelvi: {user_text}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

        # GEMINI CALL
        if payload:
            g_res = requests.post(gemini_url, json=payload).json()
            if 'candidates' in g_res:
                raw = g_res['candidates'][0]['content']['parts'][0]['text']
                tamil_reply = clean_tamil(raw)
            else:
                tamil_reply = "Puriyala pa, konjam theliva anupunga pa"

        if not tamil_reply: return

        # Responding code - Text + Voice rendu anuppuvom
        send_text(from_num, tamil_reply)
        send_voice(from_num, tamil_reply[:700])

    except Exception as e:
        print(f"Work Error: {e}")

@app.post("/webhook")
async def receive(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' not in entry: return {"ok": True}
        msg = entry['messages'][0]
        background_tasks.add_task(do_work, msg['from'], msg, msg.get('id',''))
    except: pass
    return {"ok": True}
