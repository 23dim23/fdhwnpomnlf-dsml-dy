import os
import asyncio
import sqlite3
import re
import html
import requests
import traceback
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

# ==================== НАСТРОЙКИ ====================
API_ID = int(os.getenv('API_ID', 33481567))
API_HASH = os.getenv('API_HASH', "93d073404049ef77e94be613d29fb57d")
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения!")

TARGET_GROUP_ID = -1003455134116
PROBLEM_CHAT_ID = -1001156193082
REPORT_TOPIC_ID = None
DB_FILE = 'parser_data.db'

# ==================== ЖЁСТКИЙ СПИСОК ОСОБЫХ ЧАТОВ ====================
HARDCODED_SPECIAL_CHATS = [
    -1001156193082,
]
# ================================================================

# ==================== КЛИЕНТ ====================
client = TelegramClient("telethon_worker", API_ID, API_HASH)

# ==================== ФУНКЦИИ РАБОТЫ С БД ====================
def get_special_chats():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM special_chats")
        special_chats = [int(row[0]) for row in cursor.fetchall()]
        conn.close()
        for chat_id in HARDCODED_SPECIAL_CHATS:
            if chat_id not in special_chats:
                special_chats.append(chat_id)
        return special_chats
    except Exception as e:
        print(f"❌ Ошибка чтения особых чатов: {e}")
        return HARDCODED_SPECIAL_CHATS

def is_chat_special(chat_id):
    return chat_id in get_special_chats()

def get_data_from_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT url FROM channels")
        channels = [str(row[0]).strip().lower() for row in cursor.fetchall()]
        cursor.execute("SELECT word FROM keywords")
        keywords = [str(row[0]).lower().strip() for row in cursor.fetchall() if row[0]]
        cursor.execute("SELECT word FROM stop_words")
        stop_words = [str(row[0]).lower().strip() for row in cursor.fetchall() if row[0]]
        conn.close()
        return channels, keywords, stop_words
    except Exception as e:
        print(f"❌ Ошибка чтения БД: {e}")
        return [], [], []

# ==================== ФИЛЬТРЫ ТЕКСТА ====================

def get_filters(only_active=True):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        if only_active:
            cursor.execute("SELECT id, pattern, is_regex, is_active FROM text_filters WHERE is_active = 1")
        else:
            cursor.execute("SELECT id, pattern, is_regex, is_active FROM text_filters")
        filters = cursor.fetchall()
        conn.close()
        return [{"id": f[0], "pattern": f[1], "is_regex": f[2], "is_active": f[3]} for f in filters]
    except Exception as e:
        print(f"❌ Ошибка получения фильтров: {e}")
        return []

def apply_filters(text):
    if not text:
        return text
    filters = get_filters(only_active=True)
    if not filters:
        return text
    for f in filters:
        try:
            if f["is_regex"]:
                text = re.sub(f["pattern"], '', text, flags=re.IGNORECASE | re.DOTALL)
            else:
                text = text.replace(f["pattern"], '')
        except Exception as e:
            print(f"⚠️ Ошибка применения фильтра {f['id']}: {e}")
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' +\n', '\n', text)
    text = text.strip()
    return text

# ==================== КОНВЕРТАЦИЯ MARKDOWN → HTML ====================

def markdown_to_html(text):
    """Конвертирует Markdown в HTML для Telegram"""
    if not text:
        return text
    
    # Экранируем HTML-спецсимволы
    text = html.escape(text)
    
    # Жирный текст: **текст** → <b>текст</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    
    # Курсив: *текст* → <i>текст</i>
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    
    # Подчёркивание: __текст__ → <u>текст</u>
    text = re.sub(r'__(.+?)__', r'<u>\1</u>', text)
    
    # Зачёркивание: ~~текст~~ → <s>текст</s>
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    
    # Моноширинный: `текст` → <code>текст</code>
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    
    # Ссылки: [текст](url) → <a href="url">текст</a>
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    
    return text

# ==================== КОНЕЦ КОНВЕРТАЦИИ ====================

# ==================== ОТПРАВКА В МОДЕРАЦИЮ ====================
def send_to_moderation(chat_id, thread_id, text, author_id=None, message_link=None, is_special=False):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    
    if is_special:
        keyboard = [[{"text": "⏳ Загрузка...", "callback_data": "loading"}]]
    else:
        keyboard = [
            [{"text": "✅ Опубликовать", "callback_data": "pub_approve"},
             {"text": "❌ Отклонить", "callback_data": "pub_decline"}]
        ]
        if message_link:
            keyboard.append([{"text": "🔗 Источник", "url": message_link}])
    
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {"inline_keyboard": keyboard}
    }
    if thread_id:
        payload["message_thread_id"] = thread_id
    
    try:
        r = requests.post(url, json=payload, timeout=10)
        res = r.json()
        
        if res.get("ok") and is_special:
            msg_id = res.get("result", {}).get("message_id")
            if msg_id:
                update_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup"
                buttons = []
                if message_link:
                    buttons.append({"text": "🔗 Источник", "url": message_link})
                buttons.append({"text": "✏️ Ввести контакт", "callback_data": f"edit_contact_{msg_id}"})
                buttons.append({"text": "✅ Опубликовать", "callback_data": f"pub_approve_special_{msg_id}"})
                buttons.append({"text": "❌ Отклонить", "callback_data": f"pub_decline_special_{msg_id}"})
                keyboard_rows = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
                update_payload = {
                    "chat_id": chat_id,
                    "message_id": msg_id,
                    "reply_markup": {"inline_keyboard": keyboard_rows}
                }
                requests.post(update_url, json=update_payload, timeout=10)
                print(f"✅ Кнопки для особого чата обновлены (message_id: {msg_id})")
        
        if not res.get("ok"):
            print(f"❌ Ошибка Telegram API: {res.get('description')}")
    except Exception as e:
        print(f"❌ Ошибка при отправке модератору: {e}")

