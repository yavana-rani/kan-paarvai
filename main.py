import os
import re
import hmac
import hashlib
import logging
import tempfile
import threading
from collections import OrderedDict
from io import BytesIO
from pathlib import Path

import requests
import fitz  # PyMuPDF
from PIL import Image
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from gtts import gTTS
from google import genai
from google.genai import types
from docx import Document

# ---------- CONFIG ----------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v22.0")
# Old 1.5 retired, 3.8 quota 20/day. Use stable high-quota model pa
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-1.5-flash"
).split(",") if m.strip()]
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash"
).split(",") if m.strip()]
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_PDF_IMAGE_PAGES = 5

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
def ask_gemini(contents):
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
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                    max_output_tokens=4096,
                ),
            )
            text = getattr(resp, "text", None)
            if text:
                return clean_text(text)
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
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        gTTS(text=spoken[:600], lang="ta", slow=False).save(path)
        send_audio(to, path)
    except Exception:
        log.exception("voice failed")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


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
