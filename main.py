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
import fitz   # PyMuPDF (pip package name: PyMuPDF)
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
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash"
).split(",") if m.strip()]
# Language used when the user sends only an image/file with no text
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ta")
LOCAL_TZ = os.getenv("LOCAL_TZ", "Asia/Kolkata")
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_PDF_IMAGE_PAGES = 5
MAX_VOICE_CHARS = 1200       # longer replies are cut at a sentence end
MAX_VOICE_SEGMENTS = 10      # limit of language switches per voice note


FONT_DIR = str(Path(__file__).resolve().parent / "fonts")
Path(FONT_DIR).mkdir(parents=True, exist_ok=True)


logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")
app = FastAPI(title="Kan Paarvai AI")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"


STYLE_RULES = (
    "Keep answers short, simple and clear. "
    "Write math formulas as plain text, e.g. πr^2, a^2 + b^2 = c^2, x × y, a ÷ b; "
    "do not use * or markdown."
)

SYSTEM_PROMPT = (
    "You are Kan Paarvai AI, a helpful WhatsApp assistant. "
    "Always reply in the SAME language the user used. If the user writes their "
    "language in English letters (e.g. Tanglish, Hinglish), reply in that "
    f"language's own script. If there is no user text, reply in language code "
    f"'{DEFAULT_LANG}'. " + STYLE_RULES
)

# One Gemini call per text message: decides chat vs. document and the language.
TEXT_PROMPT = """Read the user's message and reply with ONLY one JSON object, no markdown fences:
{
 "language": "",
 "kind": "chat" or "document",
 "reply": "",
 "doc_type": "", "sender_name": "", "sender_address": "",
 "recipient_title": "", "recipient_address": "",
 "date": "", "subject": "", "salutation": "",
 "body": "", "closing": "", "signature_name": "",
 "labels": {"from": "", "to": "", "date": "", "subject": ""},
 "ready_message": ""
}
Rules:
- language: ISO 639-1 code of the user's language (ta, en, hi, te, ml, kn, ar, de, ...).
  Tanglish = ta, Hinglish = hi.
- kind is "document" only if the user asks you to write a letter, application,
  complaint, leave letter, resume or similar formal document. Otherwise "chat".
- For "chat": put your answer in "reply" and leave the document fields empty.
- For "document": leave "reply" empty and fill the document fields.
  doc_type: short English name, e.g. "leave letter".
  labels: the words From, To, Date, Subject translated into that language.
  ready_message: one short sentence in that language saying the letter is ready
  and the ____ blanks must be filled in.
- Write every text field in the user's language and its native script.
- NEVER invent names or addresses. Use "" for anything missing.
- body: paragraphs only, separated by \\n\\n.
""" + STYLE_RULES + "\n\nUser message:\n"

# Media (voice, image, PDF, Word, text) -> chat answer + language
MEDIA_PROMPT = """Reply with ONLY one JSON object: {"language": "", "reply": ""}
- language: ISO 639-1 code of the language you answer in.
- Answer in the language of the user's caption. With no caption: for a voice
  message answer in the language spoken; otherwise answer in language code '""" + DEFAULT_LANG + """'.
- Use that language's native script. """ + STYLE_RULES


# Short fixed messages (shown before we know the user's language)
MSG = {
    "retry": "Please try again in a little while 🙏 (Konjam neram kalichu muyarchikavum pa)",
    "too_big": "File is too big, max 10 MB (File romba perusu pa)",
    "empty": "There is no text in this file (File la text illa pa)",
    "type": "Please send text, voice, image, PDF or Word files "
            "(Text, voice, image, PDF, Word file mattum anuppunga pa)",
    "pdf_fail": "Sorry, the PDF could not be created. Please try again later.",
}


class UnsupportedFile(Exception):
    pass


# ---------- FONTS (optional: nicer Tamil font; other scripts use PyMuPDF's built-in fonts) ----------
FONT_FILES = {
    "NotoSansTamil-Regular.ttf":
        "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/"
        "fonts/NotoSansTamil/hinted/ttf/NotoSansTamil-Regular.ttf",
    "NotoSans-Regular.ttf":
        "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/"
        "fonts/NotoSans/hinted/ttf/NotoSans-Regular.ttf",
}
FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true")


def _font_ok(p):
    try:
        return p.stat().st_size > 10_000 and p.read_bytes()[:4] in FONT_MAGIC
    except OSError:
        return False


