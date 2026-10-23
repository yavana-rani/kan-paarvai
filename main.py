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

# ---------- CONFIG ----------
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
GRAPH_VERSION = os.getenv("GRAPH_API_VERSION", "v22.0")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
MAX_MEDIA_BYTES = 10 * 1024 * 1024

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("kan-paarvai")
app = FastAPI(title="Kan Paarvai AI")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"

SYSTEM_PROMPT = "Neenga Kan Paarvai AI. Eppothum eliya Tamil la badhilalikkanum."

processed_ids = set()

def clean_text(text):
    if not text: return ""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[*_`#]{1,}", "", text)
    return text.strip()[:12000]

def clean_voice_text(text):
    text = clean_text(text)
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()

# ---------- GEMINI WITH FALLBACK PA ----------
def ask_gemini(contents):
    if not client:
        raise RuntimeError("GEMINI_API_KEY illa pa")
    
    models_to_try = [GEMINI_MODEL, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-flash-8b"]
    seen = set()
    unique = []
    for m in models_to_try:
        if m and m not in seen:
            seen.add(m)
            unique.append(m)

    last_err = None
    for model_name in unique:
        try:
            log.info(f"Trying {model_name}")
            resp = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.2,
                    max_output_tokens=4096,
                ),
            )
            if resp.text:
                return clean_text(resp.text)
        except Exception as e:
            last_err = e
            log.warning(f"{model_name} failed: {e}")
            continue
    raise RuntimeError(f"Gemini quota: {last_err}")

# ---------- WHATSAPP API ----------
def auth_headers():
    return {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

def graph_post(endpoint, payload):
    r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/{endpoint}", headers={**auth_headers(), "Content-Type": "application/json"}, json=payload, timeout=40)
    r.raise_for_status()
    return r.json()

def send_text(to, text):
    graph_post("messages", {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": clean_text(text)[:3900]}})

def upload_media(path, mime):
    with open(path, "rb") as f:
        r = requests.post(f"{GRAPH}/{PHONE_NUMBER_ID}/media", headers=auth_headers(), data={"messaging_product": "whatsapp", "type": mime}, files={"file": (Path(path).name, f, mime)}, timeout=60)
    r.raise_for_status()
    return r.json().get("id")

def send_audio(to, path):
    mid = upload_media(path, "audio/mpeg")
    graph_post("messages", {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": mid}})

def download_media(media_id):
    r = requests.get(f"{GRAPH}/{media_id}", headers=auth_headers(), timeout=30)
    r.raise_for_status()
    meta = r.json()
    url = meta.get("url")
    with requests.get(url, headers=auth_headers(), stream=True, timeout=60) as d:
        data = d.content
    return data, meta.get("mime_type", ""), meta.get("file_name", "file")

def send_voice(to, text):
    spoken = clean_voice_text(text)
    if not spoken: return
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as t:
            path = t.name
        gTTS(text=spoken[:600], lang="ta", slow=False).save(path)
        send_audio(to, path)
    except Exception:
        log.exception("voice failed")
    finally:
        if os.path.exists(path): os.remove(path)

# ---------- PROCESS ----------
def process_message(sender, message, message_id):
    if message_id in processed_ids: return
    processed_ids.add(message_id)
    kind = message.get("type", "")
    user_text = message.get("text", {}).get("body", "") if kind=="text" else message.get(kind, {}).get("caption","")

    try:
        if kind == "text":
            reply = ask_gemini(user_text)
            send_text(sender, reply)
            send_voice(sender, reply)
            return
        if kind in ["image", "document"]:
            media_id = message.get(kind, {}).get("id")
            data, mime, fname = download_media(media_id)
            if kind=="image" or mime.startswith("image"):
                img = Image.open(BytesIO(data)).convert("RGB")
                reply = ask_gemini(["Intha padatha Tamil la vilakku", img])
            else:
                # PDF logic
                with fitz.open(stream=data, filetype="pdf") as doc:
                    text = "\n".join([p.get_text() for p in doc])
                    if len(text.strip()) < 80:
                        imgs = [Image.open(BytesIO(p.get_pixmap(dpi=120).tobytes("png"))).convert("RGB") for p in doc]
                        reply = ask_gemini(["PDF pages Tamil la vilakku", *imgs])
                    else:
                        reply = ask_gemini(f"PDF text Tamil la vilakku:\n{text[:12000]}")
            send_text(sender, reply)
            send_voice(sender, reply)
    except Exception:
        log.exception("failed")
        send_text(sender, "Konjam neram kalichu muyarchikavum pa")

@app.get("/")
def home(): return {"status": "Kan Paarvai AI running pa"}

@app.get("/webhook")
def verify(request: Request):
    if request.query_params.get("hub.verify_token") == VERIFY_TOKEN:
        return PlainTextResponse(request.query_params.get("hub.challenge", ""))
    return PlainTextResponse("fail", status_code=403)

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    data = await request.json()
    for entry in data.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("from"):
                    background_tasks.add_task(process_message, msg.get("from"), msg, msg.get("id",""))
    return {"ok": True}
