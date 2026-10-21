from fastapi import FastAPI, File, UploadFile
import google.generativeai as genai
import os
from PIL import Image
import io

app = FastAPI(title="Kan Paarvai")

# 1. Gemini Setup
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

# 2. Tamil + Maths Prompt - Short & Crisp
KAN_PROMPT = """
Nee Kan Paarvai, paarvaiyatra nanbarukku uthavum nanban.
Vithi:
1. Bathil eppothum Tamil la mattum, 2-4 varila short-a crisp-a sollu.
2. Maths formula iruntha pesara maathiri sollu: 
   x² = x square, √ = veru, / = by, = = equal to, + = plus
3. Normal photo na: yaar, enna dress, enna panrar mattum sollu.
4. English, periya vilakkam vendaam.
"""

@app.get("/")
def home():
    return {"message": "Kan Paarvai Live pa! 200 OK"}

@app.post("/describe")
async def describe_image(file: UploadFile = File(...)):
    try:
        image_data = await file.read()
        image = Image.open(io.BytesIO(image_data))
        
        response = model.generate_content([KAN_PROMPT, image])
        tamil_text = response.text
        
        # Short-a cut pannu
        if len(tamil_text) > 400:
            tamil_text = tamil_text[:400]
            
        return {"tamil_reply": tamil_text}
    except Exception as e:
        return {"error": str(e), "tamil_reply": "Photo puriyala pa, vera photo anupunga"}

# Local test ku
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
