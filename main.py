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
from html.parser import HTMLParser
from collections import OrderedDict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import fitz  # PyMuPDF
from PIL import Image, ImageOps
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse, HTMLResponse
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
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "Kan Paarvai AI")
CONTACT_EMAIL = os.getenv("CONTACT_EMAIL", "")
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
TAMIL_HINT = (" If you write Tamil, use correct standard spelling and proper school maths terms "
              "(e.g. வடிவொத்த முக்கோணங்கள், தொடுகோடு, நேர்மாறுபாடு, எதிர்மாறுபாடு). Never put symbols like • inside words.")
LANG_HINT = ' "language" is the ISO 639-1 code of your reply (e.g. ta, en, hi).'
SYSTEM_PROMPT = f"You are Kan Paarvai AI, a helpful WhatsApp assistant. Always reply in the SAME language the user used. If there is no user text, reply in language code '{DEFAULT_LANG}'. " + STYLE_RULES + TAMIL_HINT

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

MEDIA_PROMPT = """Reply with ONLY one JSON object: {"language": "", "reply": ""} Answer in caption language, otherwise '""" + DEFAULT_LANG + """'. """ + STYLE_RULES + LANG_HINT + TAMIL_HINT + " "

VOICE_PROMPT = 'Reply with ONLY one JSON object: {"language": "", "reply": ""} Listen to the audio and answer it in the same language the speaker used. ' + STYLE_RULES + LANG_HINT

IMAGE_PROMPT = MEDIA_PROMPT + (
    "Task: look at the image and explain in 3-5 short lines what is in it: the main subject, "
    "important details, and any visible text (read it briefly). If the caption asks a question, answer it instead."
)

DOC_PROMPT = MEDIA_PROMPT + (
    "Task: explain in 4-6 short lines what this document contains: what kind of document it is, "
    "the main topics, and the key points or numbers. If the caption asks a question, answer it too."
)

HTML_PROMPT = MEDIA_PROMPT + (
    "Task: this is a web page (HTML file). Explain in 4-6 short lines what it contains: what the page is about "
    "or does, its main sections, and the key points. Do not show code. If the caption asks a question, answer it too."
)

