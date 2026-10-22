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

def clean_tamil(text):
    if not text:
        return ""
    text = text.replace('$','').replace('*','').replace('`','').replace('#','').replace('_','')
    return text.strip()

KAN_PROMPT = "Nee Kan Paarvai. Tamil la simple-a pesu pa. Photo document la enna irukku nu Tamil la sollu pa."

def send_text(to, txt):
    try:
        txt = clean_tamil(txt)[:3000]
        if not txt:
            txt = "Thirumba anupunga pa"
        requests.post(
            f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": to, "text": {"body": txt}}
        )
    except Exception:
        pass

def get_media_url(media_id):
    try:
        meta = requests.get(
            f"https://graph.facebook.com/v20.0/{media_id}",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        ).json()
        fname = str(meta.get('file_name','')).lower()
        if fname.endswith(('.exe','.bat','.apk','.js','.sh','.msi')):
            return None, "malware"
        if int(meta.get('file_size',0)) > 10*1024*1024:
            return None, "big"
        return meta.get('url'), "safe"
    except Exception:
        return None, "error"

@app.api_route("/", methods=["GET", "HEAD"])
def home(request: Request):
    if request.method == "HEAD":
        return PlainTextResponse("ok", 200)
    return {"status": "Kan Paarvai Live Final"}

@app.get("/webhook")
def verify(request: Request):
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if token == VERIFY_TOKEN:
        return PlainTextResponse(challenge)
    return PlainTextResponse("fail", 403)

def do_work(from_num, msg, msg_id):
    if msg_id in processed_ids:
        return
    processed_ids.add(msg_id)
    if len(processed_ids) > 100:
        processed_ids.clear()

    payload = None
    try:
        if msg['type'] in ['image','document','audio','voice']:
            send_text(from_num, "Pa file vandhuchu pa! Padikuren pa...")

        if msg['type'] == 'image':
            m_url, status = get_media_url(msg['image']['id'])
            if status != "safe":
                send_text(from_num, "Pa photo safe illa pa!")
                return
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=30).content
            b64 = base64.b64encode(b).decode()
            payload = {
                "contents": [{
                    "parts": [
                        {"text": KAN_PROMPT},
                        {"inline_data": {"mime_type": "image/jpeg", "data": b64}}
                    ]
                }]
            }

        elif msg['type'] == 'document':
            m_url, status = get_media_url(msg['document']['id'])
            if status != "safe":
                send_text(from_num, "Pa file safe illa pa!")
                return
            doc_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=30).content
            fname = msg['document'].get('filename','doc').lower()
            extracted = ""
            if fname.endswith(('.html','.htm')):
                extracted = doc_bytes.decode('utf-8', errors='ignore')[:8000]
            else:
                tp = f"/tmp/{uuid.uuid4()}.pdf"
                with open(tp,'wb') as f:
                    f.write(doc_bytes)
                doc = fitz.open(tp)
                for page in doc:
                    extracted = extracted + page.get_text() + "\n"
                    if len(extracted) > 8000:
                        break
                doc.close()
            payload = {
                "contents": [{
                    "parts": [{"text": KAN_PROMPT + " File " + fname + " " + extracted[:8000]}]
                }]
            }

        elif msg['type'] == 'text':
            q = msg['text']['body']
            payload = {
                "contents": [{
                    "parts": [{"text": KAN_PROMPT + " Kelvi " + q}]
                }]
            }

        elif msg['type'] in ['audio','voice']:
            m_url, _ = get_media_url(msg[msg['type']]['id'])
            b = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, timeout=30).content
            b64 = base64.b64encode(b).decode()
            payload = {
                "contents": [{
                    "parts": [
                        {"text": KAN_PROMPT},
                        {"inline_data": {"mime_type": "audio/ogg", "data": b64}}
                    ]
                }]
            }

        if payload is None:
            return

        models_to_try = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-1.5-flash"]
        reply = None

        for model_name in models_to_try:
            for attempt in range(2):
                try:
                    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={GEMINI_KEY}"
                    print(f"Trying {model_name} attempt {attempt+1}")
                    r = requests.post(url, json=payload, timeout=60).json()
                    if 'candidates' in r:
                        if len(r['candidates']) > 0:
                            reply = clean_tamil(r['candidates'][0]['content']['parts'][0]['text'])
                            print(f"SUCCESS with {model_name}")
                            break
                    err = r.get('error', {})
                    code = err.get('code', 0)
                    print(f"{model_name} failed {code} {err.get('message','')}")
                    if code in [503, 429, 500, 504]:
                        time.sleep(2)
                        continue
                    if code == 404:
                        break
                except Exception as e:
                    print(f"{model_name} exception {e}")
                    time.sleep(2)
                    continue
            if reply is not None:
                break

        if reply is not None:
            send_text(from_num, reply)
        else:
            send_text(from_num, "Pa Google server busy pa! 1 nimisham kalichu anupunga pa!")

    except Exception as e:
        print(f"Work Error {e}")
        try:
            send_text(from_num, "Pa chinna error pa, thirumba anupunga pa!")
        except Exception:
            pass

@app.post("/webhook")
async def receive(request: Request, background_tasks: BackgroundTasks):
    data = await request.json()
    try:
        entry = data['entry'][0]['changes'][0]['value']
        if 'messages' in entry:
            msg = entry['messages'][0]
            from_num = msg['from']
            msg_id = msg.get('id','')
            background_tasks.add_task(do_work, from_num, msg, msg_id)
    except Exception:
        pass
    return {"ok": True}
