import os
import json
import re

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash")


def _client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key or api_key.startswith("YOUR_") or api_key.startswith("your_"):
        raise RuntimeError("GEMINI_API_KEY is not configured. Add a real Gemini API key to .env and restart Flask.")
    return genai.Client(api_key=api_key)


def _extract_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match:
            return json.loads(match.group(0))
        raise ValueError("Gemini returned an invalid JSON response.")


def analyze_problem(service, description, image_path=None):
    prompt = f"""
You are Smart Serve's preliminary service-diagnosis assistant.
Selected service: {service}
Customer problem: {description}

Return ONLY one valid JSON object with exactly these fields:
problem, possible_cause, difficulty, recommended_service, estimated_price, safety_note

Rules:
- difficulty must be exactly Easy, Medium, or Hard.
- estimated_price must be an INR range such as ₹300-₹800.
- Do not invent certainty; this is a preliminary assessment.
- Keep safety_note short and empty when no warning is needed.
"""

    contents = [prompt]
    if image_path:
        with open(image_path, "rb") as image_file:
            image_bytes = image_file.read()
        ext = os.path.splitext(image_path)[1].lower()
        mime = {".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".png":"image/png", ".webp":"image/webp"}.get(ext, "image/jpeg")
        contents.append(types.Part.from_bytes(data=image_bytes, mime_type=mime))

    client = _client()
    last_error = None
    models = list(dict.fromkeys([m for m in [DEFAULT_MODEL, FALLBACK_MODEL] if m]))
    for model in models:
        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    temperature=0.2,
                    response_mime_type="application/json",
                ),
            )
            result = _extract_json(response.text)
            required = ["problem", "possible_cause", "difficulty", "recommended_service", "estimated_price", "safety_note"]
            for field in required:
                result.setdefault(field, "")
            if result["difficulty"] not in {"Easy", "Medium", "Hard"}:
                result["difficulty"] = "Medium"
            return result
        except Exception as exc:
            last_error = exc
            # A model-name/availability error can use the fallback; auth/quota errors will
            # simply be reported clearly rather than hiding the real problem.
            msg = str(exc).lower()
            if model != models[-1] and any(x in msg for x in ["not found", "404", "model", "unsupported"]):
                continue
            break
    raise RuntimeError(f"Gemini request failed: {last_error}")
