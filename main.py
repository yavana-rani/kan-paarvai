
import os
import re
import hmac
import hashlib
import logging
import tempfile
import uuid
from io import BytesIO
from pathlib import Path

import requests
import fitz
from PIL import Image
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import PlainTextResponse
from gtts import gTTS
from google import genai
from google.genai import types
from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

# ---------- CONFIGURATION ----------

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")

GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v22.0")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_MB", "10")) * 1024 * 1024
MAX_PDF_PAGES = 10

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")

app = FastAPI(title="Kan Paarvai AI", docs_url=None, redoc_url=None)

client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

SYSTEM_PROMPT = """
நீங்கள் கண் பார்வை குறைபாடு உள்ளவர்களுக்கு உதவும்
Kan Paarvai AI உதவியாளர்.

எப்போதும் எளிய, தெளிவான தமிழில் பதிலளிக்கவும்.
படத்தில் உள்ள பொருட்கள், எழுத்துகள், நிறங்கள்,
சூழல் ஆகியவற்றை இயல்பாக விளக்கவும்.

தெரியாத விவரங்களை ஊகிக்க வேண்டாம்.
ஆவணம் உருவாக்கக் கேட்டால் முழுமையான வரைவை வழங்கவும்.
படிக்க முடியாத எழுத்துகள் இருந்தால் அதைக் குறிப்பிடவும்.
"""

processed_ids = set()


# ---------- TEXT HELPERS ----------

