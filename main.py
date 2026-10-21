from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
import google.generativeai as genai
import os

app = FastAPI()

# Setup
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "kanpaarvai123")

KAN_PROMPT = "Nee Kan Paarvai. Tamil la 2-4 varila short-a sollu. Maths iruntha x square, veru nu pesara maathiri sollu."

@app.get("/")
def home():
    return {"status": "Live pa"}

# 1. Meta Verification ku - GET /webhook
@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    return PlainTextResponse("Failed", status_code=403)

# 2. Message varum pothu - POST /webhook - ITHU THAAN MUKKIYAM
@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()
    print(f"Message vanthuchu: {data}")
    # inga unga WhatsApp reply logic varum
    return {"status": "ok"}

# 405 error poganum na - HEAD ku
@app.head("/")
def head_check():
    return {"ok": True}
