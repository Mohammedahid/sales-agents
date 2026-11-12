"""Quick Gemini connectivity test. Run: test_gemini.bat"""
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
key = os.getenv("GEMINI_API_KEY", "").strip()
print("Key loaded:", f"yes ({len(key)} chars, starts {key[:4]}...)" if key else "NO - check .env")
if not key:
    sys.exit()

BASE = "https://generativelanguage.googleapis.com/v1beta"
for model in [os.getenv("GEMINI_MODEL", "gemini-flash-latest"), "gemini-flash-lite-latest", "gemini-2.5-flash"]:
    for kind in ("native", "openai"):
        t = time.time()
        try:
            if kind == "native":
                r = httpx.post(f"{BASE}/models/{model}:generateContent",
                               headers={"x-goog-api-key": key},
                               json={"contents": [{"parts": [{"text": "قل مرحبا بكلمة وحدة"}]}]},
                               timeout=httpx.Timeout(45, connect=15))
            else:
                r = httpx.post(f"{BASE}/openai/chat/completions",
                               headers={"Authorization": f"Bearer {key}"},
                               json={"model": model, "reasoning_effort": "low",
                                     "messages": [{"role": "user", "content": "قل مرحبا بكلمة وحدة"}]},
                               timeout=httpx.Timeout(45, connect=15))
            print(f"{model:28} {kind:7} {r.status_code} {time.time()-t:5.1f}s  {r.text[:160]!r}")
        except Exception as e:
            print(f"{model:28} {kind:7} FAIL {time.time()-t:5.1f}s  {type(e).__name__} {e}")
