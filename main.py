from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
import requests, os, base64, fitz, uuid
from gtts import gTTS

app = FastAPI()
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_ID = os.environ.get("PHONE_NUMBER_ID")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

processed_ids = set()

@app.get("/")
def home(): return {"status":"Live with PDF pa"}
@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge"))
    return PlainTextResponse("fail", 403)
@app.head("/")
def head(): return {"ok": True}

def do_work(from_num: str, msg: dict, msg_id: str):
    if msg_id in processed_ids:
        return
    processed_ids.add(msg_id)
    if len(processed_ids) > 100: processed_ids.clear()
    
    tamil_reply = ""
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={GEMINI_KEY}"
    payload = None

    try:
        # 1. IMAGE CASE
        if msg['type'] == 'image':
            media_id = msg['image']['id']
            m_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
            img_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            b64_img = base64.b64encode(img_bytes).decode('utf-8')
            payload = {"contents": [{"parts": [{"text": "Nee Kan Paarvai. Itha Tamil la 2-4 varila short-a sollu. Maths iruntha x square, veru nu pesara maathiri sollu."}, {"inline_data": {"mime_type": "image/jpeg", "data": b64_img}}]}]}

        # 2. PDF CASE - PUTHUSU PA!
        elif msg['type'] == 'document':
            media_id = msg['document']['id']
            filename = msg['document'].get('filename','book.pdf')
            m_url = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).json()['url']
            pdf_bytes = requests.get(m_url, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}).content
            
            # PDF la irunthu text edukkrom
            tmp_path = f"/tmp/{uuid.uuid4()}.pdf"
            with open(tmp_path, 'wb') as f: f.write(pdf_bytes)
            
            doc = fitz.open(tmp_path)
            pdf_text = ""
            for page in doc[:5]: # First 5 page mattum - token save panna
                pdf_text += page.get_text()[:4000]
            doc.close()
            
            if not pdf_text.strip():
                pdf_text = "PDF la text illa, scanned image maathiri irukku."
            
            prompt = f"Nee Kan Paarvai. Ithu oru PDF file ({filename}) la irunthu edutha text. Itha Tamil la simple-a 4-6 varila summarize pannu. Maths iruntha Tamil la pesara maathiri sollu. Text:\n\n{pdf_text[:8000]}"
            payload = {"contents": [{"parts": [{"text": prompt}]}]}

        else:
            tamil_reply = "Photo illa PDF anupunga pa!"

        # Gemini ku anuppu
        if payload:
            g_res = requests.post(gemini_url, json=payload).json()
            print(f"GEMINI: {g_res}")
            if 'candidates' in g_res:
                tamil_reply = g_res['candidates'][0]['content']['parts'][0]['text'][:700]
            else:
                tamil_reply = "PDF read panna mudiyala pa, file perusa irukka?"

        if not tamil_reply: return

        # TEXT SEND
        requests.post(f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
            headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": from_num, "text": {"body": tamil_reply}})

        # VOICE SEND
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
        from_num = msg['from']
        msg_id = msg.get('id', '')
        background_tasks.add_task(do_work, from_num, msg, msg_id)
    except Exception as e:
        print(f"Error: {e}")
    return {"ok": True}
