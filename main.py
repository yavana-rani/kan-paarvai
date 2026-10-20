from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi import Response
import requests, base64, os, io
import openai
from gtts import gTTS
from fpdf import FPDF
import base64
import requests

   headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
def handle_image(phone: str, image_id: str):
    print(f"Handling image {image_id}", flush=True)
    image_bytes = download_image(image_id)
    if not image_bytes:
        send_whatsapp(phone, "Padam pathivirakka mudiyavillai. Meendum muyarchikkavum")
        return

    # === SAFETY CHECK ===
    unsafe, tamil_alert = is_unsafe_image(image_bytes)
    if unsafe:
        print(f"Blocked unsafe image: {tamil_alert}", flush=True)
        send_whatsapp(phone, f"⚠️ {tamil_alert}")
        voice = create_tamil_voice(tamil_alert)
        if voice:
            send_whatsapp_audio(phone, voice)
        return  # STOP - don't process further
    
    if tamil_alert == "blurry":
        msg = "Padam migavum iruttaga ullathu. Vilakku potu, thelivaga oru padam edukkavum"
        send_whatsapp(phone, msg)
        voice = create_tamil_voice(msg)
        if voice:
            send_whatsapp_audio(phone, voice)
        # continue anyway or return - your choice
def is_unsafe_image(image_bytes: bytes) -> tuple[bool, str]:
    """
    Check if image is malware / inappropriate / not useful
    Returns: (is_unsafe, reason_tamil)
    """
    try:
        # 1. Basic file check - too small or not image
        if len(image_bytes) < 1024:
            return True, "Padam alavu migach siriyathu. Thelivana padam edukkavum"

        # 2. Use OpenAI Vision to classify
        b64 = base64.b64encode(image_bytes).decode()
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": 
                     "Classify this image for a blind assistance app. "
                     "Is it unsafe? Check: porn/nudity, gore, weapon, QR code promising free money/hack, "
                     "malware screenshot, abuse, or completely black/blurry with no info. "
                     "Reply ONLY JSON: {\"unsafe\": true/false, \"reason_en\": \"short reason\"} "
                     "If unsafe = true for NSFW/malware/scams. If just blurry/dark, unsafe=false but reason_en='blurry'"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
                ]
            }],
            max_tokens=100
        )
        import json
        result = json.loads(resp.choices[0].message.content)
        
        if result.get("unsafe"):
            # Map to Tamil alert
            reason = result.get("reason_en","unsafe")
            if "porn" in reason.lower() or "nude" in reason.lower():
                tamil = "Ethu thagatha padam. Ithu thavirka pattathu"
            elif "malware" in reason.lower() or "qr" in reason.lower() or "scam" in reason.lower():
                tamil = "Echcharikkai! Ithu mosadi padam pola therigirathu. Click pannatheenga. Padathil mosamana thagaval irukkalam"
            else:
                tamil = f"Echcharikkai! Ithu pathukappatra padam alla: {reason}"
            return True, tamil
        
        # 3. If blurry/dark - not unsafe but warn
        if "blurry" in result.get("reason_en","").lower():
            return False, "blurry"  # special handle
            
        return False, ""
    except Exception as e:
        print(f"Safety check failed: {e}", flush=True)
        return False, ""  # fail open - don't block if check fails

# --- Create folders first ---
os.makedirs("static", exist_ok=True)
os.makedirs("static/audio", exist_ok=True)
os.makedirs("/tmp", exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None)

# Mount static AFTER app created
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Config ---
API = "https://graph.facebook.com/v22.0"
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()

client = openai.OpenAI(api_key=OPENAI_API_KEY, timeout=60.0)

# --- Tamil Font Auto-Download ---
FONT_PATH = "/tmp/NotoSansTamil-Regular.ttf"
FONT_URL = "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansTamil/NotoSansTamil-Regular.ttf"

def ensure_tamil_font():
    if not os.path.exists(FONT_PATH):
        try:
            print("Downloading Tamil font...")
            r = requests.get(FONT_URL, timeout=30)
            if r.status_code == 200:
                with open(FONT_PATH, "wb") as f:
                    f.write(r.content)
                print("Tamil font downloaded")
        except Exception as e:
            print(f"Font download error: {e}")

ensure_tamil_font()

def get_headers():
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"}

def get_phone_id():
    return os.getenv("PHONE_NUMBER_ID", "").strip()

