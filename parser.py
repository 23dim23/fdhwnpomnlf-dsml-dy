import os
import sqlite3
import asyncio
import re
import requests
import html
import sys
from pyrogram import Client, enums
from dotenv import load_dotenv

load_dotenv()

sent_messages = set()

# --- НАСТРОЙКИ ---
API_ID = int(os.getenv('API_ID', 33481567))
API_HASH = os.getenv('API_HASH', "93d073404049ef77e94be613d29fb57d")
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения!")

TARGET_GROUP_ID = -1003455134116 
REPORT_TOPIC_ID = None
DB_FILE = 'parser_data.db'

# ==================== ЖЁСТКИЙ СПИСОК ОСОБЫХ ЧАТОВ ====================
HARDCODED_SPECIAL_CHATS = [
    -1001156193082,  # проблемный чат с ботом
]
# ================================================================

app = Client("my_account", api_id=API_ID, api_hash=API_HASH)

# ==================== ФИЛЬТРЫ ====================
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

def hard_remove_cliche(text):
    """Жёсткое удаление клеше в цитате (в обход фильтров)"""
    if not text:
        return text
    
    patterns = [
        r'🔻\s*Прочтите\s*правила\s*безопасности.*?(?=\n\n|$)',
        r'Прочтите\s*правила\s*безопасности.*?(?=\n\n|$)',
        r'правила\s*безопасности.*?(?=\n\n|$)',
    ]
    
    for pattern in patterns:
        try:
            text = re.sub(pattern, '', text, flags=re.IGNORECASE | re.DOTALL)
        except:
            pass
    
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' +\n', '\n', text)
    text = text.strip()
    
    return text

# ==================== ЧЁРНЫЙ СПИСОК ====================
def is_user_blocked(user_id):
    """Проверить, заблокирован ли пользователь или канал"""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (str(user_id),))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    except Exception as e:
        print(f"❌ Ошибка проверки ЧС: {e}")
        return False
# ==================== КОНЕЦ ЧЁРНОГО СПИСКА ====================
# ==================== КОНЕЦ ФИЛЬТРОВ ====================

def get_special_chats():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM special_chats")
        special_chats = [int(row[0]) for row in cursor.fetchall()]
        conn.close()
        
        # Добавляем жёстко заданные чаты
        for chat_id in HARDCODED_SPECIAL_CHATS:
            if chat_id not in special_chats:
                special_chats.append(chat_id)
        
        return special_chats
    except Exception as e:
        print(f"❌ Ошибка чтения особых чатов: {e}")
        return HARDCODED_SPECIAL_CHATS

async def get_contact_special_method(client, message):
    try:
        print(f"   ⭐ Применяем ОСОБЫЙ метод для чата {message.chat.id}")
        if message.from_user:
            if message.from_user.username:
                contact = f"@{message.from_user.username}"
                author_id = message.from_user.id
                print(f"   ✅ Особый метод: найден @{message.from_user.username}")
                return contact, author_id
            elif message.from_user.id:
                if message.from_user.first_name:
                    name = html.escape(message.from_user.first_name)
                    contact = name
                    author_id = message.from_user.id
                    print(f"   ✅ Особый метод: найдено имя {name} (ID: {author_id})")
                    return contact, author_id
                else:
                    contact = "Автор"
                    author_id = message.from_user.id
                    print(f"   ✅ Особый метод: найден автор с ID {author_id}")
                    return contact, author_id
        if message.reply_to_message and message.reply_to_message.from_user:
            reply_user = message.reply_to_message.from_user
            if reply_user.username:
                print(f"   ✅ Особый метод (reply): @{reply_user.username}")
                return f"@{reply_user.username}", reply_user.id
            elif reply_user.id:
                name = html.escape(reply_user.first_name or 'Автор')
                print(f"   ✅ Особый метод (reply): имя {name}")
                return name, reply_user.id
        print(f"   ❌ Особый метод: нет данных об авторе в message")
        return None, None
    except Exception as e:
        print(f"   ⚠️ Ошибка особого метода: {e}")
        return None, None

