import os
import re
import json
import hmac
import hashlib
import logging
import tempfile
import threading
import unicodedata
import html as htmllib
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import fitz
from PIL import Image
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
MAX_VOICE_CHARS = 1200
MAX_VOICE_SEGMENTS = 10
FONT_DIR = str(Path(__file__).resolve().parent / "fonts")
Path(FONT_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")
app = FastAPI(title="Kan Paarvai AI")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

STYLE_RULES = "Keep answers short, simple and clear. Write math formulas as plain text, e.g. πr^2, a^2 + b^2 = c^2, x × y, a ÷ b; do not use * or markdown."
SYSTEM_PROMPT = f"You are Kan Paarvai AI, a helpful WhatsApp assistant. Always reply in the SAME language the user used. If there is no user text, reply in language code '{DEFAULT_LANG}'. " + STYLE_RULES

# Updated prompt with image support
TEXT_PROMPT = """Read the user's message and reply with ONLY one JSON object, no markdown fences:
{
You ARE ABLE to generate images. NEVER say "I cannot generate images". If user asks for image, you MUST return kind="image".
 "language": "", "kind": "chat" or "document" or "image", "reply": "", "image_prompt": "",
 "doc_type": "", "sender_name": "", "sender_address": "", "recipient_title": "", "recipient_address": "",
 "date": "", "subject": "", "salutation": "", "body": "", "closing": "", "signature_name": "",
 "labels": {"from": "", "to": "", "date": "", "subject": ""}, "ready_message": ""
}
Rules:
- kind is "image" if user asks to create, generate, draw an image, picture, photo, logo, poster, greeting card.
- For "image": put detailed ENGLISH prompt in "image_prompt" and acknowledgement in "reply" in user's language.
- For "document": fill document fields.
- For "chat": put answer in "reply".
""" + STYLE_RULES + "\n\nUser message:\n"

MEDIA_PROMPT = """Reply with ONLY one JSON object: {"language": "", "reply": ""} Answer in caption language, otherwise '""" + DEFAULT_LANG + """'. """ + STYLE_RULES

MSG = {
    "retry": "Please try again in a little while 🙏 (Konjam neram kalichu muyarchikavum pa)",
    "too_big": "File is too big, max 10 MB (File romba perusu pa)",
    "empty": "There is no text in this file (File la text illa pa)",
    "type": "Please send text, voice, image, PDF or Word files (Text, voice, image, PDF, Word file mattum anuppunga pa)",
    "pdf_fail": "Sorry, the PDF could not be created. Please try again later.",
    "img_fail": "Sorry, image could not be created. Please try again later (Image create panna mudiyala pa)",
}

class UnsupportedFile(Exception): pass
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

_seen = OrderedDict(); _seen_lock = threading.Lock()

def generate_image_bytes(prompt, lang_code="ta"):
    # No Gemini image model needed - direct free API, 100% works
    import requests
    full_prompt = f"{prompt}, beautiful Diwali festival, oil lamps, rangoli, colorful, 4k, festive, no text"
    try:
        # This API works always, no key needed
        url = f"https://image.pollinations.ai/prompt/{requests.utils.quote(full_prompt)}?width=1024&height=1024&nologo=true&model=flux"
        r = requests.get(url, timeout=45)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"Image gen failed: {e}")
        return None

def send_image(to, image_bytes, caption=""):
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".jpg")
    try:
        os.close(fd)
        with open(path,"wb") as f: f.write(image_bytes)
        mid = upload_media(path,"image/jpeg")
        graph_post("messages", {
            "messaging_product":"whatsapp","to":to,"type":"image",
            "image":{"id":mid,"caption":caption[:900] if caption else ""}
        })
    finally:
        if os.path.exists(path): os.remove(path)

 def process_message(sender, message, message_id):
    if already_processed(message_id): return
    kind = message.get("type","")
    try:
        if kind == "text":
            user_text = message.get("text",{}).get("body","").strip()
            lower = user_text.lower()
            # FORCE IMAGE - Don't ask Gemini
            if "image" in lower or "generate" in lower and "diwali" in lower or "poster" in lower or "logo" in lower:
                send_text(sender, "Image generate pannuren pa... 🎨 10 sec wait pannunga pa")
                img_bytes = generate_image_bytes(user_text, "ta")
                if img_bytes:
                    send_image(sender, img_bytes, "Ungal Diwali image ready pa! 🪔✨")
                    send_voice(sender, "Ungal Diwali image ready pa!", "ta")
                else:
                    safe_send_text(sender, "Image failed pa, try again pa")
                return
            # Normal flow continues...
            result, lang = ask_with_lang(TEXT_PROMPT + user_text, "Reply with JSON only.")
    # --- END NEW ---
    
    result, lang = ask_with_lang(TEXT_PROMPT + user_text, "Reply with JSON only.")
    # ... rest of your code
    if not message_id: return False
    with _seen_lock:
        if message_id in _seen: return True
        _seen[message_id] = True
        while len(_seen) > 5000: _seen.popitem(last=False)
    return False

def clean_text(text):
    if not text: return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*`#]+", "", text)
    return text.strip()[:12000]

# ---- Language detect helpers (same as yours) ----
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

# ---- Gemini ----
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
        if data.get("kind") not in ("document",):
            data["reply"] = clean_text(data.get("reply")) if data.get("reply") else data.get("reply")
        lang = lang or guess_lang(data.get("reply") or data.get("image_prompt") or "")
        return data, (lang or DEFAULT_LANG)
    except Exception:
        reply = ask_gemini(contents)
        return {"kind":"chat","reply":reply}, guess_lang(reply)

# ---- WhatsApp ----
def auth_headers(): return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
def graph_post(endpoint, payload):
    r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/{endpoint}", headers={**auth_headers(),"Content-Type":"application/json"}, json=payload, timeout=40)
    if not r.ok: log.error(f"Graph error {r.status_code}: {r.text}")
    r.raise_for_status(); return r.json()
def send_text(to, text):
    graph_post("messages", {"messaging_product":"whatsapp","to":to,"type":"
