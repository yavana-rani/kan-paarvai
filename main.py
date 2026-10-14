from fastapi import FastAPI, Request
import os

app = FastAPI()

@app.get("/")
def home():
    return {"status": "Kan Paarvai is Live!"}

@app.get("/webhook")
def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    if mode == "subscribe" and token == os.getenv("VERIFY_TOKEN", "kanpaarvai123"):
        return int(challenge)
    return "Verification failed"

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    print(data)
    return {"ok": True}