MSG = {
    "retry": "Please try again in a little while 🙏 (Konjam neram kalichu muyarchikavum pa)",
    "too_big": "File is too big, max 10 MB (File romba perusu pa)",
    "empty": "There is no text in this file (File la text illa pa)",
    "type": "Please send text, voice, image, PDF, Word or HTML files (Text, voice, image, PDF, Word, HTML file mattum anuppunga pa)",
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


# ---------- TAMIL MATHS SPELLING ----------
TAMIL_RANGE = "\u0B80-\u0BFF"
TAMIL_FIXES = [  # (wrong, right) - add more here whenever you spot a repeated mistake
    ("வடிசவொ", "வடிவொ"),                  # வடிவொத்த (similar) triangles
    ("நேர்மொறுபாடு", "நேர்மாறுபாடு"),      # direct variation
    ("எதிர்மொறுபாடு", "எதிர்மாறுபாடு"),    # inverse variation
    ("மொறுபாடு", "மாறுபாடு"),
]

def correct_tamil_maths_spelling(text):
    """Fix common broken Tamil spellings/symbols that come from garbled PDF text."""
    if not text or not re.search(f"[{TAMIL_RANGE}]", text): return text
    text = unicodedata.normalize("NFC", text)                      # ெ+ா -> ொ
    text = re.sub("[\u0BCA\u0BCB]\u0BBE", lambda m: m.group(0)[0], text)   # ொா -> ொ, ோா -> ோ
    text = re.sub(f"(?<=[{TAMIL_RANGE}])[ \\t]*[•·][ \\t]*(?=[{TAMIL_RANGE}])", "", text)  # ஆவண • மாகும்
    for wrong, right in TAMIL_FIXES:
        text = text.replace(wrong, right)
    return re.sub(r"[ \t]{2,}", " ", text).strip()

def clean_for_speech(text):
    """Spelling first, then drop leftover bullets (they are never spoken)."""
    return re.sub(r"[•·●▪]", " ", correct_tamil_maths_spelling(text))

def looks_garbled(text):
    """True when extracted PDF text is probably broken (old Tamil fonts): stray bullets, orphan vowel signs."""
    flat = re.sub(r"\s+", "", text or "")
    if not flat: return True
    tamil = sum(1 for c in flat if "\u0B80" <= c <= "\u0BFF")
    if tamil:
        orphans = len(re.findall(r"(?:^|[\s•])[\u0BBE-\u0BCD]", text))
        doubles = len(re.findall("[\u0BC6\u0BC7\u0BCA\u0BCB]\u0BBE", text))
        return (orphans + doubles) / tamil > 0.01 or text.count("•") / len(flat) > 0.003
    odd = sum(1 for c in flat if "\u00c0" <= c <= "\u00ff" or c == "\ufffd")
    return odd / len(flat) > 0.05


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

MATH_WORDS = {
    "en": dict(plus="plus", minus="minus", times="times", div="divided by", eq="equals",
               ne="is not equal to", lt="is less than", gt="is greater than",
               le="is less than or equal to", ge="is greater than or equal to",
               sq="squared", cube="cubed", pow="to the power of", root="square root of",
               pi="pi", pct="percent", range="{0} to {1}"),
    "ta": dict(plus="கூட்டல்", minus="கழித்தல்", times="பெருக்கல்", div="வகுத்தல்", eq="சமம்",
               ne="சமம் அல்ல", lt="சிறியது", gt="பெரியது",
               le="சிறியது அல்லது சமம்", ge="பெரியது அல்லது சமம்",
               sq="வர்க்கம்", cube="கனம்", pow="அடுக்கு", root="வர்க்கமூலம்",
               pi="பை", pct="சதவீதம்", range="{0} முதல் {1} வரை"),
    "hi": dict(plus="जमा", minus="घटा", times="गुणा", div="भाग", eq="बराबर",
               ne="बराबर नहीं", lt="छोटा", gt="बड़ा", le="छोटा या बराबर", ge="बड़ा या बराबर",
               sq="का वर्ग", cube="का घन", pow="की घात", root="वर्गमूल",
               pi="पाई", pct="प्रतिशत", range="{0} से {1} तक"),
}
# Tamil letter names, so variables (a, b, x...) are read by the Tamil voice
TA_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz", [
    "ஏ", "பி", "சி", "டி", "இ", "எஃப்", "ஜி", "எச்", "ஐ", "ஜே", "கே", "எல்", "எம்",
    "என்", "ஓ", "பீ", "கியூ", "ஆர்", "எஸ்", "டீ", "யூ", "வி", "டபிள்யூ", "எக்ஸ்", "வை", "இசட்"]))

_VAR = r"(?<![A-Za-z])[A-Za-z](?![A-Za-z])"
_LEFT = rf"(?:{_VAR}|\d[A-Za-z]{{1,3}}(?![A-Za-z])|\d|\)|π|ரூபாய்|रुपये|rupees)(?:\^\(?-?\w+\)?|[²³])?"
# right operand: number, "(", √, π, or a lone letter that is not an article ("a great deal")
_RIGHT = rf"(?=[\d(√π]|(?<![A-Za-z])[A-Za-z](?![A-Za-z])(?!\s+[A-Za-z]{{2,}}))"

def _binop(text, op, word):
    return re.sub(rf"({_LEFT})\s*(?:{op})\s*{_RIGHT}", lambda m: f"{m.group(1)} {word} ", text)

