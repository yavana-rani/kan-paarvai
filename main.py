import os
import re
import json
import hmac
import hashlib
import logging
import tempfile
import threading
import time
import unicodedata
import html as htmllib
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import fitz  # PyMuPDF
from PIL import Image, ImageOps
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse
from gtts import gTTS
from gtts.lang import tts_langs
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
FALLBACK_MODELS = [m.strip() for m in os.getenv("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash").split(",") if m.strip()]
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ta")
LOCAL_TZ = os.getenv("LOCAL_TZ", "Asia/Kolkata")
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_PDF_IMAGE_PAGES = 5
MAX_PDF_TEXT_CHARS = 15000
MAX_VOICE_CHARS = 1200
MAX_VOICE_SEGMENTS = 25
FONT_DIR = str(Path(__file__).resolve().parent / "fonts")
Path(FONT_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")
app = FastAPI(title="Kan Paarvai AI")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

STYLE_RULES = "Keep answers short, simple and clear. Write math formulas as plain text, e.g. πr^2, a^2 + b^2 = c^2, x × y, a ÷ b; do not use * or markdown."
LANG_HINT = ' "language" is the ISO 639-1 code of your reply (e.g. ta, en, hi).'
SYSTEM_PROMPT = f"You are Kan Paarvai AI, a helpful WhatsApp assistant. Always reply in the SAME language the user used. If there is no user text, reply in language code '{DEFAULT_LANG}'. " + STYLE_RULES

TEXT_PROMPT = """You ARE ABLE to generate images. NEVER say "I cannot generate images". If user asks for an image, you MUST return kind="image".
Read the user's message and reply with ONLY one JSON object, no markdown fences:
{
 "language": "", "kind": "chat" or "document" or "image", "reply": "", "image_prompt": "",
 "doc_type": "", "sender_name": "", "sender_address": "", "recipient_title": "", "recipient_address": "",
 "date": "", "subject": "", "salutation": "", "body": "", "closing": "", "signature_name": "",
 "labels": {"from": "", "to": "", "date": "", "subject": ""}, "ready_message": ""
}
Rules:
- "language" is the ISO 639-1 code of the user's language (e.g. ta, en, hi).
- kind is "image" if user asks to create, generate, draw an image, picture, photo, logo, poster, greeting card.
- For "image": put detailed ENGLISH prompt in "image_prompt" and acknowledgement in "reply" in user's language.
- For "document": fill document fields.
- For "chat": put answer in "reply".
""" + STYLE_RULES + "\n\nUser message:\n"

MEDIA_PROMPT = """Reply with ONLY one JSON object: {"language": "", "reply": ""} Answer in caption language, otherwise '""" + DEFAULT_LANG + """'. """ + STYLE_RULES + LANG_HINT + " "

VOICE_PROMPT = 'Reply with ONLY one JSON object: {"language": "", "reply": ""} Listen to the audio and answer it in the same language the speaker used. ' + STYLE_RULES + LANG_HINT

IMAGE_PROMPT = MEDIA_PROMPT + (
    "Task: look at the image and explain in 3-5 short lines what is in it: the main subject, "
    "important details, and any visible text (read it briefly). If the caption asks a question, answer it instead."
)

DOC_PROMPT = MEDIA_PROMPT + (
    "Task: explain in 4-6 short lines what this document contains: what kind of document it is, "
    "the main topics, and the key points or numbers. If the caption asks a question, answer it too."
)

MSG = {
    "retry": "Please try again in a little while 🙏 (Konjam neram kalichu muyarchikavum pa)",
    "too_big": "File is too big, max 10 MB (File romba perusu pa)",
    "empty": "There is no text in this file (File la text illa pa)",
    "type": "Please send text, voice, image, PDF or Word files (Text, voice, image, PDF, Word file mattum anuppunga pa)",
    "pdf_fail": "Sorry, the PDF could not be created. Please try again later.",
    "img_fail": "Sorry, image could not be created. Please try again later (Image create panna mudiyala pa)",
}

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class UnsupportedFile(Exception): pass
class TooBig(Exception): pass
class EmptyFile(Exception): pass


# ---------- FONTS ----------
FONT_FILES = {
    "NotoSansTamil-Regular.ttf": "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSansTamil/hinted/ttf/NotoSansTamil-Regular.ttf",
    "NotoSans-Regular.ttf": "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSans/hinted/ttf/NotoSans-Regular.ttf",
}
FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true")

def _font_ok(p):
    try: return p.stat().st_size > 10_000 and p.read_bytes()[:4] in FONT_MAGIC
    except OSError: return False
def ensure_fonts():
    for name, url in FONT_FILES.items():
        p = Path(FONT_DIR) / name
        if _font_ok(p): continue
        try:
            r = requests.get(url, timeout=60); r.raise_for_status()
            if r.content[:4] not in FONT_MAGIC: raise ValueError("not a font file")
            p.write_bytes(r.content)
        except Exception as e: log.warning(f"Could not download {name}: {e}")
def fonts_ready(): return all(_font_ok(Path(FONT_DIR) / n) for n in FONT_FILES)

threading.Thread(target=ensure_fonts, daemon=True).start()

_seen = OrderedDict(); _seen_lock = threading.Lock()

def already_processed(message_id):
    if not message_id: return False
    with _seen_lock:
        if message_id in _seen: return True
        _seen[message_id] = True
        while len(_seen) > 5000: _seen.popitem(last=False)
    return False


# ---------- TEXT / LANGUAGE HELPERS ----------
def clean_text(text):
    if not text: return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*`#]+", "", text)
    return text.strip()[:12000]

_SCRIPT_LANG = [("TAMIL","ta"),("DEVANAGARI","hi"),("TELUGU","te"),("MALAYALAM","ml"),("KANNADA","kn"),("BENGALI","bn"),("ARABIC","ar")]
def _script_of(ch):
    try: name = unicodedata.name(ch)
    except ValueError: return None
    for script,_ in _SCRIPT_LANG:
        if name.startswith(script): return script
    return "LATIN" if name.startswith("LATIN") else None
def guess_lang(text):
    counts = {}
    for ch in text or "":
        if ch.isalpha():
            s = _script_of(ch)
            if s: counts[s] = counts.get(s,0)+1
    non_latin = {k:v for k,v in counts.items() if k!="LATIN"}
    if non_latin: return dict(_SCRIPT_LANG)[max(non_latin,key=non_latin.get)]
    return "en" if counts else DEFAULT_LANG
def is_latin_text(text): return not any(c.isalpha() and _script_of(c) not in (None,"LATIN") for c in text)
RTL_LANGS = {"ar","fa","ur","he","iw"}
def base_lang(code): return (code or "").strip().lower().replace("_","-").split("-")[0]


# ---------- GEMINI ----------
def ask_gemini(contents, system=SYSTEM_PROMPT, clean=True, json_mode=False):
    if not client: raise RuntimeError("GEMINI_API_KEY not set")
    unique = []
    for m in [GEMINI_MODEL, *FALLBACK_MODELS]:
        if m and m not in unique: unique.append(m)
    last_err = None
    for model_name in unique:
        try:
            cfg = dict(system_instruction=system, temperature=0.2, max_output_tokens=4096)
            if json_mode: cfg["response_mime_type"] = "application/json"
            resp = client.models.generate_content(model=model_name, contents=contents, config=types.GenerateContentConfig(**cfg))
            text = getattr(resp,"text",None)
            if text: return clean_text(text) if clean else text
        except Exception as e: last_err=e; log.warning(f"{model_name} failed: {e}")
    raise RuntimeError(f"All models failed: {last_err}")

def parse_json(raw):
    raw=(raw or "").strip(); start, end = raw.find("{"), raw.rfind("}")
    if start==-1 or end==-1: raise ValueError("no JSON")
    return json.loads(raw[start:end+1])

def ask_with_lang(contents, system):
    try:
        data = parse_json(ask_gemini(contents, system=system, clean=False, json_mode=True))
        lang = (data.get("language") or "").strip()
        if len(base_lang(lang)) not in (2, 3): lang = ""
        if data.get("kind") not in ("document",):
            data["reply"] = clean_text(data.get("reply")) if data.get("reply") else data.get("reply")
        lang = lang or guess_lang(data.get("reply") or data.get("image_prompt") or data.get("body") or "")
        return data, (lang or DEFAULT_LANG)
    except Exception:
        reply = ask_gemini(contents)
        return {"kind":"chat","reply":reply}, guess_lang(reply)


# ---------- WHATSAPP API ----------
def auth_headers(): return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

def graph_post(endpoint, payload):
    r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/{endpoint}", headers={**auth_headers(),"Content-Type":"application/json"}, json=payload, timeout=40)
    if not r.ok: log.error(f"Graph error {r.status_code}: {r.text}")
    r.raise_for_status(); return r.json()

def send_text(to, text):
    text = (text or "").strip() or MSG["retry"]
    for i in range(0, len(text), 4000):
        graph_post("messages", {"messaging_product":"whatsapp","to":to,"type":"text",
                                "text":{"body":text[i:i+4000],"preview_url":False}})

def safe_send_text(to, text):
    try: send_text(to, text)
    except Exception as e: log.error(f"send_text failed: {e}")

def upload_media(path, mime):
    with open(path, "rb") as f:
        r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/media", headers=auth_headers(),
                          data={"messaging_product":"whatsapp","type":mime},
                          files={"file":(os.path.basename(path), f, mime)}, timeout=90)
    if not r.ok: log.error(f"Upload error {r.status_code}: {r.text}")
    r.raise_for_status(); return r.json()["id"]

def download_media(media_id):
    meta = requests.get(f"{GRAPH}/{media_id}", headers=auth_headers(), timeout=30)
    meta.raise_for_status(); info = meta.json()
    if int(info.get("file_size") or 0) > MAX_MEDIA_BYTES: raise TooBig()
    r = requests.get(info["url"], headers=auth_headers(), timeout=60, stream=True)
    r.raise_for_status()
    buf = BytesIO()
    for chunk in r.iter_content(65536):
        buf.write(chunk)
        if buf.tell() > MAX_MEDIA_BYTES: raise TooBig()
    mime = (info.get("mime_type") or r.headers.get("Content-Type","")).split(";")[0].strip()
    return buf.getvalue(), mime

def send_image(to, image_bytes, caption=""):
    fd, path = tempfile.mkstemp(suffix=".jpg")
    try:
        os.close(fd)
        with open(path, "wb") as f: f.write(image_bytes)
        mid = upload_media(path, "image/jpeg")
        graph_post("messages", {
            "messaging_product":"whatsapp","to":to,"type":"image",
            "image":{"id":mid,"caption":caption[:900] if caption else ""}
        })
    finally:
        if os.path.exists(path): os.remove(path)

def send_document(to, path, filename, caption=""):
    mid = upload_media(path, "application/pdf")
    graph_post("messages", {
        "messaging_product":"whatsapp","to":to,"type":"document",
        "document":{"id":mid,"filename":filename,"caption":caption[:900] if caption else ""}
    })


# ---------- VOICE ----------
TTS_LANGS = tts_langs()
_EMOJI = re.compile("[\U00010000-\U0010ffff\u2600-\u27bf\ufe0f]")

NATIVE_CODES = {code for _, code in _SCRIPT_LANG}
RUPEE_WORDS = {"ta": "ரூபாய்", "hi": "रुपये", "te": "రూపాయలు", "ml": "രൂപ", "kn": "ರೂಪಾಯಿ", "bn": "টাকা"}
def speech_text(text, lang):
    """Make text safe for TTS: 1,000 -> 1000, Rs/₹/INR/ரூபாய் -> the word for rupees in the reply's script."""
    native = None if is_latin_text(text) else guess_lang(text)
    word = RUPEE_WORDS.get(native, "rupees")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    num = r"\d+(?:\.\d+)?"
    text = re.sub(rf"(?:₹|\bRs\.?|\bINR)\s*({num})", rf"\1 {word}", text, flags=re.I)
    text = re.sub(rf"({num})\s*(?:₹|/-|Rs\.?(?!\w)|INR(?!\w))", rf"\1 {word}", text, flags=re.I)
    text = re.sub(r"₹|\bRs\.?(?!\w)|\bINR\b", word, text, flags=re.I)
    text = re.sub(r"\bRs\.?\b", " ரூபாய் ", text, flags=re.I)
    
    # 10000 -> 10,000 maathiri padika vasathi, but tamil la number-a appadiye vitta pothum
    # ரூ.10000 -> ரூபாய் 10000 nu varum
    
    # Date 19.11.2026 -> 19 11 2026 nu space vachatha nalla pronounce pannum
    text = re.sub(r"(\d+)\.(\d+)\.(\d+)", r"\1 \2 \3", text)  # date fix
    
    # Existing code continues...
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    # ... rest of your code
return text

def _split_runs(text):
    """Split into runs of Latin / non-Latin letters so each run gets the right voice."""
    runs = []
    for ch in text:
        flag = (_script_of(ch) == "LATIN") if ch.isalpha() else None
        if not runs:
            runs.append([flag, ch]); continue
        cur = runs[-1]
        if flag is None or cur[0] is None or cur[0] == flag:
            if cur[0] is None: cur[0] = flag
            cur[1] += ch
        else:
            runs.append([flag, ch])
    return runs

def _voice_chunks(text, size=200):
    sentences = re.split(r"(?<=[.!?।])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > size:
            if cur: chunks.append(cur); cur = ""
            chunks.append(s[:size]); s = s[size:]
        if len(cur) + len(s) + 1 > size and cur: chunks.append(cur); cur = s
        else: cur = f"{cur} {s}".strip()
    if cur: chunks.append(cur)
    return chunks

def _voice_pieces(text, lang):
    """[(chunk, tts_lang)]: English words inside Tamil text are spoken by an English voice."""
    code = base_lang(lang)
    if code == "iw": code = "he"
    base = code if code in TTS_LANGS else "en"
    latin_lang = "en" if (code in NATIVE_CODES or code not in TTS_LANGS) else code
    pieces = []
    for is_latin, run in _split_runs(text):
        if not re.search(r"\w", run): continue
        if is_latin is None: vlang = base
        elif is_latin: vlang = latin_lang
        else:
            vlang = guess_lang(run)
            if vlang not in TTS_LANGS: vlang = "en"
        for chunk in _voice_chunks(run.strip()):
            if re.search(r"\w", chunk): pieces.append((chunk, vlang))
    return pieces[:MAX_VOICE_SEGMENTS]

def send_voice(to, text, lang):
    text = _EMOJI.sub("", clean_text(text))[:MAX_VOICE_CHARS].strip()
    pieces = _voice_pieces(speech_text(text, lang), lang)
    if not pieces: return
    audio = BytesIO()
    for chunk, vlang in pieces:
        kw = {"tld": "co.in"} if vlang == "en" else {}
        gTTS(text=chunk, lang=vlang, **kw).write_to_fp(audio)
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.close(fd)
        with open(path, "wb") as f: f.write(audio.getvalue())
        mid = upload_media(path, "audio/mpeg")
        graph_post("messages", {"messaging_product":"whatsapp","to":to,"type":"audio","audio":{"id":mid}})
    finally:
        if os.path.exists(path): os.remove(path)

def send_reply(to, text, lang):
    """Text first, then a voice copy. Voice failure never blocks the text."""
    text = (text or "").strip() or MSG["retry"]
    send_text(to, text)
    try: send_voice(to, text, lang)
    except Exception as e: log.warning(f"voice failed: {e}")


# ---------- IMAGE GENERATION ----------
_img_lock = threading.Lock()
_last_img = 0.0
IMG_GAP = 46  # free limit: 1 image per 45 s per IP

def generate_image_bytes(prompt, lang_code="ta"):
    global _last_img
    full_prompt = prompt
    if "diwali" in prompt.lower():
        full_prompt += ", beautiful Diwali festival, oil lamps, rangoli, colorful, 4k, festive, no text"
    url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(full_prompt)}?width=1024&height=1024&nologo=true&model=flux"
    with _img_lock:  # one request at a time, spaced out to respect the free limit
        for attempt in range(3):
            gap = IMG_GAP - (time.time() - _last_img)
            if gap > 0: time.sleep(gap)
            _last_img = time.time()
            try:
                r = requests.get(url, timeout=90)
                if r.ok and r.headers.get("Content-Type", "").startswith("image"):
                    return r.content
                log.error(f"Image gen HTTP {r.status_code}: {r.text[:200]}")
                m = re.search(r"Retry after (\d+)s", r.text)
                if m: time.sleep(min(int(m.group(1)) + 2, 60))
            except Exception as e:
                log.error(f"Image gen failed: {e}")
    return None

IMAGE_NOUNS = ("image", "picture", "photo", "poster", "logo", "greeting card")
IMAGE_VERBS = ("generate", "create", "draw", "make", "design", "venum", "vendum")

def wants_image(lower):
    return (any(n in lower for n in IMAGE_NOUNS) and any(v in lower for v in IMAGE_VERBS)) \
        or ("generate" in lower and "diwali" in lower)


# ---------- READ IMAGE ----------
def prepare_image(data):
    try:
        img = ImageOps.exif_transpose(Image.open(BytesIO(data)))
        img.thumbnail((1600, 1600))
        if img.mode != "RGB": img = img.convert("RGB")
        out = BytesIO(); img.save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception as e:
        raise UnsupportedFile(f"bad image: {e}")

def explain_image(sender, data, caption):
    jpeg = prepare_image(data)
    part = types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")
    result, lang = ask_with_lang([part, f"Caption: {caption}" if caption else "No caption."], IMAGE_PROMPT)
    send_reply(sender, result.get("reply") or MSG["retry"], lang)


# ---------- READ PDF / WORD ----------
def read_pdf(data):
    """Returns (page_count, text, image_parts). Scanned PDFs fall back to page images."""
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass: raise EmptyFile()
        pages = doc.page_count
        text = "\n".join(p.get_text() for p in doc).strip()
        if len(re.sub(r"\s+", "", text)) >= 50:
            return pages, text[:MAX_PDF_TEXT_CHARS], []
        parts = []
        for i in range(min(pages, MAX_PDF_IMAGE_PAGES)):
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(1.5, 1.5))
            parts.append(types.Part.from_bytes(data=pix.tobytes("jpeg"), mime_type="image/jpeg"))
        if not parts: raise EmptyFile()
        return pages, "", parts
    finally:
        doc.close()

def read_docx(data):
    d = Document(BytesIO(data))
    lines = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells: lines.append(" | ".join(cells))
    text = "\n".join(lines).strip()
    if not text: raise EmptyFile()
    return text[:MAX_PDF_TEXT_CHARS]

def explain_pdf(sender, data, filename, caption):
    pages, text, image_parts = read_pdf(data)
    contents = image_parts if image_parts else [f"Document text:\n{text}"]
    contents = list(contents) + [f"Caption: {caption}" if caption else "No caption."]
    result, lang = ask_with_lang(contents, DOC_PROMPT)
    header = f"📄 {filename} ({pages} page{'s' if pages != 1 else ''})"
    send_reply(sender, f"{header}\n\n{result.get('reply') or MSG['retry']}", lang)

def explain_docx(sender, data, filename, caption):
    text = read_docx(data)
    result, lang = ask_with_lang([f"Document text:\n{text}", f"Caption: {caption}" if caption else "No caption."], DOC_PROMPT)
    send_reply(sender, f"📝 {filename}\n\n{result.get('reply') or MSG['retry']}", lang)


# ---------- CREATE LETTER PDF ----------
def build_letter_pdf(d, lang):
    if not fonts_ready(): ensure_fonts()
    lab = d.get("labels") or {}
    esc = lambda s: htmllib.escape(str(s or "")).replace("\n", "<br>")
    def block(label, *lines):
        lines = [esc(l) for l in lines if l]
        if not lines: return ""
        head = f"<b>{esc(label)}</b><br>" if label else ""
        return f"<p>{head}{'<br>'.join(lines)}</p>"
    date = d.get("date") or datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%d-%m-%Y")
    rtl = base_lang(lang) in RTL_LANGS
    body_html = "".join([
        block(lab.get("date"), date),
        block(lab.get("from"), d.get("sender_name"), d.get("sender_address")),
        block(lab.get("to"), d.get("recipient_title"), d.get("recipient_address")),
        f"<p><b>{esc(lab.get('subject'))}</b> {esc(d.get('subject'))}</p>" if d.get("subject") else "",
        block("", d.get("salutation")),
        block("", d.get("body")),
        block("", d.get("closing"), d.get("signature_name")),
    ])
    family = "NotoTamil, NotoMain, sans-serif" if base_lang(lang) == "ta" else "NotoMain, NotoTamil, sans-serif"
    css = (
        "@font-face {font-family: NotoTamil; src: url(NotoSansTamil-Regular.ttf);}"
        "@font-face {font-family: NotoMain; src: url(NotoSans-Regular.ttf);}"
        f"body {{font-family: {family}; font-size: 12pt; line-height: 1.5;}}"
        "p {margin: 0 0 12pt 0;}"
    )
    html = f'<div dir="{"rtl" if rtl else "ltr"}">{body_html}</div>'
    fd, path = tempfile.mkstemp(suffix=".pdf"); os.close(fd)
    story = fitz.Story(html=html, user_css=css, archive=fitz.Archive(FONT_DIR))
    writer = fitz.DocumentWriter(path)
    mediabox = fitz.paper_rect("a4")
    where = mediabox + (50, 50, -50, -50)
    more = 1
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return path

def send_letter(sender, result, lang):
    path = None
    try:
        path = build_letter_pdf(result, lang)
        name = re.sub(r"[^\w\-]+", "_", result.get("doc_type") or "document").strip("_") or "document"
        ready = (result.get("ready_message") or "").strip()
        if ready: send_text(sender, ready)
        send_document(sender, path, f"{name}.pdf")
    except Exception as e:
        log.exception(f"PDF create failed: {e}")
        safe_send_text(sender, MSG["pdf_fail"])
    finally:
        if path and os.path.exists(path): os.remove(path)


# ---------- MESSAGE HANDLING ----------
def route_result(sender, result, lang, user_text=""):
    """Shared by text and voice: chat / image / document."""
    kind = result.get("kind", "chat")
    if kind == "image":
        ack = (result.get("reply") or "").strip()
        if ack: send_text(sender, ack)
        prompt = result.get("image_prompt") or user_text or "a beautiful colorful festive picture"
        img_bytes = generate_image_bytes(prompt, lang)
        if img_bytes: send_image(sender, img_bytes, "✨")
        else: safe_send_text(sender, MSG["img_fail"])
    elif kind == "document":
        send_letter(sender, result, lang)
    else:
        send_reply(sender, result.get("reply") or "", lang)

def handle_text(sender, message):
    user_text = message.get("text", {}).get("body", "").strip()
    if not user_text: return
    if wants_image(user_text.lower()):
        send_text(sender, "Image generate pannuren pa... 🎨 1 min wait pannunga pa")
        img_bytes = generate_image_bytes(user_text, "ta")
        if img_bytes:
            send_image(sender, img_bytes, "Ungal image ready pa! ✨")
            try: send_voice(sender, "உங்கள் படம் தயார்!", "ta")
            except Exception as e: log.warning(f"voice failed: {e}")
        else:
            safe_send_text(sender, MSG["img_fail"])
        return

    result, lang = ask_with_lang(TEXT_PROMPT + user_text, SYSTEM_PROMPT + " Reply with JSON only.")
    route_result(sender, result, lang, user_text)

def handle_audio(sender, message):
    data, mime = download_media(message["audio"]["id"])
    part = types.Part.from_bytes(data=data, mime_type=mime or "audio/ogg")
    result, lang = ask_with_lang(
        [part, TEXT_PROMPT + "(The user's message is in the attached audio. Listen to it and follow it.)"],
        SYSTEM_PROMPT + " Reply with JSON only.")
    route_result(sender, result, lang)

def handle_image(sender, message):
    media = message["image"]
    data, _ = download_media(media["id"])
    explain_image(sender, data, (media.get("caption") or "").strip())

def handle_document(sender, message):
    media = message["document"]
    filename = media.get("filename") or "file"
    name, mime = filename.lower(), (media.get("mime_type") or "").lower()
    caption = (media.get("caption") or "").strip()
    is_pdf = mime == "application/pdf" or name.endswith(".pdf")
    is_docx = mime == DOCX_MIME or name.endswith(".docx")
    is_img = mime.startswith("image/")
    if not (is_pdf or is_docx or is_img): raise UnsupportedFile(filename)
    data, _ = download_media(media["id"])
    if is_pdf: explain_pdf(sender, data, filename, caption)
    elif is_docx: explain_docx(sender, data, filename, caption)
    else: explain_image(sender, data, caption)

def process_message(sender, message, message_id):
    if already_processed(message_id): return
    kind = message.get("type", "")
    if kind == "reaction": return
    try:
        if kind == "text": handle_text(sender, message)
        elif kind == "audio": handle_audio(sender, message)
        elif kind == "image": handle_image(sender, message)
        elif kind == "document": handle_document(sender, message)
        else: safe_send_text(sender, MSG["type"])
    except UnsupportedFile:
        safe_send_text(sender, MSG["type"])
    except TooBig:
        safe_send_text(sender, MSG["too_big"])
    except EmptyFile:
        safe_send_text(sender, MSG["empty"])
    except Exception as e:
        log.exception(f"process_message failed: {e}")
        safe_send_text(sender, MSG["retry"])


# ---------- WEBHOOK ----------
def valid_signature(raw, header):
    if not META_APP_SECRET: return True
    expected = "sha256=" + hmac.new(META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")

@app.get("/")
def health(): return PlainTextResponse("Kan Paarvai AI is running")

@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if VERIFY_TOKEN and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)

@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not valid_signature(raw, request.headers.get("X-Hub-Signature-256")):
        return PlainTextResponse("invalid signature", status_code=403)
    try: payload = json.loads(raw)
    except ValueError: return PlainTextResponse("bad request", status_code=400)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []) or []:
                background.add_task(process_message, msg.get("from"), msg, msg.get("id"))
    return PlainTextResponse("ok")
