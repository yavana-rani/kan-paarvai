from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import requests, os, base64, uuid, re
import fitz
from gtts import gTTS

app = FastAPI()

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
processed_ids = set()

# --- SAFETY LIST PA ---
BLOCKED_EXT = ('.exe','.bat','.cmd','.js','.apk','.sh','.msi','.dll','.vbs','.scr','.com','.pif')
BLOCKED_MIMES = ['exe','apk','x-msdownload','octet-stream','x-sh','x-msdos','application/x-ms']

def clean_tamil(text: str) -> str:
    if not text: return ""
    text = text.replace('$','').replace('*','').replace('`','').replace('#','').replace('_','')
    text = text.replace('^2',' square').replace('^3',' cube')
    text = re.sub(r'\s+',' ', text)
    return text.strip()

KAN_PROMPT = """Nee Kan Paarvai. Tamil la pesu. Dollar $, Star *, markdown koodathu. Puriyala nu solla koodathu. Simple Tamil la blind student ku puriyara maathiri sollu pa."""

def send_text(to, txt):
    txt = clean_tamil(txt)[:3500] or "Thirumba anupunga pa"
    requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "text": {"body": txt}})

# --- FIXED VOICE: NO BREAK PA ---
def send_voice(to, txt):
    try:
        clean = clean_tamil(txt)
        if not clean: return

        # Sentence-a pirikuren pa - naduvula cut aagathu pa
        sentences = re.split(r'(?<=[.!?।|])\s+', clean)
        chunks = []
        current = ""
        
        for sent in sentences:
            if len(current) + len(sent) < 450:
                current += " " + sent
            else:
                if current: chunks.append(current.strip())
                current = sent
        
        if current: chunks.append(current.strip())
        
        # Max 3 voice mattum - VI ku easy pa
        if len(chunks) > 3:
            chunks = chunks[:2] + [" ".join(chunks[2:])[:450] + " pa! Meethi text la padichikonga pa!"]

        for chunk in chunks:
            if not chunk: continue
            file_name = f"/tmp/{uuid.uuid4()}.mp3"
            gTTS(text=chunk, lang='ta', slow=False).save(file_name)
            
            with open(file_name, 'rb') as f:
                up = requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                    files={"file": ("voice.mp3", f, "audio/mpeg")},
                    data={"type": "audio/mpeg", "messaging_product": "whatsapp"}).json()
            
            if 'id' in up:
                requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                    json={"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": up['id']}})
            
            try: os.remove(file_name)
            except: pass

    except Exception as e:
        print(f"Voice Error: {e}")

def get_media_url(media_id):
    try:
        meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
        file_name = meta.get('file_name','').lower()
        mime = meta.get('mime_type','').lower()
        fsize = int(meta.get('file_size',0))
        if file_name.endswith(BLOCKED_EXT): return None, "malware_exe"
        if any(x in mime for x in BLOCKED_MIMES): return None, "malware_mime"
        if fsize > 10*1024*1024: return None, "too_big"
        return meta.get('url'), "safe"
    except: return None, "error"

@app.api_route("/", methods=["GET", "HEAD"])
def home(request: Request):
    if request.method == "HEAD":
        return PlainTextResponse("ok", status_code=200)
    return {"status":"Kan Paarvai Live pa - No Voice Break pa"}

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
            send_text(from_num, "📥 Pa file/voice vandhuchu pa! Check panni padikuren pa, oru nimisham pa...")

        if msg['type'] == 'image':
            m_url, status = get_media_url(msg['image']['id'])
            if status != "safe":
                send_text(from_num, "Pa file romba perusu pa (10MB mela). Chinna file anupunga pa.")
                return
            send_text(from_num, "🧠 Pa photo safe-ah irukku pa! Ipo padikuren pa...")
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=20).content
            b64 = base64.b64encode(b).decode()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}]}

        elif msg['type'] == 'document':
            m_url, status = get_media_url(msg['document']['id'])
            file_name = msg['document'].get('filename','').lower()

            if status == "malware_exe":
                send_text(from_num, f"🚨 Pa {file_name} ithu aabathana file pa (.exe/.apk)! Paathukappukaga thirakka maaten pa! PDF, Photo mattum anupunga pa.")
                return
            if status == "malware_mime":
                send_text(from_num, "🚨 Pa ithu aabathana file type pa! PDF, Photo, HTML mattum anupunga pa.")
                return
            if status == "too_big":
                send_text(from_num, "⚠️ Pa file romba perusu pa (10MB mela). Chinna file anup