def math_speech(text, native):
    """Speak formulas: a^2 - b^2 = c -> 'a squared minus b squared equals c' (in the reply's language)."""
    w = MATH_WORDS.get(native, MATH_WORDS["en"])
    text = text.replace("<=", "≤").replace(">=", "≥").replace("!=", "≠")
    text = re.sub(r"(\d{1,2}:\d{2})\s*[-–]\s*(\d{1,2}:\d{2})",
                  lambda m: w["range"].format(m.group(1), m.group(2)), text)   # 10:30 - 11:30
    mathy = bool(re.search(r"[=+×÷^√π≤≥≠²³<>]|\d\s*[*/]\s*\d|(?<![A-Za-z])[A-Za-z]\s*[-−–]\s*[\dA-Za-z(]", text))
    if not mathy:  # plain "10-20" is a range, not a subtraction
        text = re.sub(r"(?<![\d:.])(\d+)\s*[-–]\s*(\d+)(?![\d:.])",
                      lambda m: w["range"].format(m.group(1), m.group(2)), text)
    for op, key in ((r"[-−–]", "minus"), (r"\+", "plus"), (r"[*×]", "times"),
                    (r"[÷/]", "div"), (r"<", "lt"), (r">", "gt")):
        text = _binop(text, op, w[key])
    for sym, key in (("=", "eq"), ("≠", "ne"), ("≤", "le"), ("≥", "ge"), ("π", "pi"),
                     ("√", "root"), ("÷", "div"), ("×", "times"), ("%", "pct")):
        text = text.replace(sym, f" {w[key]} ")
    text = re.sub(r"\^\s*2(?!\d)|²", f" {w['sq']} ", text)
    text = re.sub(r"\^\s*3(?!\d)|³", f" {w['cube']} ", text)
    text = re.sub(r"\^\s*\(?\s*(-?[\w.]+)\s*\)?", lambda m: f" {w['pow']} {m.group(1)} ", text)
    text = re.sub(rf"(?<![\w)])[-−](?=\d|\(|{_VAR})", f" {w['minus']} ", text)   # negative numbers: -5
    if native == "ta" and mathy:
        text = re.sub(r"(?<![A-Za-z/])[A-Za-z](?![A-Za-z/])", lambda m: f" {TA_LETTERS[m.group(0).lower()]} ", text)
        text = re.sub(r"(?<=\d)([A-Za-z]{1,3})(?![A-Za-z])",
                      lambda m: " " + " ".join(TA_LETTERS[c.lower()] for c in m.group(1)) + " ", text)  # 2ab
    text = re.sub(r"(வரை|तक)(\s+\1)+", r"\1", text)   # text already said "வரை"/"तक"
    return re.sub(r"[ \t]{2,}", " ", text)

