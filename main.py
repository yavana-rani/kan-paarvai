from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import requests, os, base64, uuid, re, time
import fitz
from gtts import gTTS

app = FastAPI()

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

processed_ids = set()
BLOCKED_EXT = ('.exe','.bat','.cmd','.js','.apk','.sh','.msi','.dll','.vbs','.scr','.com','.pif')
BLOCKED_MIMES = ['exe','apk','x-msdownload','octet-stream','x-sh','x-msdos','application/x-ms']

def clean_tamil(text: str) -> str:
    if not text: return ""
    text = text.replace('$','').replace('*','').replace('`','').replace('#','').replace('_','')
    text = text.replace('^2',' square').replace('^3',' cube')
    text = re.sub(r'\s+',' ', text)
    return text.strip()

KAN_PROMPT = """Nee Kan Paarvai. Nee Tamil Nadu blind students ku help pannura AI pa. Tamil la simple-a pesu pa. Dollar, star, markdown ellam koodathu pa. Puriyala nu solla koodathu. Photo la enna irukku, document la enna irukku nu Tamil la theliva sollu pa."""

def send_text(to, txt):
    txt = clean_tamil(txt)[:3500] or "Thirumba anupunga pa"
    requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "text": {"body": txt}})

def send_voice(to, txt):
    try:
        clean = clean_tamil(txt)
        if not clean: return
        sentences = re.split(r'(?<=[.!?])\s+', clean)
        chunks, cur = [], ""
        for s in sentences:
            if len(cur)+len(s) < 450: cur += " " + s
            else:
                if cur: chunks.append(cur.strip())
                cur = s
        if cur: chunks.append(cur.strip())
        if len(chunks) > 3: chunks = chunks[:3]
        for chunk in chunks:
            if not chunk: continue
            fn = f"/tmp/{uuid.uuid4()}.mp3"
            gTTS(text=chunk, lang='ta', slow=False).save(fn)
            with open(fn,'rb') as f:
                up = requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                    files={"file": ("voice.mp3", f, "audio/mpeg")},
                    data={"type": "audio/mpeg", "messaging_product": "whatsapp"}).json()
            if 'id' in up:
                requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                    json={"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": up['id']}})
            try: os.remove(fn)
            except: pass
    except Exception as e:
        print(f"Voice Error {e}")

def get_media_url(media_id):
    try:
        meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
        fname = meta.get('file_name','').lower()
        mime = meta.get('mime_type','').lower()
        fsize = int(meta.get('file_size',0))
        if fname.endswith(BLOCKED_EXT): return None, "malware_exe"
        if any(x in mime for x in BLOCKED_MIMES): return None, "malware_mime"
        if fsize > 10*1024*1024: return None, "too_big"
        return meta.get('url'), "safe"
    except: return None, "error"

@app.api_route("/", methods=["GET", "HEAD"])
def home(request: Request):
    if request.method == "HEAD": return PlainTextResponse("ok", 200)
    return {"status":"Kan Paarvai Live - Final Project"}

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)

def do_work(from_num, msg, msg_id):
    if msg_id in processed_ids: return
    processed_ids.add(msg_id)
    if len(processed_ids) > 100: processed_ids.clear()
    payload = None
    try:
        if msg['type'] in ['image','document','audio','voice']:
            send_text(from_num, "Pa file/voice vandhuchu pa! Check panni padikuren pa, oru nimisham pa...")

        if msg['type'] == 'image':
            m_url, status = get_media_url(msg['image']['id'])
            if status != "safe":
                send_text(from_num, "Pa ithu safe illa pa! Photo mattum anupunga pa.")
                return
            send_text(from_num, "Pa photo safe-ah irukku pa! Padikuren pa...")
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=30).content
            b64 = base64.b64encode(b).decode()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + " Photo la enna irukku nu Tamil la sollu pa."}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}]}

        elif msg['type'] == 'document':
            m_url, status = get_media_url(msg['document']['id'])
            fname = msg['document'].get('filename','').lower()
            if status == "malware_exe":
                send_text(from_num, f"Pa {fname} aabathana file pa! PDF Photo mattum anupunga pa.")
                return
            if status == "too_big":
                send_text(from_num, "Pa file perusu pa (10MB mela). Chinna file anupunga pa.")
                return
            if not m_url: return
            send_text(from_num, f"Pa {fname} safe pa! Padikuren pa...")
            doc_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=30).content
            extracted = ""
            if fname.endswith(('.html','.htm')):
                extracted = doc_bytes.decode('utf-8', errors='ignore')[:8000]
            else:
                tp = f"/tmp/{uuid.uuid4()}.pdf"
                with open(tp,'wb') as f: f.write(doc_bytes)
                doc = fitz.open(tp)
                for page in doc:
                    extracted += page.get_text() + "\n"
                    if len(extracted) > 8000: break
                doc.close()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + f" File {fname} {extracted[:8000]}"}]}]}

        elif msg['type'] == 'text':
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + f" Kelvi {msg['text']['body']}"}]}]}
        
        elif msg['type'] in ['audio','voice']:
            m_url,_ = get_media_url(msg[msg['type']]['id'])
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=30).content
            b64 = base64.b64encode(b).decode()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + " Voice Tamil la purinjuko pa."}, {"inline_data": {"mime_type": "audio/ogg", "data": b64}}]}]}

        if not payload: return

        models_to