def upload_media(media_bytes, mime_type):
    phone_id = get_phone_id()
    token = os.getenv("WHATSAPP_TOKEN", "").strip()
    url = f"{API}/{phone_id}/media"
    try:
        files = {'file': ('file', media_bytes, mime_type)}
        data = {'messaging_product': 'whatsapp'}
        headers = {"Authorization": f"Bearer {token}"}
        r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
        print(f"UPLOAD {r.status_code}: {r.text[:500]}")
        if r.status_code == 200:
            return r.json().get("id")
    except Exception as e:
        print(f"Upload error: {e}")
    return None

def send_whatsapp(to, text):
    phone_id = get_phone_id()
    headers = {**get_headers(), "Content-Type": "application/json"}
    for i in range(0, len(text), 4000):
        chunk = text[i:i+4000]
        try:
            requests.post(f"{API}/{phone_id}/messages", headers=headers,
                json={"messaging_product": "whatsapp","to": to,"type": "text","text": {"body": chunk}}, timeout=20)
        except Exception as e:
            print(f"Send failed: {e}")

def send_whatsapp_audio(to, audio_bytes):
    media_id = upload_media(audio_bytes, "audio/mpeg")
    if not media_id: return
    phone_id = get_phone_id()
    headers = {**get_headers(), "Content-Type": "application/json"}
    requests.post(f"{API}/{phone_id}/messages", headers=headers,
        json={"messaging_product": "whatsapp","to": to,"type": "audio","audio": {"id": media_id}}, timeout=20)

def send_whatsapp_pdf(to, pdf_bytes, filename="KanPaarvai.pdf"):
    media_id = upload_media(pdf_bytes, "application/pdf")
    if not media_id: return
    phone_id = get_phone_id()
    headers = {**get_headers(), "Content-Type": "application/json"}
    requests.post(f"{API}/{phone_id}/messages", headers=headers,
        json={"messaging_product": "whatsapp","to": to,"type": "document","document": {"id": media_id, "filename": filename}}, timeout=20)

def create_tamil_voice(text):
    short_text = text[:500]
    if SARVAM_API_KEY:
        try:
            print("Trying Sarvam AI Tamil voice...")
            resp = requests.post(
                "https://api.sarvam.ai/text-to-speech",
                headers={"api-subscription-key": SARVAM_API_KEY},
                json={
                    "inputs": [short_text],
                    "target_language_code": "ta-IN",
                    "speaker": "meera",
                    "pitch": 0, "pace": 1.0, "loudness": 1.0,
                    "speech_sample_rate": 8000,
                    "enable_preprocessing": True,
                    "model": "bulbul:v2"
                }, timeout=30
            )
            if resp.status_code == 200:
                data = resp.json()
                audio_b64 = data.get("audios", [None])[0]
                if audio_b64:
                    return base64.b64decode(audio_b64)
        except Exception as e:
            print(f"Sarvam error: {e}")

    try:
        print("Fallback to gTTS Tamil")
        tts = gTTS(text=short_text, lang='ta', slow=False)
        mp3_fp = io.BytesIO()
        tts.write_to_fp(mp3_fp)
        mp3_fp.seek(0)
        return mp3_fp.read()
    except Exception as e:
        print(f"TTS error: {e}")
        return None

def create_tamil_pdf(content, title="Kan Paarvai - Paarvai Araikkai"):
    try:
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        has_tamil_font = os.path.exists(FONT_PATH)
        if has_tamil_font:
            try:
                pdf.add_font("NotoTamil", "", FONT_PATH, uni=True)
                pdf.set_font("NotoTamil", '', 14)
            except:
                has_tamil_font = False
        if not has_tamil_font:
            pdf.set_font("Arial", 'B', 14)
        pdf.cell(0, 12, title, ln=True, align='C')
        pdf.ln(8)
        if has_tamil_font:
            pdf.set_font("NotoTamil", '', 11)
        else:
            pdf.set_font("Arial", '', 11)
        pdf.multi_cell(0, 7, content)
        pdf.ln(10)
        pdf.set_font("Arial", 'I', 8)
        pdf.cell(0, 10, "Generated by Kan Paarvai | Photos not stored | Privacy safe", ln=True, align='C')
        out = pdf.output(dest='S')
        if isinstance(out, str):
            return out.encode('latin-1')
        return bytes(out)
    except Exception as e:
        print(f"PDF error: {e}")
        return None