def speech_text(text, lang):
    """Make text safe for TTS: remove URLs, 1,000 -> 1000, 19.11.2026 -> 19 11 2026,
    Rs / ₹ / INR / ரூ -> rupees word, and maths symbols (+ - × ÷ = ^ √ π ...) -> spoken words."""
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    native = None if is_latin_text(text) else guess_lang(text)
    word = RUPEE_WORDS.get(native, "rupees")
    text = re.sub(r"(?<=\d),(?=\d)", "", text)
    text = re.sub(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", r"\1 \2 \3", text)
    num = r"\d+(?:\.\d+)?"
    text = re.sub(rf"(?:₹|\bRs\.?|\bINR|ரூ\.?)\s*({num})", rf"\1 {word}", text, flags=re.I)
    text = re.sub(rf"({num})\s*(?:₹|/-|Rs\.?(?!\w)|INR(?!\w))", rf"\1 {word}", text, flags=re.I)
    text = re.sub(r"₹|\bRs\.?(?!\w)|\bINR\b", word, text, flags=re.I)
    return math_speech(text, native)

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
    text = clean_for_speech(_EMOJI.sub("", clean_text(text)))[:MAX_VOICE_CHARS].strip()
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
    text = correct_tamil_maths_spelling((text or "").strip()) or MSG["retry"]
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
def _sample_pages(pages, n):
    if pages <= n: return list(range(pages))
    return sorted({round(i * (pages - 1) / (n - 1)) for i in range(n)})

def read_pdf(data):
    """Returns (page_count, text, image_parts). Scanned or garbled PDFs are read from page images."""
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        if doc.needs_pass: raise EmptyFile()
        pages = doc.page_count
        text = "\n".join(p.get_text() for p in doc).strip()
        if len(re.sub(r"\s+", "", text)) >= 50 and not looks_garbled(text):
            return pages, text[:MAX_PDF_TEXT_CHARS], []
        parts = []
        for i in _sample_pages(pages, MAX_PDF_IMAGE_PAGES):   # spread across the file, not just the first pages
            pix = doc[i].get_pixmap(matrix=fitz.Matrix(2, 2))
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
    contents = ([*image_parts, f"These are {len(image_parts)} sample pages, spread across a {pages}-page PDF."]
                if image_parts else [f"Document text:\n{text}"])
    contents = list(contents) + [f"Caption: {caption}" if caption else "No caption."]
    result, lang = ask_with_lang(contents, DOC_PROMPT)
    header = f"📄 {filename} ({pages} page{'s' if pages != 1 else ''})"
    send_reply(sender, f"{header}\n\n{result.get('reply') or MSG['retry']}", lang)

def explain_docx(sender, data, filename, caption):
    text = read_docx(data)
    result, lang = ask_with_lang([f"Document text:\n{text}", f"Caption: {caption}" if caption else "No caption."], DOC_PROMPT)
    send_reply(sender, f"📝 {filename}\n\n{result.get('reply') or MSG['retry']}", lang)


# ---------- READ HTML ----------
class _HTMLReader(HTMLParser):
    """Collects visible text (no scripts/styles) plus a few structure facts."""
    SKIP = {"script", "style", "noscript", "template", "svg"}
    HEADS = {"h1", "h2", "h3"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
             "header", "footer", "ul", "ol", "table", "pre", "blockquote", "nav", "main"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.headings = [], []
        self.title = ""
        self.links = self.images = self.forms = self.scripts = 0
        self._skip, self._in_title, self._head = 0, False, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
            if tag == "script": self.scripts += 1
            return
        if tag == "title": self._in_title = True
        elif tag == "a" and dict(attrs).get("href"): self.links += 1
        elif tag == "img": self.images += 1
        elif tag == "form": self.forms += 1
        if tag in self.HEADS: self._head = []
        if tag in self.BLOCK: self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1); return
        if tag == "title": self._in_title = False
        if tag in self.HEADS and self._head is not None:
            h = " ".join("".join(self._head).split())
            if h: self.headings.append(h)
            self._head = None
        if tag in self.BLOCK: self.text.append("\n")

    def handle_data(self, data):
        if self._skip: return
        if self._in_title: self.title += data; return
        self.text.append(data)
        if self._head is not None: self._head.append(data)

def decode_html(data):
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"): return data.decode("utf-16", errors="replace")
    try: return data.decode("utf-8-sig")
    except UnicodeDecodeError: return data.decode("cp1252", errors="replace")

def read_html(data):
    p = _HTMLReader()
    p.feed(decode_html(data)); p.close()
    body = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.text))
    body = re.sub(r"\n\s*\n+", "\n", body).strip()
    title = " ".join(p.title.split())
    if not body and not title and not p.scripts: raise EmptyFile()
    return (f"Title: {title or '-'}\n"
            f"Headings: {' | '.join(p.headings[:15]) or '-'}\n"
            f"Links: {p.links}, Images: {p.images}, Forms: {p.forms}, Scripts: {p.scripts}\n\n"
            f"Visible text:\n{body[:MAX_PDF_TEXT_CHARS] or '(none - the page is built by scripts)'}")

def explain_html(sender, data, filename, caption):
    info = read_html(data)
    result, lang = ask_with_lang([f"HTML page summary:\n{info}", f"Caption: {caption}" if caption else "No caption."], HTML_PROMPT)
    send_reply(sender, f"🌐 {filename}\n\n{result.get('reply') or MSG['retry']}", lang)


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
    is_html = mime in ("text/html", "application/xhtml+xml") or name.endswith((".html", ".htm", ".xhtml"))
    is_img = mime.startswith("image/")
    if not (is_pdf or is_docx or is_html or is_img): raise UnsupportedFile(filename)
    data, _ = download_media(media["id"])
    if is_pdf: explain_pdf(sender, data, filename, caption)
    elif is_docx: explain_docx(sender, data, filename, caption)
    elif is_html: explain_html(sender, data, filename, caption)
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


