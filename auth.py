import os
from pyrogram import Client
from dotenv import load_dotenv

load_dotenv()

# --- НАСТРОЙКИ ---
api_id = int(os.getenv('API_ID', 33481567))
api_hash = os.getenv('API_HASH', "93d073404049ef77e94be613d29fb57d")

app = Client("my_account", api_id=api_id, api_hash=api_hash)

if __name__ == "__main__":
    print("🚀 Запускаю авторизацию...")
    print(f"📌 API_ID: {api_id}")
    print(f"📌 API_HASH: {api_hash[:10]}... (скрыто)")
    app.run()