async def get_author_username(client, message):
    try:
        if message.from_user:
            if message.from_user.username:
                print(f"   ✅ Найден username: @{message.from_user.username}")
                return f"@{message.from_user.username}", message.from_user.id
            elif message.from_user.id:
                if message.from_user.first_name:
                    name = html.escape(message.from_user.first_name)
                    print(f"   ✅ Найдено имя: {name}")
                    return name, message.from_user.id
                else:
                    print(f"   ✅ Найден автор с ID {message.from_user.id}")
                    return "Автор", message.from_user.id
        return None, None
    except Exception as e:
        print(f"⚠️ Ошибка получения автора: {e}")
        return None, None

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
            message_id = res.get("result", {}).get("message_id")
            if message_id:
                update_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup"
                buttons = []
                if message_link:
                    buttons.append({"text": "🔗 Источник", "url": message_link})
                buttons.append({"text": "✏️ Ввести контакт", "callback_data": f"edit_contact_{message_id}"})
                buttons.append({"text": "✅ Опубликовать", "callback_data": f"pub_approve_special_{message_id}"})
                buttons.append({"text": "❌ Отклонить", "callback_data": f"pub_decline_special_{message_id}"})
                keyboard_rows = []
                for i in range(0, len(buttons), 2):
                    keyboard_rows.append(buttons[i:i+2])
                update_payload = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": keyboard_rows}
                }
                update_res = requests.post(update_url, json=update_payload, timeout=10)
                if update_res.status_code == 200:
                    print(f"✅ Кнопки для особого чата обновлены (message_id: {message_id})")
                else:
                    print(f"⚠️ Ошибка обновления кнопок: {update_res.text}")
        if not res.get("ok"):
            print(f"❌ Ошибка Telegram API: {res.get('description')}")
            if "thread not found" in str(res).lower() and thread_id:
                payload.pop("message_thread_id", None)
                requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"❌ Ошибка при отправке модератору: {e}")

