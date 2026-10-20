from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
import requests, base64, os, io
from gtts import gTTS
from fpdf import FPDF
from google import genai
from google.genai import types
from PIL import Image
from io import BytesIO

# --- Folders ---
os.makedirs("static", exist_ok=True)
os.makedirs("static/audio", exist_ok=True)
os.makedirs("/tmp", exist_ok=True)

app = FastAPI(docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- Config ---
API = "https://graph.facebook.com/v22.0"
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123").strip()
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()

# Gemini config
gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL_NAME = "gemini-3.8-flash"

# --- Tamil Font ---
FONT_PATH = "/tmp/NotoSansTamil-Regular.ttf"
FONT_URL = "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansTamil/NotoSansTamil-Regular.ttf"
def ensure_tamil_font():
    if not os.path.exists(FONT_PATH):
        try:
            r = requests.get(FONT_URL, timeout=30)
            if r.status_code == 200:
                with open(FONT_PATH, "wb") as f:
                    f.write(r.content)
        except Exception as e:
            print(f"Font error: {e}")
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

def download_image(image_id):
    try:
        headers = get_headers()
        info = requests.get(f"{API}/{image_id}", headers=headers, timeout=20).json()
        print(f"Image info: {info}")
        if "url" not in info:
            return None
        img_bytes = requests.get(info["url"], headers=headers, timeout=30).content
        return img_bytes
    except Exception as e:
        print(f"Download error: {e}")
        return None

def handle_image(from_number, image_id):
    try:
        print(f"Handling image {image_id} from {from_number}", flush=True)
        img_bytes = download_image(image_id)
        if not img_bytes:
            send_whatsapp(from_number, "Padam pathivirakka mudiyavillai. Meendum muyarchikkavum")
            return
        if len(img_bytes) > 4 * 1024 * 1024:
            send_whatsapp(from_number, "Photo romba perisu. Chinna photo anupunga.")
            return

        # Gemini Vision
        image = Image.open(BytesIO(img_bytes))
        prompt = """
        You are Kan Paarvai, helpful Tamil eye assistant for blind people.
        Describe this image in simple, clear Tamil (pure Tamil). 
        Mention objects, danger, location, text in image.
        If QR code / scam / malware, say 'Echcharikkai! Mosadi!'
        Keep 3-4 lines, kind tone.
        """
        response = gemini_client.models.generate_content(
    model=GEMINI_MODEL_NAME,
    contents=[tamil_prompt, image]
)
answer = response.text
        print(f"Gemini answer: {answer[:200]}")

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
        send_whatsapp(from_number, "Mannikkavum, photo padikka mudiyavillai. Meendum muyarchikkavum.")

@app.api_route("/", methods=["GET","HEAD"])
async def home():
    return {"status": "Kan Paarvai LIVE Gemini","tamil_font": os.path.exists(FONT_PATH),"sarvam": bool(SARVAM_API_KEY)}

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
    try:
        value = body["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return {"ok": True}
        msg = value["messages"][0]
        from_number = msg["from"]
        
        if msg["type"] == "image":
            bg.add_task(handle_image, from_number, msg["image"]["id"])
        else:
            # VI WELCOME - TEXT + VOICE BOTH
            welcome_text = "Vanakkam! 🙏 Naan Kan Paarvai. Oru photo anupunga, naan Tamil-il voice-il vilakkam tharren. Photo eduthu anupunga."
            
            def welcome_with_voice():
                send_whatsapp(from_number, welcome_text)
                voice = create_tamil_voice(welcome_text)
                if voice:
                    send_whatsapp_audio(from_number, voice)
            
            bg.add_task(welcome_with_voice)
            
    except Exception as e:
        print(f"Webhook error: {e}")
    return {"ok": True}

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
