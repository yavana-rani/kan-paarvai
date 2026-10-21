from fastapi import FastAPI, Request, BackgroundTasks
import os, requests, tempfile
from PIL import Image
from gtts import gTTS
from io import BytesIO
import fitz
from fpdf import FPDF  # <--- CREATE KU
from google import genai
KAN_PAARVAI_PROMPT = """
Nee Kan Paarvai, paarvaiyatra nanban. Nee ippo oru photo-va paakara.

UN RULES - KANDIPA FOLLOW PANNU:
1. Bathil eppothum Tamil la mattum, 2-4 varila SHORT-A, CRISP-A sollanum.
2. Photo la maths formula iruntha:
   - Symbol-a pesara maathiri maathu:
     x² -> "x square", x/2 -> "x by two", √ -> "veru", = -> "equal to", + -> "plus", - -> "minus"
     Example: E=mc² -> "E equal to m c square"
   - Step-a short-a sollu.
3. Maths illa, normal photo-na:
   - Yaar, enna dress, enna panrar nu mattum sollu. Katha vendaam.
4. Romba vilakkam, English, emoji vendaam. Simple Tamil mattum.

Ippo intha photo-ku Tamil la short-a bathil sollu.
"""

app = FastAPI()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "kanpaarvai123")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GEMINI_MODEL_NAME = "gemini-3.8-flash"

def send_whatsapp(to, text):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text[:4000]}}
    try: requests.post(url, headers=headers, json=payload, timeout=15)
    except: pass

def send_whatsapp_audio(to, audio_path):
    try:
        url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/media"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        with open(audio_path, 'rb') as f:
            files = {'file': (os.path.basename(audio_path), f, 'audio/mpeg')}
            data = {'type': 'audio/mpeg', 'messaging_product': 'whatsapp'}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            media_id = r.json().get('id')
        url2 = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
        headers2 = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": media_id}}
        requests.post(url2, headers=headers2, json=payload, timeout=15)
    except Exception as e: print(f"Audio send error: {e}")

def send_whatsapp_document(to, doc_path, filename="KanPaarvai_Letter.pdf"):
    try:
        url = f"https://graph.facebook.com/v26.0/{PHONE_NUMBER_ID}/media"
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        with open(doc_path, 'rb') as f:
            files = {'file': (filename, f, 'application/pdf')}
            data = {'type': 'document', 'messaging_product': 'whatsapp'}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=30)
            media_id = r.json().get('id')
        url2 = f"https://graph.facebook.com/v26.0/{PHONE_NUMBER_ID}/messages"
        headers2 = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
        payload = {"messaging_product": "whatsapp", "to": to, "type": "document", "document": {"id": media_id, "filename": filename}}
        requests.post(url2, headers=headers2, json=payload, timeout=15)
    except Exception as e: print(f"Doc send error: {e}")

def create_tamil_voice(text):
    try:
        clean = text.replace('*','').replace('#','')[:800]
        tts = gTTS(text=clean, lang='ta', slow=False)
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        tts.save(path)
        return path
    except: return None

def send_engage_voice(to, text):
    vp = create_tamil_voice(text)
    if vp:
        send_whatsapp_audio(to, vp)
        try: os.remove(vp)
        except: pass

def create_letter_pdf(content_text):
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("Noto", "", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", uni=True) # Tamil support try
    pdf.set_font("Arial", size=12)
    # Tamil font illa-na English la varum, aana content Tamil la irukkum
    pdf.multi_cell(0, 10, content_text)
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    pdf.output(path)
    return path

# ... (handle_image, handle_pdf, handle_voice same as before) ...
def handle_image(from_number, image_id):
    try:
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        image_url = requests.get(f"https://graph.facebook.com/v26.0/{image_id}", headers=headers, timeout=15).json().get("url")
        img_data = requests.get(image_url, headers=headers, timeout=20).content
        image = Image.open(BytesIO(img_data))
        send_engage_voice(from_number, "Photo kidaichiduchu pa, paarkuren")
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=["Tamil-il vilakku", image])
        response = model.generate_content([KAN_PAARVAI_PROMPT, image])
        answer = response.text
        send_whatsapp(from_number, answer)
        send_engage_voice(from_number, answer)
    except Exception as e: print(e)