def handle_image(from_number, image_id):
    try:
        headers = get_headers()
        info = requests.get(f"{API}/{image_id}", headers=headers, timeout=20).json()
        if "url" not in info:
            send_whatsapp(from_number, "Photo URL kidaikkavillai. Meendum anupunga.")
            return
        img_bytes = requests.get(info["url"], headers=headers, timeout=30).content
        if len(img_bytes) > 4 * 1024 * 1024:
            send_whatsapp(from_number, "Photo romba perisu. Chinna photo anupunga.")
            return
        b64 = base64.b64encode(img_bytes).decode()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user","content": [
                {"type": "text", "text": "You are Kan Paarvai, helpful Tamil eye assistant. Explain image in simple, clear Tamil (pure Tamil). Short, kind, 300 words."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}]
        )
        answer = resp.choices[0].message.content
        send_whatsapp(from_number, answer)
        voice_bytes = create_tamil_voice(answer)
        if voice_bytes:
            send_whatsapp_audio(from_number, voice_bytes)
            send_whatsapp(from_number, "🔊 Mela ulla voice-ai ketkavum")
        pdf_bytes = create_tamil_pdf(answer)
        if pdf_bytes:
            send_whatsapp_pdf(from_number, pdf_bytes, filename="KanPaarvai_Tamil_Report.pdf")
            send_whatsapp(from_number, "📄 PDF report-um anuppi ullen")
    except Exception as e:
        print(f"Image handle error: {e}")
        send_whatsapp(from_number, "Mannikkavum, photo padikka mudiyavillai.")

@app.api_route("/", methods=["GET","HEAD"])
async def home():
    return {"status": "Kan Paarvai LIVE","tamil_font": os.path.exists(FONT_PATH),"sarvam": bool(SARVAM_API_KEY)}

@app.get("/privacy", response_class=HTMLResponse)
async def privacy(): return "<html><body><h1>Privacy</h1><p>Photos not stored. Deleted in 24h.</p></body></html>"

@app.get("/terms", response_class=HTMLResponse)
async def terms(): return "<html><body><h1>Terms</h1><p>Accessibility only.</p></body></html>"

@app.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion(): return "<html><body><h1>Delete</h1><p>Email kanpaarvai.help@gmail.com to delete.</p></body></html>"

@app.post("/webhook")
async def incoming(request: Request, bg: BackgroundTasks):
    print("=== WEBHOOK POST RECEIVED ===", flush=True)
    body = await request.json()
    print(body, flush=True)
    try:
        value = body["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return {"ok": True}
        msg = value["messages"][0]
        from_number = msg["from"]
        if msg["type"] == "image":
            bg.add_task(handle_image, from_number, msg["image"]["id"])
        else:
            bg.add_task(send_whatsapp, from_number, "Vanakkam! 🙏 Photo anupunga, Tamil voice + PDF tharren.")
    except Exception as e:
        print(f"Webhook error: {e}")
    return {"ok": True}

@app.get("/test-whatsapp")
async def test_whatsapp(phone: str = "918248926187", msg: str = "Vanakkam! Kan Paarvai test"):
    """Test without Meta - call from browser"""
    print(f"=== TEST TRIGGER to {phone}: {msg} ===", flush=True)
    try:
        send_whatsapp(phone, msg)
        voice = create_tamil_voice(msg)
        if voice:
            send_whatsapp_audio(phone, voice)
        return {"status": "sent", "to": phone, "message": msg, "voice": bool(voice)}
    except Exception as e:
        return {"error": str(e)}

@app.get("/test-image/{image_id}")
async def test_image(image_id: str, phone: str = "918248926187"):
    """Test image handling with real image_id"""
    print(f"=== TEST IMAGE {image_id} to {phone} ===", flush=True)
    handle_image(phone, image_id)
    return {"status": "processing", "image_id": image_id, "to": phone}
@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    token = params.get("hub.verify_token")
    print(f"VERIFY ATTEMPT: received='{token}' expected='{VERIFY_TOKEN}'")
    if params.get("hub.mode") == "subscribe" and token == VERIFY_TOKEN:
        print("VERIFY SUCCESS!")
        return PlainTextResponse(content=params.get("hub.challenge", "0"), status_code=200)
    print("VERIFY FAILED")
    return PlainTextResponse("Verification failed", status_code=403)
