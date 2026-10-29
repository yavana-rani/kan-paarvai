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

# ---------- CONFIG ----------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v22.0")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash"
).split(",") if m.strip()]
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_PDF_IMAGE_PAGES = 5
MAX_VOICE_CHARS = 1200      # longer replies are cut at a sentence end
MAX_VOICE_SEGMENTS = 10     # limit of Tamil/English pieces per voice note

FONT_DIR = str(Path(__file__).resolve().parent / "fonts")
Path(FONT_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")
app = FastAPI(title="Kan Paarvai AI")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

SYSTEM_PROMPT = (
    "நீங்கள் Kan Paarvai AI. எப்போதும் எளிய தமிழில் (தமிழ் எழுத்துகளில்) "
    "சுருக்கமாகவும் தெளிவாகவும் பதிலளிக்கவும்."
)


class UnsupportedFile(Exception):
    pass


# ---------- FONTS (auto-download if missing) ----------
FONT_FILES = {
    "NotoSansTamil-Regular.ttf":
        "https://github.com/notofonts/tamil/raw/main/fonts/NotoSansTamil/full/ttf/NotoSansTamil-Regular.ttf",
    "NotoSans-Regular.ttf":
        "https://github.com/notofonts/latin-greek-cyrillic/raw/main/fonts/NotoSans/full/ttf/NotoSans-Regular.ttf",
}


def ensure_fonts():
    for name, url in FONT_FILES.items():
        p = Path(FONT_DIR) / name
        if p.exists() and p.stat().st_size > 10_000:
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            if r.content[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
                raise ValueError("not a font file")
            p.write_bytes(r.content)
            log.info("Downloaded font %s", name)
        except Exception as e:
            log.error("Could not download %s: %s - put the file in %s manually",
                      name, e, FONT_DIR)


ensure_fonts()


def fonts_ready():
    return all((Path(FONT_DIR) / n).exists() for n in FONT_FILES)


# ---------- DEDUPE ----------
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


# ---------- GEMINI WITH FALLBACK ----------
def ask_gemini(contents, system=SYSTEM_PROMPT, clean=True, json_mode=False):
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
            cfg = dict(system_instruction=system, temperature=0.2,
                       max_output_tokens=4096)
            if json_mode:
                cfg["response_mime_type"] = "application/json"
            resp = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(**cfg),
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


# ---------- VOICE (Tamil + English, proper pronunciation) ----------
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+")
# runs of English words (letters/digits inside words) -> spoken with the English voice
EN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*(?:[ \t]+[A-Za-z][A-Za-z0-9'’\-]*)*")

SYMBOL_WORDS = {
    "=": " சமம் ",
    "+": " கூட்டல் ",
    "×": " பெருக்கல் ",
    "÷": " வகுத்தல் ",
    "%": " சதவீதம் ",
    "√": " வர்க்கமூலம் ",
    "π": " பை ",
    "²": " வர்க்கம் ",
    "³": " கனம் ",
    "&": " மற்றும் ",
}


def speech_text(text):
    """Turn a chat reply into text a TTS engine can read cleanly."""
    t = clean_text(text)
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)           # links are not read
    t = EMOJI_RE.sub(" ", t)                                 # emojis are not read
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)                # 1,000 -> 1000
    t = re.sub(r"₹\s*(\d+(?:\.\d+)?)", r"\1 ரூபாய்", t)       # ₹500 -> 500 ரூபாய்
    for sym, word in SYMBOL_WORDS.items():
        t = t.replace(sym, word)
    t = re.sub(r"^\s*[-•▪●]+\s*", "", t, flags=re.M)         # bullets
    t = re.sub(r"[_~|<>\[\]{}\\^/]", " ", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)                        # line breaks -> pause
    t = re.sub(r"([.!?])(\s*\.)+", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _trim_sentence(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(c) for c in ".!?")
    return cut[:idx + 1] if idx > limit * 0.5 else cut


def _tts_bytes(text, lang):
    buf = BytesIO()
    gTTS(text=text, lang=lang, slow=False).write_to_fp(buf)
    return buf.getvalue()


def _split_segments(spoken):
    segs, pos = [], 0
    for m in EN_RUN.finditer(spoken):
        if m.start() > pos:
            segs.append(["ta", spoken[pos:m.start()]])
        segs.append(["en", m.group()])
        pos = m.end()
    if pos < len(spoken):
        segs.append(["ta", spoken[pos:]])
    segs = [[l, s.strip()] for l, s in segs if re.search(r"\w", s)]
    merged = []
    for l, s in segs:
        if merged and merged[-1][0] == l:
            merged[-1][1] += " " + s
        else:
            merged.append([l, s])
    return merged


def synthesize_speech(text):
    """Return MP3 bytes. Tamil parts use the Tamil voice, English parts the English voice."""
    spoken = _trim_sentence(speech_text(text), MAX_VOICE_CHARS)
    if not spoken:
        return b""
    segs = _split_segments(spoken)
    if not segs:
        return b""
    if len(segs) > MAX_VOICE_SEGMENTS:
        segs = [["ta", spoken]]
    parts = []
    for lang, seg in segs:
        try:
            parts.append(_tts_bytes(seg, lang))
        except Exception:
            log.exception("tts failed for %s segment", lang)
    return b"".join(parts)


def send_voice(to, text):
    path = None
    try:
        audio = synthesize_speech(text)
        if not audio:
            return
        fd, path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        send_audio(to, path)
        log.info("Voice sent")
    except Exception:
        log.exception("voice failed")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ---------- LETTER / PDF CREATION ----------
BLANK = "____________"
LETTER_RE = re.compile(
    r"\b(letter|application|resume|cv|bonafide|complaint|pdf|leave|kadit\w*)\b"
    r"|கடிதம்|விடுப்பு|விண்ணப்பம்",
    re.I,
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
- language: "en" if English, "ta" if Tamil/Tanglish.
- Write every text field in that language. For "ta" use Tamil script only.
- NEVER invent names. Use "" for missing.
- body: paragraphs only, separated by \\n\\n.
- No explanations outside the JSON.

User request:
"""

# Tamil text uses the Tamil font; every run of ASCII (English, digits, punctuation)
# is wrapped in <span class="en"> and uses the Latin font. This avoids missing glyphs.
LETTER_CSS = """
@font-face{font-family:NotoTamil;src:url('NotoSansTamil-Regular.ttf')}
@font-face{font-family:NotoLatin;src:url('NotoSans-Regular.ttf')}
body{font-family:NotoTamil;font-size:12pt;line-height:1.6}
span.en{font-family:NotoLatin}
p{margin:0 0 12pt 0}
p.b{text-align:justify;text-indent:30pt}
p.r{text-align:right}
"""
FALLBACK_CSS = (
    "body{font-size:12pt;line-height:1.6} p{margin:0 0 12pt 0} "
    "p.b{text-align:justify;text-indent:30pt} p.r{text-align:right}"
)

_ASCII_SPLIT = re.compile(r"([\x20-\x7E]+)")


def looks_like_letter_request(text):
    return bool(LETTER_RE.search(text or ""))


def parse_json(raw):
    raw = (raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in reply")
    return json.loads(raw[start:end + 1])


def _fmt(s):
    """Escape, keep line breaks, and tag ASCII runs with the Latin font."""
    out_lines = []
    for line in (s or "").strip().split("\n"):
        pieces = []
        for piece in _ASCII_SPLIT.split(line):
            if not piece:
                continue
            esc = htmllib.escape(piece)
            pieces.append(f"<span class='en'>{esc}</span>" if _ASCII_SPLIT.fullmatch(piece) else esc)
        out_lines.append("".join(pieces))
    return "<br>".join(out_lines)


def build_letter_html(d):
    today = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d/%m/%Y")
    is_ta = d.get("language") == "ta"
    lbl_from, lbl_to, lbl_date, lbl_sub = (
        ("அனுப்புநர்", "பெறுநர்", "நாள்", "பொருள்") if is_ta
        else ("From", "To", "Date", "Subject")
    )
    sender = _fmt(d.get("sender_name")) or BLANK
    sender_addr = _fmt(d.get("sender_address")) or BLANK
    rec_title = _fmt(d.get("recipient_title")) or BLANK
    rec_addr = _fmt(d.get("recipient_address"))
    date = _fmt(d.get("date")) or today
    subject = _fmt(d.get("subject"))
    salute = _fmt(d.get("salutation"))
    closing = _fmt(d.get("closing"))
    sign = _fmt(d.get("signature_name")) or sender
    paras = "".join(
        f"<p class='b'>{_fmt(p)}</p>"
        for p in (d.get("body") or "").split("\n\n") if p.strip()
    )
    to_block = rec_title + (f"<br>{rec_addr}" if rec_addr else "")
    return f"""<html><body>
<p><b>{lbl_from}:</b><br>{sender}<br>{sender_addr}</p>
<p><b>{lbl_to}:</b><br>{to_block}</p>
<p class='r'><b>{lbl_date}:</b> {date}</p>
<p><b>{lbl_sub}:</b> {subject}</p>
<p>{salute}</p>
{paras}
<p class='r'>{closing}<br><br><br>{sign}</p>
</body></html>"""


def build_letter_pdf(data, path):
    is_ta = data.get("language") == "ta"
    if fonts_ready():
        ensure_fonts()
        story = fitz.Story(html=build_letter_html(data), user_css=LETTER_CSS,
                           archive=fitz.Archive(FONT_DIR))
    elif is_ta:
        # Built-in PDF fonts have no Tamil glyphs -> would print empty boxes
        raise RuntimeError(f"Tamil fonts missing in {FONT_DIR}")
    else:
        story = fitz.Story(html=build_letter_html(data), user_css=FALLBACK_CSS)

    writer = fitz.DocumentWriter(path)
    try:
        page = fitz.paper_rect("a4")
        where = page + (60, 60, -60, -60)
        more = 1
        while more:
            dev = writer.begin_page(page)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
    finally:
        writer.close()
    log.info("PDF created at %s", path)


def try_letter(sender, user_text):
    if not looks_like_letter_request(user_text):
        return False
    try:
        raw = ask_gemini(LETTER_PROMPT + user_text,
                         system="Reply with JSON only.", clean=False, json_mode=True)
        data = parse_json(raw)
    except Exception:
        log.exception("letter intent check failed - using normal reply")
        return False
    if not data.get("wants_document"):
        return False

    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        build_letter_pdf(data, path)
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", data.get("doc_type") or "letter")
        send_document(sender, path, f"{name.strip('_') or 'letter'}.pdf")
        log.info("Letter PDF sent to %s", sender)
    except Exception:
        log.exception("Failed to build/send PDF")
        safe_send_text(sender, "மன்னிக்கவும், PDF உருவாக்க முடியவில்லை. சிறிது நேரம் கழித்து முயற்சிக்கவும்.")
        return True  # error already shown; do not also send a normal reply
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
            if len(text.strip()) < 80:
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
