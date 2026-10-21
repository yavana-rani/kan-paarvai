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
    # Dollar, star, markdown ellam thookidum pa!
    text = text.replace('$', '').replace('*', '').replace('`', '').replace('#', '')
    text = text.replace('^2', ' square').replace('^3', ' cube')
    text = re.sub(r'\s+', ' ', text) # extra space clean
    # x2 maathiri iruntha x square nu maathu
    text = re.sub(r'x2', 'x square', text, flags=re.IGNORECASE)
    return text.strip()

KAN_PROMPT = """
Nee Kan Paarvai. Tamil la mattum pesu.
MUKKIYAM: Dollar ($), Star (*), Hash (#), LaTeX, ^ symbol ellam use panna koodathu. Markdown koodathu. Plain Tamil mattum.
Maths iruntha 'x square', 'x cube', 'kooda', 'kazhithal', 'samakkuRi' nu Tamil vaartha la sollu.
Maths illana photo la enna irukku nu simple-a sollu.
"""

@app.get("/")
def home(): return {"status":"Live clean Tamil pa"}
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
        if msg['type'] == 'image':
            media_id = msg['image']['id']
            m_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
            img_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64_img = base64.b64encode(img_bytes).decode('utf-8')
            payload = {"contents": [{"parts": [{"text": KAN_PROMPT}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}]}]}

        elif msg['type'] == 'document':
            media_id = msg['document']['id']
            m_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
            pdf_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            tmp_path = f"/tmp/{uuid.uuid4()}.pdf"
            with open(tmp_path, 'wb') as f: f.write(pdf_bytes)
            doc = fitz.open(tmp_path)
            pdf_text = "".join([page.get_text()[:4000] for page in doc[:5]])
            doc.close()
            if not pdf_text.strip(): pdf_text = "Scanned PDF"
            prompt = f"{KAN_PROMPT}\nIthu PDF text: {pdf_text[:8000]}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

        if payload:
            g_res = requests.post(gemini_url, json=payload).json()
            if 'candidates' in g_res:
                raw = g_res['candidates'][0]['content']['parts'][0]['text']
                tamil_reply = clean_tamil(raw)[:700] # CLEAN PANNI THAAN ANUPPROM PA!
            else:
                tamil_reply = "Puriyala pa, thelivaana photo anupunga"

        if not tamil_reply: return

        requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": from_num, "text": {"body": tamil_reply}})

        try:
            file_name = f"/tmp/{uuid.uuid4()}.mp3"
            gTTS(text=tamil_reply, lang='ta', slow=False).save(file_name)
            with open(file_name, 'rb') as f:
                up = requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                    files={"file": ("voice.mp3", f, "audio/mpeg")},
                    data={"type": "audio/mpeg", "messaging_product": "whatsapp"}).json()
            if 'id' in up:
                requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                    json={"messaging_product": "whatsapp", "to": from_num, "type": "audio", "audio": {"id": up['id']}})
        except Exception as ve:
            print(f"Voice Error: {ve}")

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
