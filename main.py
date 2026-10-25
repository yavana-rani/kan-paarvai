import os
import re
import json
import hmac
import hashlib
import logging
import tempfile
import threading
import html as htmllib
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import fitz  # PyMuPDF
from PIL import Image
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from gtts import gTTS
from google import genai
from google.genai import types
from docx import Document
fonts/NotoSansTamil-Regular.ttf and fonts/NotoSans-Regular.ttf
# ---------- CONFIG ----------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v22.0")
# Old 1.5 retired, 3.8 quota 20/day. Use stable high-quota model pa
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
# (this was defined twice before; the second one was the one actually used)
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash"
).split(",") if m.strip()]
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_PDF_IMAGE_PAGES = 5

# Fonts for PDF creation: put NotoSansTamil-Regular.ttf and
# NotoSans-Regular.ttf inside a "fonts" folder next to this file.
FONT_DIR = str(Path(__file__).resolve().parent / "fonts")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")
app = FastAPI(title="Kan Paarvai AI")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Tamil SCRIPT is needed so gTTS (lang="ta") can read the reply properly.
SYSTEM_PROMPT = (
    "நீங்கள் Kan Paarvai AI. எப்போதும் எளிய தமிழில் (தமிழ் எழுத்துகளில்) "
    "சுருக்கமாகவும் தெளிவாகவும் பதிலளிக்கவும்."
)


class UnsupportedFile(Exception):
    pass


# ---------- DEDUPE (bounded + thread-safe) ----------
_seen = OrderedDict()
_seen_lock = threading.Lock()


def already_processed(message_id):
    if not message_id:
        return False
    with _seen_lock:
        if message_id in _seen:
            return True
        _seen[message_id] = True
        while len(_seen) > 5000:
            _seen.popitem(last=False)
    return False


