from pyrogram import Client

api_id = 33481567  # Твой ID
api_hash = "93d073404049ef77e94be613d29fb57d"

app = Client("my_account", api_id=api_id, api_hash=api_hash)

if __name__ == "__main__":
    print("Запускаю авторизацию...")
    app.run()