# ---------- PRIVACY POLICY (served at /privacy) ----------
PRIVACY_UPDATED = "November 7, 2026"
_PRIVACY_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - @@BIZ@@</title>
<style>
:root{--bg:#ffffff;--fg:#1d1d1f;--muted:#5f6368;--accent:#0b7a4b}
@media (prefers-color-scheme: dark){:root{--bg:#121212;--fg:#ececec;--muted:#a0a4a8;--accent:#4cc38a}}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.6;margin:0}
main{max-width:760px;margin:0 auto;padding:24px 18px 48px}
h1{font-size:1.7rem;margin:.2em 0}h2{font-size:1.15rem;margin:1.6em 0 .4em;color:var(--accent)}
p,li{font-size:1rem}.muted{color:var(--muted);font-size:.9rem}
</style></head><body><main>
<h1>Privacy Policy</h1>
<p class="muted">@@BIZ@@ &middot; Last updated: @@DATE@@</p>

<h2>1. About this service</h2>
<p>@@BIZ@@ ("we", "us") is an AI assistant that works inside WhatsApp. You send a message to our WhatsApp number and we reply. This policy explains what information we handle when you use it.</p>

<h2>2. Information we receive</h2>
<ul>
<li><b>Your phone number and WhatsApp name</b>, provided by WhatsApp so we can reply to you.</li>
<li><b>The messages you send</b>: text, voice notes, images, and PDF, Word or HTML files.</li>
<li><b>Technical information</b> such as message IDs and error details, which our hosting provider may keep in server logs.</li>
</ul>
<p>We only act on messages you send to us. We do not send promotions or unsolicited messages.</p>

<h2>3. How we use it</h2>
<ul>
<li>To understand your message and reply in your language, as text and as a short voice message.</li>
<li>To describe your images, summarise your files, create images you ask for, and create letters as PDF.</li>
<li>To keep the service working, prevent duplicate replies and fix errors.</li>
</ul>
<p>We do not sell your information and we do not use it for advertising.</p>

<h2>4. Other services that process your content</h2>
<p>To give you a reply, your message content is sent to these providers, who handle it under their own terms and privacy policies:</p>
<ul>
<li><b>Meta / WhatsApp</b>: delivers messages and media between you and us.</li>
<li><b>Google Gemini</b>: reads your text, voice, images and file contents and writes the reply.</li>
<li><b>Google text-to-speech</b>: turns the reply text into the voice message.</li>
<li><b>Pollinations (image generation)</b>: receives only the description of the image you asked for.</li>
<li><b>Our hosting provider (Render)</b>: runs the service and may keep technical logs.</li>
</ul>

<h2>5. How long we keep information</h2>
<p>We do not build a profile or a history of your conversations. Files, images and audio are handled in temporary memory or temporary files and are deleted once your reply is sent. Message IDs are held in memory only to avoid answering the same message twice and are lost when the service restarts. Technical logs kept by our hosting provider may contain your phone number and error details for a limited time set by that provider.</p>

<h2>6. Your choices and deletion</h2>
<p>You can stop using the service at any time by no longer messaging us, and you can block the number in WhatsApp. To ask what we hold about you, or to have any data about you deleted, email us at @@EMAIL@@ with your WhatsApp number and we will respond within a reasonable time.</p>

<h2>7. Security</h2>
<p>Messages from WhatsApp are verified using Meta's signature, and data is sent over encrypted connections. No online service can be guaranteed to be completely secure.</p>

<h2>8. Children</h2>
<p>This service is not directed at children under 13, and we do not knowingly collect information from them.</p>

<h2>9. AI-generated content</h2>
<p>Replies are produced by AI and may contain mistakes. Please do not rely on them for medical, legal or financial decisions, and do not send sensitive personal documents such as passwords or bank details.</p>

<h2>10. Changes</h2>
<p>We may update this policy. The date at the top shows when it last changed.</p>

<h2>11. Contact</h2>
<p>@@BIZ@@ &middot; @@EMAIL@@</p>
</main></body></html>"""

def privacy_html():
    email = htmllib.escape(CONTACT_EMAIL) if CONTACT_EMAIL else "[set the CONTACT_EMAIL setting]"
    return (_PRIVACY_TEMPLATE.replace("@@BIZ@@", htmllib.escape(BUSINESS_NAME))
            .replace("@@DATE@@", PRIVACY_UPDATED).replace("@@EMAIL@@", email))


# ---------- TERMS OF SERVICE (served at /terms) ----------
_TERMS_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terms of Service - @@BIZ@@</title>
<style>
:root{--bg:#ffffff;--fg:#1d1d1f;--muted:#5f6368;--accent:#0b7a4b}
@media (prefers-color-scheme: dark){:root{--bg:#121212;--fg:#ececec;--muted:#a0a4a8;--accent:#4cc38a}}
body{background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;line-height:1.6;margin:0}
main{max-width:760px;margin:0 auto;padding:24px 18px 48px}
h1{font-size:1.7rem;margin:.2em 0}h2{font-size:1.15rem;margin:1.6em 0 .4em;color:var(--accent)}
p,li{font-size:1rem}.muted{color:var(--muted);font-size:.9rem}a{color:var(--accent)}
</style></head><body><main>
<h1>Terms of Service</h1>
<p class="muted">@@BIZ@@ &middot; Last updated: @@DATE@@</p>

<h2>1. Acceptance</h2>
<p>By sending a message to @@BIZ@@ ("the Service") on WhatsApp, you agree to these terms. If you do not agree, please stop using the Service. Your use of WhatsApp is also governed by WhatsApp's own terms.</p>

<h2>2. What the Service does</h2>
<p>The Service is an AI assistant on WhatsApp. It answers questions, explains images, summarises PDF, Word and HTML files, creates images, creates letters as PDF, and replies as text and voice. It replies only to messages you send to it.</p>

<h2>3. AI replies can be wrong</h2>
<p>Replies are generated by AI and may be incomplete, outdated or incorrect, including spellings and calculations. Please check important information yourself. Do not rely on the Service for medical, legal, financial or safety decisions.</p>

<h2>4. Acceptable use</h2>
<p>You agree not to use the Service to:</p>
<ul>
<li>break the law or violate anyone's rights;</li>
<li>create or share harmful, hateful, sexual, violent or misleading content, or images of real people without their permission;</li>
<li>send malware, spam, or attempt to disrupt, overload or hack the Service;</li>
<li>send passwords, bank details or other highly sensitive documents.</li>
</ul>
<p>We may refuse a request, or stop replying to a number that misuses the Service.</p>

<h2>5. Your content</h2>
<p>You keep ownership of what you send. You confirm that you have the right to send it. You allow us and the services described in our <a href="/privacy">Privacy Policy</a> to process it only to produce your reply.</p>

<h2>6. Generated content</h2>
<p>Images, letters and other content the Service creates for you are provided as is. Similar outputs may be produced for other users. You are responsible for how you use them, including checking that they are suitable and do not infringe anyone's rights.</p>

<h2>7. Availability and limits</h2>
<p>The Service is provided free and as is. It may be slow, limited (for example, image creation can take about a minute), interrupted or changed at any time. File size is limited to 10 MB.</p>

<h2>8. Liability</h2>
<p>To the fullest extent allowed by law, we are not responsible for any loss or damage resulting from your use of, or inability to use, the Service, or from any reply it gives.</p>

<h2>9. Privacy</h2>
<p>How we handle your information is described in our <a href="/privacy">Privacy Policy</a>.</p>

<h2>10. Changes and ending</h2>
<p>We may update these terms or stop the Service at any time. The date above shows the latest version. Continuing to use the Service after a change means you accept it. You can stop at any time by no longer messaging us.</p>

<h2>11. Contact</h2>
<p>@@BIZ@@ &middot; @@EMAIL@@</p>
</main></body></html>"""

def terms_html():
    email = htmllib.escape(CONTACT_EMAIL) if CONTACT_EMAIL else "[set the CONTACT_EMAIL setting]"
    return (_TERMS_TEMPLATE.replace("@@BIZ@@", htmllib.escape(BUSINESS_NAME))
            .replace("@@DATE@@", PRIVACY_UPDATED).replace("@@EMAIL@@", email))


# ---------- WEBHOOK ----------
def valid_signature(raw, header):
    if not META_APP_SECRET: return True
    expected = "sha256=" + hmac.new(META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")

@app.get("/")
def health(): return PlainTextResponse("Kan Paarvai AI is running")

@app.get("/privacy")
@app.get("/privacy-policy")
def privacy(): return HTMLResponse(privacy_html())

@app.get("/terms")
@app.get("/terms-of-service")
def terms(): return HTMLResponse(terms_html())

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
