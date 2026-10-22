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
    # Text display ku - azhaga enter-oda irukkum pa
    if not text: return ""
    # Enter-a mattum oru space-a mathuvom, 2 enter iruntha paragraph gap pa
    text = text.replace('$','').replace('*','').replace('`','').replace('#','').replace('_','')
    text = text.replace('\r','')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
    
KAN_PROMPT = "Nee Kan Paarvai. Tamil la simple-a pesu pa. Photo document la enna irukku nu Tamil la sollu pa."
# PROMPT-A INGA MAATHUNGA PA!

SYSTEM_PROMPT = """
You are Kan Paarvai, a helpful assistant for visually impaired users.
RULE: You MUST ALWAYS reply ONLY in Tamil (Tanglish style - Tamil written in English letters is also okay pa).
Never reply in English. Even if user asks in English, you MUST reply in Tamil only pa.
Be friendly and add 'pa' at end of sentences.
"""

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

def clean_for_voice(text):
    # Voice ku mattum - enter, quote ellam full remove pa!
    if not text:
        return ""
    text = text.replace('\n',' ').replace('\r',' ').replace('\t',' ')
    text = text.replace('"',' ').replace("'"," ").replace('(',' ').replace(')',' ')
    text = text.replace('[',' ').replace(']',' ').replace('{',' ').replace('}',' ')
    text = text.replace('$','').replace('*','').replace('`','').replace('#','')
    text = text.replace('/',' ').replace('\\',' ').replace('|',' ')
    text = re.sub(r'\s+',' ', text)
    return text.strip()

def send_voice(to, txt):
    try:
        clean = clean_for_voice(txt)
        if not clean:
            return
        parts = []
        cur = ""
        for sentence in clean.split('.'):
            if not sentence.strip():
                continue
            if len(cur) + len(sentence) < 380:
                cur = cur + sentence + ". "
            else:
                if cur:
                    parts.append(cur.strip())
                cur = sentence + ". "
        if cur:
            parts.append(cur.strip())
        if len(parts) > 4:
            parts = parts[:4]

        for chunk in parts:
            if len(chunk) < 5:
                continue
            fn = f"/tmp/{uuid.uuid4()}.mp3"
            gTTS(text=chunk, lang='ta', slow=False).save(fn)
            with open(fn, 'rb') as f:
                up = requests.post(
                    f"https://graph.facebook.com/v20.0/{PHONE_ID}/media",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"},
                    files={"file": ("voice.mp3", f, "audio/mpeg")},
                    data={"type": "audio/mpeg", "messaging_product": "whatsapp"}
                ).json()
            if 'id' in up:
                requests.post(
                    f"https://graph.facebook.com/v20.0/{PHONE_ID}/messages",
                    headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"},
                    json={
                        "messaging_product": "whatsapp",
                        "to": to,
                        "type": "audio",
                        "audio": {"id": up['id']}
                    }
                )
            try:
                os.remove(fn)
            except:
                pass
    except Exception as e:
        print(f"Voice Error {e}")

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
import fitz  # PyMuPDF
from PIL import Image
import io

def pdf_to_photos_and_read(media_id, token):
    # 1. PDF download (mela sonna function pa)
    pdf_path = download_whatsapp_media(media_id, token) # /tmp/temp.pdf
    
    doc = fitz.open(pdf_path)
    full_text = ""
    
    for i in range(min(len(doc), 3)): # Muthal 3 page mattum pa
        page = doc[i]
        
        # Text irukka nu paarkrom pa
        text = page.get_text().strip()
        
        if len(text) < 50: # Text illa na - Scanned photo pa!
            print(f"Page {i+1} scanned pa, photo va maathuren pa!")
            # PHOTO VA MAATHUREN PA - ITHU THAN TRICK PA!
            pix = page.get_pixmap(dpi=200)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            
            # Itha Gemini vision ku anuppanum pa
            # gemini_model.generate_content([prompt, img])
            page_text = gemini_read_image(img) 
        else:
            page_text = text
            
        full_text += page_text + "\n"
    
    return full_text
# Gemini call pannum edathula pa:
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents=[
        SYSTEM_PROMPT + "\n\nUser content: " + user_text,
        image # if photo
    ]
         )
def gemini_read_image(image):
    # Gemini 2.5 / 3.8 Flash ku image kuduthu padikka veikkrom pa
    response = gemini_client.models.generate_content(
        model="gemini-2.0-flash",
        contents=["Itha full-a Tamil la padichu sollu pa, enna irukku nu theliva:", image]
    )
    return response.text
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
            send_text(from_num, " file vandhuchu pa! Padikuren pa...")

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

        # ---- FIXED VOICE PART PA ----
        if reply is not None:
            send_text(from_num, reply)
            send_voice(from_num, reply)
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