@app.on_message()
async def check_messages(client, message):
    msg_id = f"{message.chat.id}_{message.id}"
    if msg_id in sent_messages:
        return
    sent_messages.add(msg_id)
    if len(sent_messages) > 10000:
        sent_messages.clear()
    
    # ========== ПРОВЕРКА ЧЁРНОГО СПИСКА ==========
    if message.from_user and is_user_blocked(message.from_user.id):
        print(f"🚫 Пропущено: пользователь {message.from_user.id} в ЧС")
        return

    if message.sender_chat and is_user_blocked(message.sender_chat.id):
        print(f"🚫 Пропущено: канал {message.sender_chat.id} в ЧС")
        return
    # ============================================
    
    # Игнорируем сообщения от бота в группе модерации
    if message.chat.id == TARGET_GROUP_ID and message.from_user and message.from_user.is_bot:
        return
    
    if message.chat.id == -1001156193082:
        print(f"\n🔴🔴🔴 ВХОДИТ СООБЩЕНИЕ ИЗ ТВОЕГО ЧАТА! 🔴🔴🔴")
        print(f"ID сообщения: {message.id}")
        print(f"Текст: {message.text or message.caption}")
        print(f"message.from_user: {message.from_user}")
        print(f"message.sender_chat: {message.sender_chat}")
        print(f"message.chat.type: {message.chat.type}")
        channels_db, keywords, stop_words = get_data_from_db()
        print(f"Ключевые слова в БД: {keywords}")
        print(f"Стоп-слова в БД: {stop_words}")
        content = message.text or message.caption or ""
        msg_text_lower = content.lower()
        for word in keywords:
            if word and word in msg_text_lower:
                print(f"✅ Найдено ключевое слово: {word}")
        for stop_word in stop_words:
            if stop_word and stop_word in msg_text_lower:
                print(f"❌ Найдено стоп-слово: {stop_word}")
        print(f"Чат {message.chat.id} в списке каналов для парсинга: {str(message.chat.id) in channels_db or message.chat.username in channels_db if channels_db else 'Нет каналов'}")
        print(f"🔴🔴🔴 КОНЕЦ ОТЛАДКИ 🔴🔴🔴\n")
    
    special_chats = get_special_chats()
    print(f"\n{'='*50}")
    print(f"🔍 Обработка сообщения ID: {message.id}")
    print(f"   Чат ID: {message.chat.id}")
    print(f"   Тип чата: {message.chat.type}")
    print(f"   Особый чат: {'✅ ДА' if message.chat.id in special_chats else '❌ НЕТ'}")
    if message.from_user:
        print(f"   Автор username: @{message.from_user.username}" if message.from_user.username else f"   Автор ID: {message.from_user.id}, username: отсутствует")
        print(f"   Автор first_name: {message.from_user.first_name}")
    else:
        print(f"   Автор: не определён (message.from_user = None)")
    print(f"{'='*50}")
    
    if message.chat.id in special_chats:
        print(f"\n🔴🔴🔴 ДЕТАЛЬНАЯ ОТЛАДКА ДЛЯ ОСОБОГО ЧАТА 🔴🔴🔴")
        print(f"message.sender_chat: {message.sender_chat}")
        print(f"message.date: {message.date}")
        print(f"message.reply_to_message: {message.reply_to_message}")
        try:
            async for msg in client.get_chat_history(message.chat.id, limit=10):
                if msg.id == message.id:
                    print(f"Найдено сообщение в истории:")
                    print(f"  msg.from_user: {msg.from_user}")
                    print(f"  msg.sender_chat: {msg.sender_chat}")
                    if msg.from_user:
                        print(f"  msg.from_user.username: {msg.from_user.username}")
                        print(f"  msg.from_user.first_name: {msg.from_user.first_name}")
                    break
        except Exception as e:
            print(f"Ошибка истории: {e}")
        print(f"🔴🔴🔴 КОНЕЦ ОТЛАДКИ 🔴🔴🔴\n")
    
    if not message.chat or message.chat.id == TARGET_GROUP_ID:
        return

    content = ""
    
    if message.text:
        content = message.text
    elif message.caption:
        content = message.caption
    
    if message.reply_to_message:
        quote_text = message.reply_to_message.text or message.reply_to_message.caption
        if quote_text:
            if content:
                content = f"{content}\n\n{quote_text}"
            else:
                content = quote_text
            print(f"📝 Добавлена цитата в content: {quote_text[:50]}...")
    
    if not content:
        print("❌ Текст отсутствует, пропускаем")
        return
    
    print(f"📄 ИТОГОВЫЙ ТЕКСТ ДЛЯ АНАЛИЗА:\n{content[:200]}...")

    channels_db, keywords, stop_words = get_data_from_db()
    msg_text_lower = content.lower()

    is_target = False
    cid_full = str(message.chat.id)
    cid_short = cid_full.replace("-100", "")
    uname = (message.chat.username or "").lower()
    raw_topic_id = getattr(message, "message_thread_id", None)
    current_thread_id = str(raw_topic_id) if raw_topic_id else None

    for conf in channels_db:
        clean_conf = conf.replace('https://t.me/', '').replace('http://t.me/', '').replace('@', '').strip()
        target_chat = clean_conf
        target_topic = None

        if "/" in clean_conf:
            parts = clean_conf.rstrip('/').split('/')
            if parts[0] == "c" and len(parts) >= 3:
                target_chat, target_topic = parts[1], parts[2]
            elif len(parts) >= 2:
                target_chat, target_topic = parts[0], parts[1]

        if (target_chat == uname) or (target_chat == cid_full) or (target_chat == cid_short):
            if target_topic:
                if current_thread_id == target_topic:
                    is_target = True
                    break
            else:
                is_target = True
                break

    if not is_target:
        print("❌ Чат не в списке каналов, пропускаем")
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
        print("❌ Ключевое слово не найдено, пропускаем")
        return

    body_html = ""
    
    if message.text:
        body_html = message.text.html
    elif message.caption:
        body_html = html.escape(message.caption)
    
    if message.reply_to_message:
        if message.reply_to_message.text:
            quote_html = message.reply_to_message.text.html
        elif message.reply_to_message.caption:
            quote_html = html.escape(message.reply_to_message.caption)
        else:
            quote_html = None
        if quote_html:
            if body_html:
                body_html = f"{body_html}\n\n{quote_html}"
            else:
                body_html = quote_html
            print(f"📝 Добавлена цитата в HTML: {quote_html[:50]}...")
    
    if not body_html:
        print("❌ HTML отсутствует, пропускаем")
        return
    
    # ========== ПРИМЕНЯЕМ ФИЛЬТРЫ ==========
    body_html = apply_filters(body_html)
    # ======================================

    contact = "Не указан"
    author_id = None
    source_username = message.chat.username

    is_special = message.chat.id in special_chats
    
    if is_special:
        print(f"🔧 ЧАТ В СПИСКЕ ОСОБЫХ! Применяем специальный метод...")
        special_contact, special_author_id = await get_contact_special_method(client, message)
        if special_contact:
            contact = special_contact
            author_id = special_author_id
            print(f"📱 Особый метод сработал: {contact}")
        else:
            print(f"⚠️ Особый метод не дал результат, пробуем обычные способы...")
    
    if contact == "Не указан":
        entities = message.entities or message.caption_entities
        if entities:
            for entity in entities:
                if entity.type == enums.MessageEntityType.TEXT_LINK:
                    if "t.me/" in entity.url or "tg://user" in entity.url:
                        if source_username and source_username.lower() in entity.url.lower():
                            continue
                        contact = entity.url
                        print(f"🔗 Контакт найден в TEXT_LINK: {contact}")
                        break
                elif entity.type == enums.MessageEntityType.MENTION:
                    mention = content[entity.offset:entity.offset+entity.length]
                    if source_username and mention.lower().strip('@') == source_username.lower():
                        continue
                    contact = mention
                    print(f"📢 Контакт найден в MENTION: {contact}")
                    break

        if contact == "Не указан":
            found_usernames = re.findall(r"@[a-zA-Z0-9_]{5,}", content)
            for fu in found_usernames:
                if source_username and fu.lower().strip('@') == source_username.lower():
                    continue
                contact = fu
                print(f"📝 Контакт найден регуляркой: {contact}")
                break

    if contact == "Не указан" and not is_special:
        print(f"🔍 Контакт не найден в тексте, ищем через профиль автора...")
        author_contact, author_id = await get_author_username(client, message)
        if author_contact:
            contact = author_contact
            print(f"📱 Контакт получен через профиль автора: {contact} (ID: {author_id})")
        else:
            email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
            phone_pattern = r'\+?[\d\s\-\(\)]{10,}'
            email_match = re.search(email_pattern, content)
            phone_match = re.search(phone_pattern, content)
            if email_match:
                contact = email_match.group()
                print(f"📧 Контакт найден как email: {contact}")
            elif phone_match:
                contact = phone_match.group()
                print(f"📞 Контакт найден как телефон: {contact}")
            else:
                contact = "<i>⚠️ Контакт не найден (укажите @username или добавьте контакт в текст)</i>"
                print(f"⚠️ Контакт не найден в сообщении из чата {message.chat.id}")

    header = "<b>| 🇻 🇦 🇨 🇦 🇳 🇨 🇾 |</b>\n\n"
    contact_block = f"<b>Контакт для связи:</b>\n{contact}\n\n"
    instruction = (
        "<b><code>Для публикации вакансии\\резюме, напишите заявку в </code>"
        "<a href='https://t.me/Vakansii_GetJob_bot'>бота</a></b>"
    )
    tags = "\n\n#вакансия #парсер"

    pretty_text = f"{header}{body_html}\n\n{contact_block}{instruction}{tags}"
    
    # ========== ЖЁСТКОЕ УДАЛЕНИЕ КЛЕШЕ ==========
    pretty_text = hard_remove_cliche(pretty_text)
    # ============================================
    
    message_link = message.link if hasattr(message, 'link') else None
    if message_link:
        print(f"🔗 Ссылка на исходное сообщение: {message_link}")
    
    send_to_moderation(TARGET_GROUP_ID, REPORT_TOPIC_ID, pretty_text, author_id, message_link, is_special=is_special)

async def main():
    print("🚀 Запуск парсера...")
    await app.start()
    try:
        await app.get_chat(TARGET_GROUP_ID)
        print(f"✅ Доступ к группе модерации подтвержден")
    except Exception as e:
        print(f"⚠️ Группа {TARGET_GROUP_ID} не доступна: {e}")
    print(f"📡 Мониторинг запущен.")
    await asyncio.Event().wait()

if __name__ == "__main__":
    try:
        app.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Парсер остановлен")
    except Exception as e:
        print(f"💥 Критическая ошибка: {e}")
        sys.exit(1)