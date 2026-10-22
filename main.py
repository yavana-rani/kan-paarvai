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

def clean_tamil(text: str) -> str:
    if not text: return ""
    text = text.replace('$','').replace('*','').replace('`','').replace('#','').replace('_','')
    text = text.replace('^2',' square').replace('^3',' cube')
    text = re.sub(r'\s+',' ', text)
    return text.strip()

KAN_PROMPT = """Nee Kan Paarvai. Tamil la pesu. Dollar $, Star *, markdown koodathu. Puriyala nu solla koodathu."""

def send_text(to, txt):
    txt = clean_tamil(txt)[:3500] or "Thirumba anupunga pa"
    requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "text": {"body": txt}})

def send_voice(to, txt):
    try:
        file_name = f"/tmp/{uuid.uuid4()}.mp3"
        gTTS(text=clean_tamil(txt)[:700] or "Tamil la solluren pa", lang='ta', slow=False).save(file_name)
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
    try:
        meta = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()
        if any(x in meta.get('mime_type','') for x in ['exe','apk','x-msdownload']): return None, "malware"
        return meta.get('url'), "safe"
    except: return None, "error"

# HEAD + GET rendu venum pa - ithu than fix!
@app.api_route("/", methods=["GET", "HEAD"])
def home(request: Request):
    if request.method == "HEAD":
        return PlainTextResponse("ok", status_code=200)
    return {"status":"Kan Paarvai Live pa"}

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)
def do_work(from_num, msg, msg_id):
    if msg_id in processed_ids: return
    processed_ids.add(msg_id)
    if len(processed_ids) > 100: processed_ids.clear()
    
    # Payload prepare - same as before, but text-a mattum edukkrom
    payload = None
    try:
        if msg['type'] == 'image':
            m_url, _ = get_media_url(msg['image']['id'])
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=20).content
            b64 = base64.b64encode(b).decode()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64}}]}]}

        elif msg['type'] == 'document':
            m_url, _ = get_media_url(msg['document']['id'])
            file_name = msg['document'].get('filename','').lower()
            doc_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=20).content
            
            extracted_text = ""
            if file_name.endswith(('.html','.htm')):
                extracted_text = doc_bytes.decode('utf-8', errors='ignore')[:8000] # 8k mattum
            else:
                tmp_path = f"/tmp/{uuid.uuid4()}.pdf"
                with open(tmp_path, 'wb') as f: f.write(doc_bytes)
                doc = fitz.open(tmp_path)
                for page in doc:
                    extracted_text += page.get_text() + "\n"
                    if len(extracted_text) > 8000: break # Seekram break
                doc.close()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + f" File: {file_name}\n{extracted_text[:8000]}"}]}]}

        elif msg['type'] == 'text':
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + f" Kelvi: {msg['text']['body']}"}]}]}
        elif msg['type'] in ['audio','voice']:
            m_url, _ = get_media_url(msg[msg['type']]['id'])
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=20).content
            b64 = base64.b64encode(b).decode()
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT + " Voice Tamil la"}, {"inline_data": {"mime_type": "audio/ogg", "data": b64}}]}]}

        if not payload: return

        # GEMINI CALL WITH RETRY - ithu than main fix pa
        models_to_try = ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest"]
        reply = None
        
        for model_name in models_to_try:
            for attempt in range(2): # 2 vaati try
                try:
                    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_KEY}"
                    print(f"Trying {model_name} attempt {attempt+1}")
                    g_res = requests.post(gemini_url, json=payload, timeout=60).json() # 60 sec aakiten pa!
                    
                    if 'candidates' in g_res:
                        reply = clean_tamil(g_res['candidates'][0]['content']['parts'][0]['text'])
                        break
                    if g_res.get('error',{}).get('code') in [503, 429, 504]:
                        print(f"{model_name} busy {g_res['error']['code']}")
                        continue
                except requests.exceptions.ReadTimeout:
                    print(f"{model_name} timeout, retrying...")
                    continue
                except Exception as e:
                    print(f"{model_name} error {e}")
                    continue
            if reply: break

        if reply:
            send_text(from_num, reply)
            send_voice(from_num, reply)
        else:
            send_text(from_num, "Pa Google server konjam busy pa! 1 nimisham kalichu thirumba anupunga pa, naan ready-a irukken pa!")

    except Exception as e:
        print(f"Work Error {e}")
        send_text(from_num, "Pa oru chinna error pa, thirumba anupunga pa!")


@app.post("/webhook")
async def receive(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            background_tasks.add_task(do_work, msg['from'], msg, msg.get('id',''))
    except: pass
    return {"ok": True}