# ==================== ОБРАБОТКА СООБЩЕНИЯ ====================
async def process_message(message):
    try:
        # ========== СБОР ТЕКСТА С ПРИОРИТЕТОМ НА ЦИТАТУ ==========
        content = ""
        
        if message._reply_message:
            content = message._reply_message.text or message._reply_message.caption or ""
            print(f"📝 Текст взят из _reply_message: {content[:50]}...")
        
        if not content:
            content = message.text or message.caption or ""
            print(f"📝 Текст взят из самого сообщения: {content[:50]}...")
        
        if not content:
            return
        # =======================================================

        # ========== ПРИМЕНЯЕМ ФИЛЬТРЫ ==========
        content = apply_filters(content)
        # ======================================
        
        channels_db, keywords, stop_words = get_data_from_db()
        msg_text_lower = content.lower()
        
        is_target = False
        cid_full = str(PROBLEM_CHAT_ID)
        cid_short = cid_full.replace("-100", "")
        
        for conf in channels_db:
            clean_conf = conf.replace('https://t.me/', '').replace('http://t.me/', '').replace('@', '').strip()
            if (clean_conf == cid_full) or (clean_conf == cid_short):
                is_target = True
                break
        
        if not is_target:
            return
        
        for stop_word in stop_words:
            if stop_word and stop_word in msg_text_lower:
                print(f"🚫 Пропущено (стоп-слово): '{stop_word}'")
                return
        
        found_keyword = False
        for word in keywords:
            if not word: continue
            pattern = rf"(?:^|[^а-яёa-z0-9]){re.escape(word)}(?:$|[^а-яёa-z0-9])"
            try:
                if re.search(pattern, msg_text_lower):
                    found_keyword = True
                    break
            except:
                if word in msg_text_lower:
                    found_keyword = True
                    break
        
        if not found_keyword:
            return
        
        is_special = is_chat_special(PROBLEM_CHAT_ID)
        print(f"🔧 Чат {PROBLEM_CHAT_ID} особый: {'✅ ДА' if is_special else '❌ НЕТ'}")
        
        sender = await message.get_sender()
        contact = "Не указан"
        author_id = None
        has_username = False
        
        if sender:
            if sender.username:
                contact = f"@{sender.username}"
                author_id = sender.id
                has_username = True
                print(f"📱 Найден username: @{sender.username}")
            elif sender.first_name:
                contact = html.escape(sender.first_name)
                author_id = sender.id
                print(f"📱 Нет username, найдено имя: {contact}")
            else:
                contact = "Автор"
                author_id = sender.id
        
        if is_special and not has_username:
            contact = "<i>⚠️ Контакт не найден (укажите @username или добавьте контакт в текст)</i>"
        
        # ========== КОНВЕРТИРУЕМ MARKDOWN → HTML ==========
        content = markdown_to_html(content)
        # =================================================
        
        header = "<b>| 🇻 🇦 🇨 🇦 🇳 🇨 🇾 |</b>\n\n"
        body_html = content  # уже в HTML
        
        if is_special and not has_username:
            contact_block = f"<b>Контакт для связи:</b>\n{contact}\n\n"
        elif not has_username:
            contact_block = f"<b>Контакт для связи:</b>\n<i>⚠️ Контакт не найден (укажите @username или добавьте контакт в текст)</i>\n\n"
        else:
            contact_block = f"<b>Контакт для связи:</b>\n{contact}\n\n"
        
        instruction = (
            "<b><code>Для публикации вакансии\\резюме, напишите заявку в </code>"
            "<a href='https://t.me/Vakansii_GetJob_bot'>бота</a></b>"
        )
        tags = "\n\n#вакансия #парсер"
        pretty_text = f"{header}{body_html}\n\n{contact_block}{instruction}{tags}"
        
        message_link = None
        if str(PROBLEM_CHAT_ID).startswith("-100"):
            chat_id_for_link = str(PROBLEM_CHAT_ID)[4:]
            message_link = f"https://t.me/c/{chat_id_for_link}/{message.id}"
        
        send_to_moderation(TARGET_GROUP_ID, REPORT_TOPIC_ID, pretty_text, author_id, message_link, is_special=is_special)
        print(f"✅ Отправлено в модерацию: {message.id} (особый режим: {is_special})")
        
    except Exception as e:
        print(f"❌ Ошибка в process_message: {e}")
        traceback.print_exc()

# ==================== ПОЛЛИНГ ====================
async def main():
    print("🚀 Запуск Telethon-воркера для проблемного чата...")
    
    try:
        await client.start()
        print("✅ Клиент Telethon запущен")
    except Exception as e:
        print(f"❌ Ошибка запуска клиента: {e}")
        traceback.print_exc()
        return
    
    try:
        entity = await client.get_entity(PROBLEM_CHAT_ID)
        print(f"✅ Доступ к чату {PROBLEM_CHAT_ID} получен")
    except Exception as e:
        print(f"❌ Нет доступа к чату {PROBLEM_CHAT_ID}: {e}")
        traceback.print_exc()
        return
    
    last_id = 0
    
    while True:
        try:
            messages = await client.get_messages(entity, limit=10, min_id=last_id)
            
            for msg in reversed(messages):
                if msg.id > last_id:
                    await process_message(msg)
                    last_id = max(last_id, msg.id)
            
            await asyncio.sleep(30)
            
        except Exception as e:
            print(f"⚠️ Ошибка в цикле polling: {e}")
            traceback.print_exc()
            await asyncio.sleep(30)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Telethon-воркер остановлен")
    except Exception as e:
        print(f"💥 Критическая ошибка: {e}")
        traceback.print_exc()