# ---------- TEXT CLEANING ----------
def clean_text(text):
    if not text:
        return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*`#]+", "", text)
    return text.strip()[:12000]


def clean_voice_text(text):
    return re.sub(r"\s+", " ", clean_text(text)).strip()


# ---------- GEMINI WITH FALLBACK ----------
# system / clean are optional, so every old call ask_gemini(x) works as before.
def ask_gemini(contents, system=SYSTEM_PROMPT, clean=True):
    if not client:
        raise RuntimeError("GEMINI_API_KEY not set")

    unique = []
    for m in [GEMINI_MODEL, *FALLBACK_MODELS]:
        if m and m not in unique:
            unique.append(m)

    last_err = None
    for model_name in unique:
        try:
            log.info("Trying %s", model_name)
            resp = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.2,
                    max_output_tokens=4096,
                ),
            )
            text = getattr(resp, "text", None)
            if text:
                return clean_text(text) if clean else text
            log.warning("%s returned empty text", model_name)
        except Exception as e:
            last_err = e
            log.warning("%s failed: %s", model_name, e)
    raise RuntimeError(f"All Gemini models failed: {last_err}")


# ---------- WHATSAPP API ----------
def auth_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}


def graph_post(endpoint, payload):
    r = requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/{endpoint}",
        headers={**auth_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=40,
    )
    if not r.ok:
        log.error("Graph error %s: %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json()


def send_text(to, text):
    graph_post("messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": clean_text(text)[:3900] or "..."},
    })


def safe_send_text(to, text):
    try:
        send_text(to, text)
    except Exception:
        log.exception("could not send text")


def upload_media(path, mime):
    with open(path, "rb") as f:
        r = requests.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/media",
            headers=auth_headers(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (Path(path).name, f, mime)},
            timeout=60,
        )
    r.raise_for_status()
    return r.json().get("id")


def send_audio(to, path):
    mid = upload_media(path, "audio/mpeg")
    graph_post("messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "audio",
        "audio": {"id": mid},
    })


def send_document(to, path, filename="letter.pdf"):
    mid = upload_media(path, "application/pdf")
    graph_post("messages", {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "document",
        "document": {"id": mid, "filename": filename},
    })


def download_media(media_id):
    r = requests.get(f"{GRAPH}/{media_id}", headers=auth_headers(), timeout=30)
    r.raise_for_status()
    meta = r.json()
    url = meta.get("url")
    mime = meta.get("mime_type", "")
    if int(meta.get("file_size") or 0) > MAX_MEDIA_BYTES:
        raise UnsupportedFile("too_big")

    buf = bytearray()
    with requests.get(url, headers=auth_headers(), stream=True, timeout=60) as d:
        d.raise_for_status()
        for chunk in d.iter_content(64 * 1024):
            buf.extend(chunk)
            if len(buf) > MAX_MEDIA_BYTES:
                raise UnsupportedFile("too_big")
    return bytes(buf), mime


def send_voice(to, text):
    spoken = clean_voice_text(text)
    if not spoken:
        return

    # Full text-a chunk-a split pannrom pa - 1 part = 350 letters (~25 sec) pa
    # Ippo full PDF-um voice la varum pa, cut aagathu pa!
    chunks = [spoken[i:i+350] for i in range(0, len(spoken), 350)]
    # Max 5 part (avoid spam) pa - romba perusa iruntha first 5 part mattum pa
    chunks = chunks[:5]

    for idx, chunk in enumerate(chunks):
        path = None
        try:
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            gTTS(text=chunk, lang="ta", slow=False).save(path)
            send_audio(to, path)
            log.info(f"Voice part {idx+1}/{len(chunks)} sent")
        except Exception:
            log.exception(f"voice part {idx+1} failed")
        finally:
            if path and os.path.exists(path):
                os.remove(path)


# ---------- LETTER / PDF CREATION ----------
BLANK = "____________"

# cheap pre-filter so normal chat doesn't cost an extra Gemini call
LETTER_KEYWORDS = (
    "letter", "application", "resume", "cv", "bonafide", "complaint",
    "kadithum", "kaditham", "கடிதம்", "விடுப்பு", "விண்ணப்பம்", "pdf",
)

LETTER_PROMPT = """You help write formal documents.
Read the user's request and reply with ONLY one JSON object, no markdown fences:
{
 "wants_document": true or false,
 "language": "ta" or "en",
 "doc_type": "leave letter",
 "sender_name": "", "sender_address": "",
 "recipient_title": "", "recipient_address": "",
 "date": "", "subject": "", "salutation": "",
 "body": "", "closing": "", "signature_name": ""
}
Rules:
- wants_document is true only if the user is asking you to write a letter,
  application, complaint, resume or similar document. Normal questions = false.
- language: "en" if the user asks for English or writes the request in English.
  "ta" if they ask for Tamil or write in Tamil / Tanglish and do not ask for English.
- Write every text field in that language. For "ta" use Tamil script only.
- NEVER invent names, addresses or dates. Use "" for anything the user did not give.
- body: the letter paragraphs only, separated by \\n\\n. No subject, no greeting.
- salutation like "Respected Sir/Madam," or "மதிப்பிற்குரிய ஐயா/அம்மையீர்,".
- closing like "Thanking you," / "நன்றி,". No explanations outside the JSON.

User request:
"""

LETTER_CSS = """
@font-face{font-family:NotoTamil;src:url(NotoSansTamil-Regular.ttf)}
@font-face{font-family:NotoLatin;src:url(NotoSans-Regular.ttf)}
body{font-family:NotoLatin,NotoTamil;font-size:12pt;line-height:1.6}
p{margin:0 0 12pt 0}
p.b{text-align:justify;text-indent:30pt}
p.r{text-align:right}
"""


def looks_like_letter_request(text):
    t = (text or "").lower()
    return any(k in t for k in LETTER_KEYWORDS)


def parse_json(raw):
    raw = (raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in reply")
    return json.loads(raw[start:end + 1])


def _e(s):
    return htmllib.escape((s or "").strip())


def _lines(s):
    return _e(s).replace("\n", "<br>")


def build_letter_html(d):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d/%m/%Y")
    is_ta = d.get("language") == "ta"
    lbl_from, lbl_to, lbl_date, lbl_sub = (
        ("அனுப்புநர்", "பெறுநர்", "நாள்", "பொருள்") if is_ta
        else ("From", "To", "Date", "Subject")
    )
    sender = _e(d.get("sender_name")) or BLANK
    sender_addr = _lines(d.get("sender_address")) or BLANK
    rec_title = _e(d.get("recipient_title")) or BLANK
    rec_addr = _lines(d.get("recipient_address"))
    date = _e(d.get("date")) or today
    subject = _e(d.get("subject"))
    salute = _e(d.get("salutation"))
    closing = _e(d.get("closing"))
    sign = _e(d.get("signature_name")) or sender

    paras = "".join(
        f"<p class='b'>{_lines(p)}</p>"
        for p in (d.get("body") or "").split("\n\n") if p.strip()
    )
    to_block = rec_title + (f"<br>{rec_addr}" if rec_addr else "")

    return f"""<body>
<p><b>{lbl_from}:</b><br>{sender}<br>{sender_addr}</p>
<p><b>{lbl_to}:</b><br>{to_block}</p>
<p class='r'><b>{lbl_date}:</b> {date}</p>
<p><b>{lbl_sub}:</b> {subject}</p>
<p>{salute}</p>
{paras}
<p class='r'>{closing}<br><br><br>{sign}</p>
</body>"""


def build_letter_pdf(data, path):
    story = fitz.Story(html=build_letter_html(data), user_css=LETTER_CSS,
                       archive=fitz.Archive(FONT_DIR))
    writer = fitz.DocumentWriter(path)
    page = fitz.paper_rect("a4")
    where = page + (60, 60, -60, -60)
    more = 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def try_letter(sender, user_text):
    """Returns True if a letter PDF was created and sent, else False
    (False means: carry on with the normal chat reply)."""
    if not looks_like_letter_request(user_text):
        return False

    # Step 1: ask Gemini if this is really a document request. Any failure
    # here just falls back to the normal reply, so old behaviour is safe.
    try:
        raw = ask_gemini(LETTER_PROMPT + user_text,
                         system="Reply with JSON only.", clean=False)
        data = parse_json(raw)
    except Exception:
        log.exception("letter intent check failed - using normal reply")
        return False
    if not data.get("wants_document"):
        return False

    # Step 2: build and send the PDF.
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        build_letter_pdf(data, path)
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", data.get("doc_type") or "letter")
        send_document(sender, path, f"{name.strip('_') or 'letter'}.pdf")
    finally:
        if os.path.exists(path):
            os.remove(path)

    if data.get("language") == "ta":
        send_text(sender, "கடிதம் தயார். ____ இடங்களை நிரப்பவும்.")
    else:
        send_text(sender, "Your letter is ready. Fill in the ____ blanks.")
    return True


# ---------- MEDIA HANDLING ----------
def handle_media(data, mime, fname, caption):
    mime = (mime or "").lower()
    name = (fname or "").lower()

    if mime.startswith("audio/"):
        clean_mime = mime.split(";")[0].strip()
        part = types.Part.from_bytes(data=data, mime_type=clean_mime)
        return ask_gemini([caption or "இந்தக் குரல் செய்தியைக் கேட்டு தமிழில் பதிலளிக்கவும்", part])

    if mime.startswith("image/"):
        img = Image.open(BytesIO(data)).convert("RGB")
        prompt = caption or "இந்தப் படத்தை தமிழில் விளக்கவும்"
        return ask_gemini([prompt, img])

    if mime == "application/pdf" or name.endswith(".pdf"):
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "\n".join(p.get_text() for p in doc)
            if len(text.strip()) < 80:  # scanned PDF -> send page images
                imgs = []
                for i, p in enumerate(doc):
                    if i >= MAX_PDF_IMAGE_PAGES:
                        break
                    png = p.get_pixmap(dpi=120).tobytes("png")
                    imgs.append(Image.open(BytesIO(png)).convert("RGB"))
                return ask_gemini([caption or "இந்த PDF பக்கங்களை தமிழில் விளக்கவும்", *imgs])
            return ask_gemini(f"{caption or 'இந்த PDF உரையை தமிழில் விளக்கவும்'}:\n{text[:12000]}")

    if "wordprocessingml" in mime or name.endswith(".docx"):
        doc = Document(BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs).strip()
        if not text:
            raise UnsupportedFile("empty")
        return ask_gemini(f"{caption or 'இந்த ஆவணத்தை தமிழில் விளக்கவும்'}:\n{text[:12000]}")

    if mime.startswith("text/") or name.endswith(".txt"):
        text = data.decode("utf-8", errors="ignore").strip()
        if not text:
            raise UnsupportedFile("empty")
        return ask_gemini(f"{caption or 'இந்த உரையை தமிழில் விளக்கவும்'}:\n{text[:12000]}")

    raise UnsupportedFile("type")


# ---------- PROCESS ----------
def process_message(sender, message, message_id):
    if already_processed(message_id):
        return
    kind = message.get("type", "")

    try:
        if kind == "text":
            user_text = message.get("text", {}).get("body", "").strip()
            if not user_text:
                return
            # NEW: letter / PDF creation (falls through to normal chat if not a letter)
            if try_letter(sender, user_text):
                return
            reply = ask_gemini(user_text)

        elif kind in ("image", "document", "audio", "voice"):
            info = message.get(kind, {})
            data, mime = download_media(info.get("id"))
            reply = handle_media(
                data, mime, info.get("filename", ""), (info.get("caption") or "").strip()
            )
        else:
            safe_send_text(sender, "Text, voice, image, PDF, Word file mattum anuppunga pa")
            return

        send_text(sender, reply)
        send_voice(sender, reply)

    except UnsupportedFile as e:
        reason = str(e)
        msg = {
            "too_big": "File romba perusu pa (max 10MB)",
            "empty": "File la text illa pa",
        }.get(reason, "Intha file type support illa pa. Image, PDF, Word anuppunga")
        safe_send_text(sender, msg)
    except Exception:
        log.exception("failed")
        safe_send_text(sender, "Konjam neram kalichu muyarchikavum pa")


# ---------- ROUTES ----------
def valid_signature(body: bytes, header: str) -> bool:
    if not META_APP_SECRET:
        log.warning("META_APP_SECRET not set - signature check skipped")
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(META_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


@app.api_route("/", methods=["GET", "HEAD"])
def home():
    return {"status": "Kan Paarvai AI running pa"}


@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if VERIFY_TOKEN and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("fail", status_code=403)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    if not valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        return PlainTextResponse("invalid signature", status_code=403)
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}

    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("from"):
                    background_tasks.add_task(process_message, msg["from"], msg, msg.get("id", ""))
    return {"ok": True}