def clean_text(text):
    if not text:
        return ""
    text = text.replace("\r", "")
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_`#]{1,}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:12000]


def clean_voice_text(text):
    text = clean_text(text)
    text = re.sub(r"[{}\[\]<>|\\]", " ", text)
    text = re.sub(r"\s+", " ", text.replace("\n", "। "))
    return text.strip()


# ---------- GEMINI ----------

def ask_gemini(contents):
    if not client:
        raise RuntimeError("GEMINI_API_KEY அமைக்கப்படவில்லை.")

    # Mudhalla namma model, quota thinthu pona backup models pa
    models_to_try = [
        os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        "gemini-2.0-flash",
        "gemini-1.5-flash",
        "gemini-1.5-flash-8b"
    ]

    last_error = None
    for model_name in models_to_try:
        try:
            log.info(f"Trying {model_name} attempt")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                    max_output_tokens=4096,
                ),
            )
            answer = getattr(response, "text", None)
            if answer:
                return clean_text(answer)
        except Exception as e:
            last_error = e
            log.warning(f"{model_name} failed: {e}")
            # 429 quota error na adutha model ku po pa
            if "429" in str(e) or "quota" in str(e).lower():
                continue
            else:
                continue
    
    raise RuntimeError(f"Gemini quota thinthuduchu pa: {last_error}")

# ---------- WHATSAPP API ----------

def auth_headers():
    if not WHATSAPP_TOKEN:
        raise RuntimeError("WHATSAPP_TOKEN அமைக்கப்படவில்லை.")
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


def graph_post(endpoint, payload):
    if not PHONE_NUMBER_ID:
        raise RuntimeError("PHONE_NUMBER_ID அமைக்கப்படவில்லை.")

    response = requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/{endpoint}",
        headers={
            **auth_headers(),
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=40,
    )
    response.raise_for_status()
    return response.json()


def send_text(to, text):
    text = clean_text(text) or "மீண்டும் ஒரு முறை அனுப்புங்கள்."
    graph_post("messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text[:3900],
        },
    })


def upload_media(path, mime):
    with open(path, "rb") as f:
        response = requests.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/media",
            headers=auth_headers(),
            data={
                "messaging_product": "whatsapp",
                "type": mime,
            },
            files={
                "file": (Path(path).name, f, mime)
            },
            timeout=60,
        )

    response.raise_for_status()
    media_id = response.json().get("id")
    if not media_id:
        raise RuntimeError("WhatsApp media ID கிடைக்கவில்லை.")
    return media_id


def send_audio(to, path):
    media_id = upload_media(path, "audio/mpeg")
    graph_post("messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": {"id": media_id},
    })


def send_pdf(to, path):
    media_id = upload_media(path, "application/pdf")
    graph_post("messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": "KanPaarvai_Tamil.pdf",
            "caption": "உங்கள் தமிழ் PDF ஆவணம்",
        },
    })


def download_media(media_id):
    response = requests.get(
        f"{GRAPH}/{media_id}",
        headers=auth_headers(),
        timeout=30,
    )
    response.raise_for_status()
    metadata = response.json()

    size = int(metadata.get("file_size") or 0)
    if size > MAX_MEDIA_BYTES:
        raise ValueError("கோப்பு 10 MB வரம்பை மீறுகிறது.")

    url = metadata.get("url")
    if not url:
        raise RuntimeError("Media URL கிடைக்கவில்லை.")

    with requests.get(
        url,
        headers=auth_headers(),
        stream=True,
        timeout=60,
    ) as r:
        r.raise_for_status()
        data = bytearray()

        for chunk in r.iter_content(65536):
            if not chunk:
                continue
            data.extend(chunk)

            if len(data) > MAX_MEDIA_BYTES:
                raise ValueError("கோப்பு 10 MB வரம்பை மீறுகிறது.")

    return bytes(data), metadata.get(
        "mime_type", "application/octet-stream"
    ), metadata.get("file_name", "file")


# ---------- TAMIL VOICE ----------

def send_voice(to, text):
    spoken = clean_voice_text(text)
    if not spoken:
        return

    sentences = re.split(r"(?<=[.!?।])\s+", spoken)
    chunks = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(current) + len(sentence) < 600:
            current = (current + " " + sentence).strip()
        else:
            if current:
                chunks.append(current)
            current = sentence

    if current:
        chunks.append(current)

    # Limit outgoing voice messages to avoid excessive API usage.
    for chunk in chunks[:5]:
        path = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".mp3", delete=False
            ) as temp:
                path = temp.name

            gTTS(text=chunk, lang="ta", slow=False).save(path)
            send_audio(to, path)

        except Exception:
            log.exception("Tamil voice generation failed")
        finally:
            if path and os.path.exists(path):
                os.remove(path)


# ---------- IMAGE READING ----------

def read_image(data, question=""):
    image = Image.open(BytesIO(data)).convert("RGB")

    prompt = """
    இந்தப் படத்தை பார்வைக் குறைபாடு உள்ளவருக்கு
    உதவும் வகையில் தமிழில் விளக்கவும்.

    முக்கியப் பொருட்கள், எழுத்துகள், நிறங்கள்,
    படத்தில் உள்ள செயல்கள் மற்றும் கவனிக்க வேண்டிய
    விஷயங்களைத் தெளிவாகச் சொல்லவும்.

    தெரியாத தகவல்களை ஊகிக்க வேண்டாம்.
    """

    if question:
        prompt += "\nபயனர் கேள்வி: " + question[:1000]

    return ask_gemini([prompt, image])


# ---------- PDF READING ----------

def read_pdf(data, question=""):
    with fitz.open(stream=data, filetype="pdf") as doc:
        if len(doc) == 0:
            raise ValueError("PDF காலியாக உள்ளது.")

        if len(doc) > MAX_PDF_PAGES:
            raise ValueError(
                "PDF-இல் அதிகபட்சம் 10 பக்கங்கள் மட்டுமே "
                "வாசிக்க முடியும். தயவுசெய்து சிறிய PDF அனுப்பவும்."
            )

        extracted = []
        for i, page in enumerate(doc):
            text = page.get_text("text").strip()
            if text:
                extracted.append(f"பக்கம் {i+1}:\n{text}")

        full_text = "\n\n".join(extracted)

        if len(full_text.strip()) >= 80:
            prompt = (
                "கீழே உள்ள PDF உரையை எளிய தமிழில் விளக்கவும். "
                "முக்கிய தகவல்கள், எண்கள், தேதிகள் மற்றும் "
                "செய்ய வேண்டிய செயல்களைத் தெளிவாகக் கூறவும்.\n"
                f"பயனர் கேள்வி: {question or 'இந்த ஆவணத்தை விளக்கவும்'}\n\n"
                + full_text[:12000]
            )
            return ask_gemini(prompt)

        # Scanned PDF: render pages as images for Gemini.
        images = []
        for page in doc:
            pix = page.get_pixmap(dpi=120, alpha=False)
            images.append(
                Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
            )

        prompt = (
            "இவை PDF ஆவணத்தின் பக்கங்கள். வரிசைப்படி வாசித்து "
            "தமிழில் விளக்கவும். படிக்க முடியாத பகுதிகளை "
            "ஊகிக்காமல் குறிப்பிடவும்.\n"
            f"பயனர் கேள்வி: {question or 'இந்த ஆவணத்தை விளக்கவும்'}"
        )
        return ask_gemini([prompt, *images])


# ---------- DOCUMENT CREATION ----------

def create_pdf(text, output_path):
    font_path = os.getenv("TAMIL_FONT_PATH", "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf")
    if not font_path or not os.path.isfile(font_path):
        # Render la Tamil font illana, English font la create pannidalam pa
        font_path = "" 
        # ... fallback logic, illa na simple-a skip pannalam pa
    
        )

    font_name = "TamilFont"
    if font_name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(font_name, font_path))

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TamilTitle",
        parent=styles["Title"],
        fontName=font_name,
        fontSize=16,
        leading=24,
    )
    body_style = ParagraphStyle(
        "TamilBody",
        parent=styles["BodyText"],
        fontName=font_name,
        fontSize=11,
        leading=17,
        spaceAfter=8,
    )

    def escape_xml(value):
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    pdf = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        rightMargin=20*mm,
        leftMargin=20*mm,
        topMargin=20*mm,
        bottomMargin=20*mm,
    )

    story = [
        Paragraph("கண் பார்வை AI", title_style),
        Spacer(1, 8),
    ]

    for paragraph in re.split(r"\n\s*\n", text.strip()):
        if paragraph.strip():
            story.append(
                Paragraph(
                    escape_xml(paragraph).replace("\n", "<br/>"),
                    body_style,
                )
            )
            story.append(Spacer(1, 4))

    pdf.build(story)


def is_document_request(text):
    terms = [
        "கடிதம்", "விண்ணப்பம்", "அறிக்கை",
        "pdf", "பிடிஎஃப்", "ஆவணம் உருவாக்கு",
        "தயாரித்து அனுப்பு", "create a pdf",
        "write a letter", "create a document",
        "generate pdf", "draft a letter",
    ]
    value = text.lower()
    return any(term in value for term in terms)


def create_and_send_pdf(to, text):
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, f"{uuid.uuid4().hex}.pdf")
        create_pdf(text, path)
        send_pdf(to, path)


# ---------- MESSAGE PROCESSOR ----------

def process_message(sender, message, message_id):
    if message_id and message_id in processed_ids:
        return

    if message_id:
        processed_ids.add(message_id)
        if len(processed_ids) > 2000:
            processed_ids.clear()

    kind = message.get("type", "")
    user_text = ""

    if kind == "text":
        user_text = message.get("text", {}).get("body", "").strip()
    elif kind == "image":
        user_text = message.get("image", {}).get("caption", "").strip()
    elif kind == "document":
        user_text = message.get("document", {}).get("caption", "").strip()

    try:
        if kind == "text":
            if not user_text:
                send_text(sender, "உங்கள் கேள்வியை அனுப்புங்கள்.")
                return

            reply = ask_gemini(
                "பயனரின் செய்திக்கு தமிழில் பதிலளிக்கவும். "
                "ஆவணம் உருவாக்கச் சொன்னால் முழுமையான வரைவைத் தரவும்.\n"
                + user_text
            )
            send_text(sender, reply)
            send_voice(sender, reply)

            if is_document_request(user_text):
                try:
                    create_and_send_pdf(sender, reply)
                except Exception:
                    log.exception("PDF creation failed")
                    send_text(
                        sender,
                        "தமிழ் உரையை அனுப்பிவிட்டேன். "
                        "PDF உருவாக்க முடியவில்லை; "
                        "தமிழ் எழுத்துரு அமைப்பைச் சரிபார்க்கவும்."
                    )
            return

        if kind == "image":
            media_id = message.get("image", {}).get("id")
            data, mime, filename = download_media(media_id)
            reply = read_image(data, user_text or "இந்தப் படத்தில் என்ன உள்ளது?")
            send_text(sender, reply)
            send_voice(sender, reply)
            return

        if kind == "document":
            item = message.get("document", {})
            media_id = item.get("id")
            filename = item.get("filename", "file").lower()

            data, mime, _ = download_media(media_id)

            if filename.endswith(".pdf") or mime == "application/pdf":
                reply = read_pdf(data, user_text)

            elif filename.endswith((".txt", ".csv")) or mime.startswith("text/"):
                raw = data.decode("utf-8", errors="replace")[:12000]
                reply = ask_gemini(
                    "இந்த ஆவணத்தை தமிழில் விளக்கவும்:\n" + raw
                )

            elif filename.endswith(".docx"):
                with tempfile.NamedTemporaryFile(
                    suffix=".docx"
                ) as temp:
                    temp.write(data)
                    temp.flush()
                    doc = Document(temp.name)
                    raw = "\n".join(p.text for p in doc.paragraphs)
                reply = ask_gemini(
                    "இந்த ஆவணத்தை தமிழில் விளக்கவும்:\n" + raw[:12000]
                )

            else:
                send_text(
                    sender,
                    "PDF, DOCX, TXT அல்லது CSV கோப்பை அனுப்பவும்."
                )
                return

            send_text(sender, reply)
            send_voice(sender, reply)
            return

        if kind == "audio":
            item = message.get("audio", {})
            data, mime, _ = download_media(item.get("id"))

            audio_part = types.Part.from_bytes(
                data=data,
                mime_type=mime or "audio/ogg",
            )

            reply = ask_gemini([
                "இந்தக் குரல் செய்தியைப் புரிந்து, "
                "அதில் உள்ள கேள்விக்கு தமிழில் பதிலளிக்கவும். "
                "தேவைப்பட்டால் பேசப்பட்டதை முதலில் எழுத்தாக்கவும்.",
                audio_part,
            ])

            send_text(sender, reply)
            send_voice(sender, reply)
            return

        send_text(
            sender,
            "தமிழ் உரை, படம், PDF, DOCX, TXT அல்லது குரல் செய்தியை அனுப்பலாம்."
        )

    except ValueError as exc:
        send_text(sender, str(exc))
    except Exception:
        log.exception("Message processing failed")
        try:
            send_text(
                sender,
                "மன்னிக்கவும். தற்போது செயல்படுத்த முடியவில்லை. "
                "சிறிது நேரம் கழித்து மீண்டும் முயற்சிக்கவும்."
            )
        except Exception:
            log.exception("Could not send error response")


# ---------- WEBHOOK ----------

@app.get("/")
def home():
    return {"status": "Kan Paarvai AI is running"}


@app.head("/")
def home_head():
    return PlainTextResponse("ok")


@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    if (
        mode == "subscribe"
        and VERIFY_TOKEN
        and hmac.compare_digest(token, VERIFY_TOKEN)
    ):
        return PlainTextResponse(challenge)

    return PlainTextResponse("Verification failed", status_code=403)


def valid_signature(body, signature):
    if not META_APP_SECRET:
        # Configure META_APP_SECRET in production.
        log.error("META_APP_SECRET is not configured")
        return False

    if not signature:
        return False

    digest = hmac.new(
        META_APP_SECRET.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest("sha256=" + digest, signature)


@app.post("/webhook")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
):
    body = await request.body()

    if not valid_signature(
        body,
        request.headers.get("x-hub-signature-256", ""),
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            for message in value.get("messages", []):
                sender = message.get("from")
                message_id = message.get("id", "")

                if sender:
                    background_tasks.add_task(
                        process_message,
                        sender,
                        message,
                        message_id,
                    )

    return {"ok": True}