def ensure_fonts():
    for name, url in FONT_FILES.items():
        p = Path(FONT_DIR) / name
        if _font_ok(p):
            continue
        try:
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            if r.content[:4] not in FONT_MAGIC:
                raise ValueError("not a font file")
            p.write_bytes(r.content)
            log.info("Downloaded font %s", name)
        except Exception as e:
            log.warning("Could not download %s: %s (built-in font will be used)", name, e)


ensure_fonts()


def fonts_ready():
    return all(_font_ok(Path(FONT_DIR) / n) for n in FONT_FILES)


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


# ---------- TEXT / LANGUAGE HELPERS ----------
def clean_text(text):
    if not text:
        return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*`#]+", "", text)
    return text.strip()[:12000]


# Script -> language code, used only when Gemini did not tell us the language
_SCRIPT_LANG = [
    ("TAMIL", "ta"), ("DEVANAGARI", "hi"), ("TELUGU", "te"), ("MALAYALAM", "ml"),
    ("KANNADA", "kn"), ("BENGALI", "bn"), ("GUJARATI", "gu"), ("GURMUKHI", "pa"),
    ("ARABIC", "ar"), ("HEBREW", "iw"), ("CYRILLIC", "ru"), ("GREEK", "el"),
    ("THAI", "th"), ("HANGUL", "ko"), ("HIRAGANA", "ja"), ("KATAKANA", "ja"),
    ("CJK", "zh-CN"), ("SINHALA", "si"), ("MYANMAR", "my"), ("KHMER", "km"),
]


def _script_of(ch):
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return None
    for script, _ in _SCRIPT_LANG:
        if name.startswith(script):
            return script
    return "LATIN" if name.startswith("LATIN") else None


def guess_lang(text):
    counts = {}
    for ch in text or "":
        if ch.isalpha():
            s = _script_of(ch)
            if s:
                counts[s] = counts.get(s, 0) + 1
    non_latin = {k: v for k, v in counts.items() if k != "LATIN"}
    if non_latin:
        top = max(non_latin, key=non_latin.get)
        return dict(_SCRIPT_LANG)[top]
    return "en" if counts else DEFAULT_LANG


def is_latin_text(text):
    """True if the text has no letters from a non-Latin script (e.g. German, French)."""
    return not any(c.isalpha() and _script_of(c) not in (None, "LATIN") for c in text)


RTL_LANGS = {"ar", "fa", "ur", "he", "iw", "ps", "sd", "yi", "ug", "dv", "ckb"}


def base_lang(code):
    return (code or "").strip().lower().replace("_", "-").split("-")[0]


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


def parse_json(raw):
    raw = (raw or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON in reply")
    return json.loads(raw[start:end + 1])


def ask_with_lang(contents, system):
    """Return (data dict, language code). Falls back to a plain reply if JSON fails."""
    try:
        data = parse_json(ask_gemini(contents, system=system, clean=False, json_mode=True))
        lang = (data.get("language") or "").strip()
        if data.get("kind") != "document":
            data["reply"] = clean_text(data.get("reply"))
            if not data["reply"]:
                raise ValueError("empty reply")
            lang = lang or guess_lang(data["reply"])
        return data, (lang or DEFAULT_LANG)
    except Exception:
        log.exception("JSON reply failed - using plain reply")
        reply = ask_gemini(contents)
        return {"kind": "chat", "reply": reply}, guess_lang(reply)


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


# ---------- VOICE (any gTTS language; English words in non-Latin text use the English voice) ----------
EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿️]+")

# Runs of English words (numbers may join a run, but a run must contain a letter)
EN_RUN = re.compile(
    r"(?=[A-Za-z0-9.'’\- \t]*[A-Za-z])"
    r"[A-Za-z0-9](?:[A-Za-z0-9'’\-]|\.(?=\d))*"
    r"(?:[ \t]+[A-Za-z0-9](?:[A-Za-z0-9'’\-]|\.(?=\d))*)*"
)

# Spoken words for symbols left after math conversion; other languages drop them
SYMBOL_WORDS = {
    "ta": {"=": " சமம் ", "+": " கூட்டல் ", "×": " பெருக்கல் ", "÷": " வகுத்தல் ",
           "%": " சதவீதம் ", "√": " வர்க்கமூலம் ", "π": " பை ", "²": " வர்க்கம் ",
           "³": " கனம் ", "&": " மற்றும் "},
    "en": {"=": " equals ", "+": " plus ", "×": " times ", "÷": " divided by ",
           "%": " percent ", "√": " square root of ", "π": " pi ", "²": " squared ",
           "³": " cubed ", "&": " and "},
}
CURRENCY_WORD = {"ta": "ரூபாய்", "en": "rupees", "hi": "रुपये"}

# ---- math -> spoken English ----
_MATH_TOKEN = r"[A-Za-zπ√0-9()²³^]+(?:\.[0-9]+)?"
_MATH_RE = re.compile(rf"{_MATH_TOKEN}(?:[ \t]*[=+×÷*/\-][ \t]*{_MATH_TOKEN})*")
_MATH_STRONG = re.compile(r"[=+×÷*^²³√π]")
_KEEP_WORDS = {"sin", "cos", "tan", "log", "ln", "sec", "cot", "cosec", "max", "min"}


def _letters(m):
    w = m.group()
    if w.lower() in _KEEP_WORDS or len(w) > 3:
        return w
    return " ".join(w)          # "lb" -> "l b"


def _math_to_english(expr):
    e = re.sub(r"(?<=\d)(?=[A-Za-z])|(?<=[A-Za-z])(?=\d)", " ", expr)
    e = re.sub(r"[A-Za-z]+", _letters, e)
    e = re.sub(r"\^\s*2(?!\d)|²", " squared ", e)
    e = re.sub(r"\^\s*3(?!\d)|³", " cubed ", e)
    e = re.sub(r"\^\(?([A-Za-z0-9.]+)\)?", r" to the power \1 ", e)
    for sym, word in {
        "√": " square root of ", "π": " pi ", "=": " equals ",
        "+": " plus ", "-": " minus ", "×": " times ", "*": " times ",
        "÷": " divided by ", "/": " divided by ",
        "(": " ", ")": " ",
    }.items():
        e = e.replace(sym, word)
    return re.sub(r"\s+", " ", e).strip()


def math_to_english(text):
    def repl(m):
        s = m.group()
        return f" {_math_to_english(s)} " if _MATH_STRONG.search(s) else s
    return _MATH_RE.sub(repl, text)


def speech_text(text, lang):
    """Turn a chat reply into text a TTS engine can read cleanly."""
    b = base_lang(lang)
    t = clean_text(text)
    t = re.sub(r"https?://\S+|www\.\S+", " ", t)                 # links are not read
    t = EMOJI_RE.sub(" ", t)                                     # emojis are not read
    t = re.sub(r"(?<=\d),(?=\d{3}\b)", "", t)                    # 1,000 -> 1000
    if b in CURRENCY_WORD:
        t = re.sub(r"₹\s*(\d+(?:\.\d+)?)", rf"\1 {CURRENCY_WORD[b]}", t)
    t = math_to_english(t)                                       # formulas -> spoken English
    for sym, word in SYMBOL_WORDS.get(b, {}).items():
        t = t.replace(sym, word)
    t = re.sub(r"^\s*[-•▪●]+\s*", "", t, flags=re.M)             # bullets
    t = re.sub(r"[_~|<>\[\]{}\\^/=+×÷√²³]", " ", t)
    t = re.sub(r"\s*\n+\s*", ". ", t)                            # line breaks -> pause
    t = re.sub(r"([.!?।。])(\s*\.)+", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _trim_sentence(text, limit):
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = max(cut.rfind(c) for c in ".!?।。")
    return cut[:idx + 1] if idx > limit * 0.5 else cut


_TTS_LANGS = None


def tts_code(lang):
    """Map a language code to one gTTS supports, or None."""
    global _TTS_LANGS
    if _TTS_LANGS is None:
        try:
            _TTS_LANGS = {k.lower(): k for k in tts_langs()}
        except Exception:
            log.exception("could not load gTTS language list")
            _TTS_LANGS = {}
    code = (lang or "").strip().lower().replace("_", "-")
    aliases = {"zh": "zh-cn", "he": "iw", "jw": "jw", "fil": "tl"}
    for c in (code, aliases.get(code), base_lang(code), aliases.get(base_lang(code))):
        if c and c in _TTS_LANGS:
            return _TTS_LANGS[c]
    return None


def _tts_bytes(text, lang):
    buf = BytesIO()
    gTTS(text=text, lang=lang, slow=False).write_to_fp(buf)
    return buf.getvalue()


def _split_segments(spoken, main_lang):
    if is_latin_text(spoken):          # German, French, English ... -> one voice
        return [[main_lang, spoken]]
    segs, pos = [], 0
    for m in EN_RUN.finditer(spoken):
        if m.start() > pos:
            segs.append([main_lang, spoken[pos:m.start()]])
        segs.append(["en", m.group()])
        pos = m.end()
    if pos < len(spoken):
        segs.append([main_lang, spoken[pos:]])
    segs = [[l, s.strip()] for l, s in segs if re.search(r"\w", s)]
    merged = []
    for l, s in segs:
        if merged and merged[-1][0] == l:
            merged[-1][1] += " " + s
        else:
            merged.append([l, s])
    return merged


def synthesize_speech(text, lang):
    """Return MP3 bytes, or b"" if the language has no voice."""
    main = tts_code(lang)
    if not main:
        log.info("No voice available for language %r - text reply only", lang)
        return b""
    spoken = _trim_sentence(speech_text(text, lang), MAX_VOICE_CHARS)
    if not spoken:
        return b""
    segs = _split_segments(spoken, main)
    if not segs:
        return b""
    if len(segs) > MAX_VOICE_SEGMENTS:
        segs = [[main, spoken]]
    parts = []
    for seg_lang, seg in segs:
        try:
            parts.append(_tts_bytes(seg, seg_lang))
        except Exception:
            log.exception("tts failed for %s segment", seg_lang)
    return b"".join(parts)


def send_voice(to, text, lang):
    path = None
    try:
        audio = synthesize_speech(text, lang)
        if not audio:
            return
        fd, path = tempfile.mkstemp(suffix=".mp3")
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        send_audio(to, path)
        log.info("Voice sent (%s)", lang)
    except Exception:
        log.exception("voice failed")
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ---------- LETTER / PDF CREATION (any language) ----------
BLANK = "____________"
DEFAULT_LABELS = {"from": "From", "to": "To", "date": "Date", "subject": "Subject"}
TAMIL_LABELS = {"from": "அனுப்புநர்", "to": "பெறுநர்", "date": "நாள்", "subject": "பொருள்"}

# Tamil with the downloaded Noto Sans fonts (ASCII runs use the Latin font)
TAMIL_CSS = """
@font-face{font-family:NotoTamil;src:url('NotoSansTamil-Regular.ttf')}
@font-face{font-family:NotoLatin;src:url('NotoSans-Regular.ttf')}
body{font-family:NotoTamil;font-size:12pt;line-height:1.6}
span.en{font-family:NotoLatin}
p{margin:0 0 12pt 0}
p.b{text-align:justify;text-indent:30pt}
p.r{text-align:right}
"""
# Every other language: PyMuPDF picks a matching built-in Noto font per script.
# In right-to-left pages PyMuPDF mirrors the alignment, so p.r ends up on the left,
# which is where the closing goes in Arabic/Urdu letters.
BASE_CSS = (
    "body{font-size:12pt;line-height:1.6} p{margin:0 0 12pt 0} "
    "p.b{text-align:justify;text-indent:30pt} p.r{text-align:right}"
)

_ASCII_SPLIT = re.compile(r"([\x20-\x7E]+)")


def _fmt(s):
    """Escape, keep line breaks, and tag ASCII runs (used by the Tamil font CSS)."""
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
    lang = base_lang(d.get("language"))
    today = datetime.now(ZoneInfo(LOCAL_TZ)).strftime("%d/%m/%Y")
    labels = dict(TAMIL_LABELS if lang == "ta" else DEFAULT_LABELS)
    labels.update({k: v for k, v in (d.get("labels") or {}).items() if k in labels and v})
    lbl_from, lbl_to, lbl_date, lbl_sub = (
        _fmt(labels["from"]), _fmt(labels["to"]), _fmt(labels["date"]), _fmt(labels["subject"]))
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
    direction = "rtl" if lang in RTL_LANGS else "ltr"
    return f"""<html dir='{direction}'><body dir='{direction}'>
<p><b>{lbl_from}:</b><br>{sender}<br>{sender_addr}</p>
<p><b>{lbl_to}:</b><br>{to_block}</p>
<p class='r'><b>{lbl_date}:</b> {date}</p>
<p><b>{lbl_sub}:</b> {subject}</p>
<p>{salute}</p>
{paras}
<p class='r'>{closing}<br><br><br>{sign}</p>
</body></html>"""


def build_letter_pdf(data, path):
    html = build_letter_html(data)
    if base_lang(data.get("language")) == "ta" and fonts_ready():
        story = fitz.Story(html=html, user_css=TAMIL_CSS, archive=fitz.Archive(FONT_DIR))
    else:
        story = fitz.Story(html=html, user_css=BASE_CSS)

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


def send_letter(sender, data):
    fd, path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        build_letter_pdf(data, path)
        name = re.sub(r"[^A-Za-z0-9_-]+", "_", data.get("doc_type") or "letter")
        send_document(sender, path, f"{name.strip('_') or 'letter'}.pdf")
        log.info("Letter PDF sent to %s", sender)
    except Exception:
        log.exception("Failed to build/send PDF")
        safe_send_text(sender, MSG["pdf_fail"])
        return
    finally:
        if os.path.exists(path):
            os.remove(path)
    send_text(sender, data.get("ready_message") or "Your letter is ready. Fill in the ____ blanks.")


# ---------- MEDIA HANDLING ----------
def handle_media(data, mime, fname, caption):
    """Return (reply, language)."""
    mime = (mime or "").lower()
    name = (fname or "").lower()
    if mime.startswith("audio/"):
        clean_mime = mime.split(";")[0].strip()
        part = types.Part.from_bytes(data=data, mime_type=clean_mime)
        contents = [caption or "Listen to this voice message and answer it.", part]
    elif mime.startswith("image/"):
        img = Image.open(BytesIO(data)).convert("RGB")
        contents = [caption or "Explain this image.", img]
    elif mime == "application/pdf" or name.endswith(".pdf"):
        with fitz.open(stream=data, filetype="pdf") as doc:
            text = "\n".join(p.get_text() for p in doc)
            if len(text.strip()) < 80:
                imgs = []
                for i, p in enumerate(doc):
                    if i >= MAX_PDF_IMAGE_PAGES:
                        break
                    png = p.get_pixmap(dpi=120).tobytes("png")
                    imgs.append(Image.open(BytesIO(png)).convert("RGB"))
                contents = [caption or "Explain these PDF pages.", *imgs]
            else:
                contents = f"{caption or 'Explain this PDF text'}:\n{text[:12000]}"
    elif "wordprocessingml" in mime or name.endswith(".docx"):
        doc = Document(BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs).strip()
        if not text:
            raise UnsupportedFile("empty")
        contents = f"{caption or 'Explain this document'}:\n{text[:12000]}"
    elif mime.startswith("text/") or name.endswith(".txt"):
        text = data.decode("utf-8", errors="ignore").strip()
        if not text:
            raise UnsupportedFile("empty")
        contents = f"{caption or 'Explain this text'}:\n{text[:12000]}"
    else:
        raise UnsupportedFile("type")
    if not caption:
        # tell Gemini there was no caption, so it uses the voice language / DEFAULT_LANG
        note = "(The user sent no caption.)"
        contents = [note, *contents] if isinstance(contents, list) else f"{note}\n{contents}"
    result, lang = ask_with_lang(contents, MEDIA_PROMPT)
    return result["reply"], lang


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
            result, lang = ask_with_lang(TEXT_PROMPT + user_text, "Reply with JSON only.")
            if result.get("kind") == "document":
                result["language"] = lang
                send_letter(sender, result)
                return
            reply = result["reply"]
        elif kind in ("image", "document", "audio", "voice"):
            info = message.get(kind, {})
            data, mime = download_media(info.get("id"))
            reply, lang = handle_media(
                data, mime, info.get("filename", ""), (info.get("caption") or "").strip()
            )
        else:
            safe_send_text(sender, MSG["type"])
            return
        send_text(sender, reply)
        send_voice(sender, reply, lang)
    except UnsupportedFile as e:
        safe_send_text(sender, MSG.get(str(e), MSG["type"]))
    except Exception:
        log.exception("failed")
        safe_send_text(sender, MSG["retry"])


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


@app.get("/fonts")
def fonts_status():
    return {"font_dir": FONT_DIR,
            "files": {f.name: f.stat().st_size for f in Path(FONT_DIR).glob("*")},
            "tamil_fonts_ready": fonts_ready()}


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