def handle_pdf(from_number, doc_id):
    try:
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        doc_url = requests.get(f"https://graph.facebook.com/v26.0/{doc_id}", headers=headers, timeout=15).json().get("url")
        send_engage_voice(from_number, "PDF kidaichiduchu konjam wait pannunga process panren")
        pdf_data = requests.get(doc_url, headers=headers, timeout=30).content
        doc = fitz.open(stream=pdf_data, filetype="pdf")
        full = ""
        for i in range(min(3, len(doc))):
            pix = doc[i].get_pixmap(dpi=200)
            image = Image.open(BytesIO(pix.tobytes("png")))
            resp = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=["Tamilil vilakku", image])
            full += resp.text + "\n"
        send_whatsapp(from_number, full[:4000])
        send_engage_voice(from_number, full)
    except Exception as e: print(e)

def handle_voice(from_number, audio_id):
    try:
        headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
        audio_url = requests.get(f"https://graph.facebook.com/v26.0/{audio_id}", headers=headers, timeout=15).json().get("url")
        audio_data = requests.get(audio_url, headers=headers, timeout=20).content
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        open(path, "wb").write(audio_data)
        send_engage_voice(from_number, "Voice kidaichiduchu process panren konjam wait pannunga ")
        myfile = gemini_client.files.upload(file=path)
        response = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=["Intha voice-a purinthu enna ketkiranga sollunga", myfile])
        answer = response.text
        os.remove(path)
        send_whatsapp(from_number, answer)
        send_engage_voice(from_number, answer)
    except Exception as e: print(e)

def handle_text_create(from_number, text):
    # Letter create pannuma nu check
    keywords = ["letter", "create", "document", "petition", "manu", "application"]
    if any(k in text.lower() for k in keywords):
        send_engage_voice(from_number, "Letter create pannuren ,irunga oru nimisham")
        try:
            # Gemini kita letter content vaangu
            prompt = f"User ketta letter: {text}. Itha formal Tamil/English letter-a create pannu. Date, To, Subject, Body ellam vachu. Simple."
            resp = gemini_client.models.generate_content(model=GEMINI_MODEL_NAME, contents=[prompt])
            letter_content = resp.text
            
            pdf_path = create_letter_pdf(letter_content)
            send_whatsapp(from_number, f"Letter ready ! \n\n{letter_content[:2000]}")
            send_whatsapp_document(from_number, pdf_path, "KanPaarvai_Letter.pdf")
            send_engage_voice(from_number, "Letter PDF ready , ungalukku anupiten")
            os.remove(pdf_path)
            return True
        except Exception as e:
            print(f"Create PDF error: {e}")
    return False

@app.get("/")
async def root(): return {"status": "Kan Paarvai LIVE - Read + Create PDF - 3.8 Flash"}

@app.get("/webhook")
async def verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge", "0"))
    return {"error": "invalid"}

@app.post("/webhook")
async def incoming(request: Request, bg: BackgroundTasks):
    body = await request.json()
    try:
        value = body["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return {"ok": True}
        msg = value["messages"][0]
        from_number = msg["from"]
        if msg["type"] == "image":
            bg.add_task(handle_image, from_number, msg["image"]["id"])
        elif msg["type"] == "document":
            bg.add_task(handle_pdf, from_number, msg["document"]["id"])
        elif msg["type"] == "audio":
            bg.add_task(handle_voice, from_number, msg["audio"]["id"])
        elif msg["type"] == "text":
            txt = msg["text"]["body"]
            # Create check
            if not handle_text_create(from_number, txt):
                def welcome():
                    t = "Vanakkam! Photo, PDF anupunga padippen. Letter venumna 'collector ku letter venum' nu sollunga, naan PDF create panni tharen!"
                    send_whatsapp(from_number, t)
                    send_engage_voice(from_number, t)
                bg.add_task(welcome)
    except Exception as e: print(e)
    return {"ok": True}
