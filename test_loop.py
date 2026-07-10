import os
import time
from dotenv import load_dotenv
from google import genai
from google.genai import errors

load_dotenv(override=True)
api_key = os.getenv("GOOGLE_API_KEY")
model_name = os.getenv("GEMINI_MODEL")

print("Using key starts with:", api_key[:12])
print("Model:", model_name)

client = genai.Client(api_key=api_key)

for i in range(1, 6):
    print(f"Request {i}...")
    try:
        response = client.models.generate_content(
            model=model_name,
            contents="Merhaba"
        )
        print(f"Success {i}: {response.text[:30]}")
    except errors.APIError as e:
        print(f"APIError {i}: code={e.code}, msg={e.message}")
        break
    except Exception as e:
        print(f"Error {i}: {str(e)}")
        break
    time.sleep(1)
