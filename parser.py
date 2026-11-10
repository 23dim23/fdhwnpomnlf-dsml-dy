import os
import sqlite3
import asyncio
import re
import requests
import html
import sys
from pyrogram import Client, enums
from dotenv import load_dotenv

from smart_filter import (
    balance_html_tags,
    strip_html_links_keep_formatting,
    is_user_blocked_smart,
    extract_all_contacts_and_ids,
    normalize_for_analysis,
    evaluate_vacancy_smart,
    get_smart_filter_settings,
    is_message_processed,
    mark_message_processed,
)

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


# ==================== ФИЛЬТРЫ ТЕКСТА (АВТОЗАМЕНА) ====================
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
    """Жёсткое удаление клише в цитате (в обход фильтров)"""
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
        except Exception:
            pass

    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' +\n', '\n', text)
    text = text.strip()

    return text


def get_special_chats():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id FROM special_chats")
        special_chats = []
        for row in cursor.fetchall():
            raw_val = str(row[0]).strip()
            if raw_val.lstrip('-').isdigit():
                special_chats.append(int(raw_val))
        conn.close()

        for chat_id in HARDCODED_SPECIAL_CHATS:
            if chat_id not in special_chats:
                special_chats.append(chat_id)

        return special_chats
    except Exception as e:
        print(f"❌ Ошибка чтения особых чатов: {e}")
        return HARDCODED_SPECIAL_CHATS


async def get_contact_special_method(client, message):
    """
    Для особых чатов возвращает (username_contact_or_none, author_id).
    Игнорирует сервисных ботов чата, чтобы не брать @username бота как контакт работодателя.
    """
    try:
        print(f"   ⭐ Применяем ОСОБЫЙ метод для чата {message.chat.id}")
        if message.from_user and not getattr(message.from_user, "is_bot", False):
            author_id = message.from_user.id
            if message.from_user.username and not message.from_user.username.lower().endswith("bot"):
                contact = f"@{message.from_user.username}"
                print(f"   ✅ Особый метод: найден @{message.from_user.username} (ID: {author_id})")
                return contact, author_id
            print(f"   ℹ️ Особый метод: у автора ID {author_id} нет личного @username, поищем в тексте")
            return None, author_id

        if message.reply_to_message and message.reply_to_message.from_user:
            reply_user = message.reply_to_message.from_user
            if not getattr(reply_user, "is_bot", False):
                if reply_user.username and not reply_user.username.lower().endswith("bot"):
                    print(f"   ✅ Особый метод (reply): @{reply_user.username}")
                    return f"@{reply_user.username}", reply_user.id
                return None, reply_user.id

        print("   ℹ️ Особый метод: сообщение от бота/анонима, ищем контакт внутри текста")
        return None, None
    except Exception as e:
        print(f"   ⚠️ Ошибка особого метода: {e}")
        return None, None


async def get_author_username(client, message):
    try:
        if message.from_user and not getattr(message.from_user, "is_bot", False):
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
    keyboard = [[{"text": "⏳ Загрузка...", "callback_data": "loading"}]]
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
        if res.get("ok"):
            message_id = res.get("result", {}).get("message_id")
            if message_id:
                update_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup"
                safe_author_id = author_id if author_id else 0

                top_row = []
                if message_link:
                    top_row.append({"text": "🔗 Источник", "url": message_link})
                top_row.append({"text": "🚫 В ЧС", "callback_data": f"ban_mod_{message_id}_{safe_author_id}"})

                keyboard_rows = [
                    top_row,
                    [
                        {"text": "✏️ Ввести контакт", "callback_data": f"edit_contact_{message_id}"},
                        {"text": "✏️ Редактировать", "callback_data": f"edit_text_{message_id}"}
                    ],
                    [
                        {"text": "✅ Опубликовать", "callback_data": f"pub_approve_special_{message_id}"},
                        {"text": "❌ Отклонить", "callback_data": f"pub_decline_special_{message_id}"}
                    ]
                ]

                update_payload = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": {"inline_keyboard": keyboard_rows}
                }
                update_res = requests.post(update_url, json=update_payload, timeout=10)
                if update_res.status_code == 200:
                    print(f"✅ Кнопки модерации обновлены (message_id: {message_id})")
                else:
                    print(f"⚠️ Ошибка обновления кнопок: {update_res.text}")
            return True
        else:
            print(f"❌ Ошибка Telegram API: {res.get('description')}")
            if "thread not found" in str(res).lower() and thread_id:
                payload.pop("message_thread_id", None)
                r2 = requests.post(url, json=payload, timeout=10)
                if r2.json().get("ok"):
                    return True
    except Exception as e:
        print(f"❌ Ошибка при отправке модератору: {e}")
    return False


@app.on_message()
async def check_messages(client, message):
    if not message.chat or message.chat.id == TARGET_GROUP_ID:
        return

    msg_id = f"{message.chat.id}_{message.id}"
    if msg_id in sent_messages:
        return
    sent_messages.add(msg_id)
    if len(sent_messages) > 10000:
        sent_messages.clear()

    special_chats = get_special_chats()
    channels_db, keywords, stop_words = get_data_from_db()

    is_target = False
    cid_full = str(message.chat.id)
    cid_short = cid_full[4:] if cid_full.startswith("-100") else cid_full.lstrip("-")
    uname = (message.chat.username or "").lower()

    # Определяем ID топика (форума) во всех вариантах Pyrogram
    raw_topic_id = (
        getattr(message, "message_thread_id", None)
        or getattr(getattr(message, "topic", None), "id", None)
        or getattr(message, "reply_to_top_message_id", None)
    )
    current_thread_id = str(raw_topic_id) if raw_topic_id else None
    is_forum_chat = bool(getattr(message.chat, "is_forum", False))

    if message.chat.id in special_chats or int(cid_short) in [ abs(int(x)) for x in special_chats if str(x).lstrip('-').isdigit() ]:
        is_target = True
    else:
        for conf in channels_db:
            clean_conf = conf.replace('https://t.me/', '').replace('http://t.me/', '').replace('t.me/', '').replace('@', '').strip()
            target_chat = clean_conf.strip('/')
            target_topic = None

            if "/" in target_chat:
                parts = [p for p in target_chat.split('/') if p]
                if parts and parts[0] == "c":
                    if len(parts) >= 3:
                        target_chat, target_topic = parts[1], parts[2]
                    elif len(parts) == 2:
                        target_chat, target_topic = parts[1], None
                elif len(parts) >= 2:
                    target_chat, target_topic = parts[0], parts[1]

            target_chat_short = target_chat[4:] if target_chat.startswith("-100") else target_chat.lstrip("-")

            if (target_chat == uname and uname) or (target_chat == cid_full) or (target_chat_short == cid_short):
                if target_topic and is_forum_chat and current_thread_id:
                    if current_thread_id == target_topic:
                        is_target = True
                        break
                else:
                    is_target = True
                    break

    if not is_target:
        return

    # Если это сообщение уже было успешно отправлено на модерацию (например, через telethon_worker) — пропускаем
    if is_message_processed(message.chat.id, message.id):
        print(f"⏭️ Сообщение {message.chat.id}_{message.id} уже отправлено на модерацию, пропускаем")
        return

    # Собираем ID и username реального автора (исключая сам канал/чат и сервисных ботов чата)
    extra_ids = []
    extra_usernames = []

    if message.from_user and not getattr(message.from_user, "is_bot", False):
        extra_ids.append(message.from_user.id)
        if message.from_user.username:
            extra_usernames.append(message.from_user.username)

    if message.sender_chat and message.sender_chat.id != message.chat.id:
        extra_ids.append(message.sender_chat.id)
        if message.sender_chat.username:
            extra_usernames.append(message.sender_chat.username)

    if getattr(message, "forward_from", None) and not getattr(message.forward_from, "is_bot", False):
        extra_ids.append(message.forward_from.id)
        if message.forward_from.username:
            extra_usernames.append(message.forward_from.username)

    if getattr(message, "forward_from_chat", None) and message.forward_from_chat.id != message.chat.id:
        extra_ids.append(message.forward_from_chat.id)
        if message.forward_from_chat.username:
            extra_usernames.append(message.forward_from_chat.username)

    if message.reply_to_message:
        r_user = message.reply_to_message.from_user
        if r_user and not getattr(r_user, "is_bot", False):
            extra_ids.append(r_user.id)
            if r_user.username:
                extra_usernames.append(r_user.username)

    # ========== РАННЯЯ ПРОВЕРКА ЧЁРНОГО СПИСКА ПО АВТОРУ ==========
    if is_user_blocked_smart(extra_ids=extra_ids, extra_usernames=extra_usernames, source_username=message.chat.username):
        print(f"🚫 Пропущено: автор {extra_ids} / {extra_usernames} в ЧС")
        return
    # ==============================================================

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

    if not content:
        return

    # Нормализованный текст (с защитой от латинских букв-двойников) для проверки стоп-слов и ключей
    msg_text_norm = normalize_for_analysis(content)
    msg_text_lower = content.lower()

    for stop_word in stop_words:
        if stop_word and (stop_word in msg_text_lower or stop_word in msg_text_norm):
            print(f"🚫 Пропущено (стоп-слово): '{stop_word}'")
            return

    found_keyword = False
    for word in keywords:
        if not word:
            continue
        pattern = rf"(?:^|[^а-яёa-z0-9]){re.escape(word)}(?:$|[^а-яёa-z0-9])"
        try:
            if re.search(pattern, msg_text_lower) or re.search(pattern, msg_text_norm):
                found_keyword = True
                break
        except Exception:
            if word in msg_text_lower or word in msg_text_norm:
                found_keyword = True
                break

    if not found_keyword:
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

    if not body_html:
        return

    # Собираем также все ссылки из сущностей сообщения (TEXT_LINK, MENTION) до удаления <a href>
    source_username = message.chat.username
    entity_contact = None
    entities = (message.entities or []) + (message.caption_entities or [])
    for entity in entities:
        if entity.type == enums.MessageEntityType.TEXT_LINK and entity.url:
            if "t.me/" in entity.url or "tg://user" in entity.url:
                if source_username and source_username.lower() in entity.url.lower():
                    continue
                extra_usernames.append(entity.url)
                if not entity_contact:
                    entity_contact = entity.url
        elif entity.type == enums.MessageEntityType.MENTION:
            mention = content[entity.offset:entity.offset + entity.length]
            if source_username and mention.lower().strip('@') == source_username.lower():
                continue
            extra_usernames.append(mention)
            if not entity_contact:
                entity_contact = mention

    # Извлекаем все контакты из текста и HTML
    text_usernames, text_ids, primary_text_contact = extract_all_contacts_and_ids(
        raw_text=content,
        html_text=body_html,
        source_username=source_username
    )
    extra_usernames.extend(list(text_usernames))
    extra_ids.extend(list(text_ids))

    # Определяем автора и контакт для связи (не берём ID самого канала или сервисного бота)
    contact = "Не указан"
    author_id = None
    if message.from_user and not getattr(message.from_user, "is_bot", False):
        author_id = message.from_user.id

    is_special = message.chat.id in special_chats

    if is_special:
        special_contact, special_author_id = await get_contact_special_method(client, message)
        if special_author_id:
            author_id = special_author_id
        if special_contact:
            contact = special_contact

    if contact == "Не указан":
        if primary_text_contact:
            contact = primary_text_contact
        elif entity_contact:
            if 't.me/' in entity_contact:
                uname_part = entity_contact.split('t.me/')[-1].split('?')[0].strip('/')
                if uname_part and not uname_part.startswith('+') and uname_part != 'c':
                    contact = f"@{uname_part.lstrip('@')}"
                else:
                    contact = entity_contact
            else:
                contact = entity_contact

    if contact == "Не указан":
        if not is_special:
            author_contact, fetched_author_id = await get_author_username(client, message)
            if fetched_author_id:
                author_id = fetched_author_id
            if author_contact and author_contact.startswith('@'):
                contact = author_contact
            else:
                email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
                phone_pattern = r'\+?[\d\s\-\(\)]{10,}'
                email_match = re.search(email_pattern, content)
                phone_match = re.search(phone_pattern, content)
                if email_match:
                    contact = email_match.group()
                elif phone_match:
                    contact = phone_match.group()
                elif author_contact:
                    contact = author_contact
                else:
                    contact = "<i>⚠️ Контакт не найден (укажите @username или добавьте контакт в текст)</i>"
        else:
            contact = "<i>⚠️ Контакт не найден (укажите @username или добавьте контакт в текст)</i>"

    if author_id:
        extra_ids.append(author_id)
    if contact and "⚠️" not in contact and contact != "Не указан":
        extra_usernames.append(contact)

    # ========== ПОЛНАЯ ПРОВЕРКА ЧЁРНОГО СПИСКА (АВТОР + ВСЕ КОНТАКТЫ В ТЕКСТЕ) ==========
    if is_user_blocked_smart(
        user_id=author_id,
        username=contact if "⚠️" not in contact else None,
        extra_ids=extra_ids,
        extra_usernames=extra_usernames,
        raw_text=content,
        html_text=body_html,
        source_username=source_username
    ):
        print(f"🚫 Пропущено (ЧС): автор ID={author_id}, контакт={contact}, найденные={extra_usernames}")
        return
    # ====================================================================================

    # ========== ПРИМЕНЯЕМ АВТОЗАМЕНЫ И УДАЛЯЕМ ВШИТЫЕ ССЫЛКИ ИЗ HTML ====================
    body_html = apply_filters(body_html)
    body_html = strip_html_links_keep_formatting(body_html)
    body_html = hard_remove_cliche(body_html)
    # ====================================================================================

    # ========== УМНЫЙ ФИЛЬТР ВАКАНСИЙ (ТЕСТОВЫЙ ИЛИ БОЕВОЙ РЕЖИМ) =======================
    eval_res = evaluate_vacancy_smart(
        raw_text=content,
        contact=contact,
        author_id=author_id,
        chat_id=message.chat.id,
        message_id=message.id
    )
    sf_settings = get_smart_filter_settings()

    if sf_settings["live_mode_enabled"] and not eval_res["passed"]:
        print(f"🛡️ Отклонено умным фильтром (БОЕВОЙ РЕЖИМ): {'; '.join(eval_res['reasons'])}")
        return

    tech_block = ""
    if sf_settings["tech_comment_enabled"]:
        tech_block = f"{eval_res['tech_comment']}\n\n"
    # ====================================================================================

    header = "<b>| 🇻 🇦 🇨 🇦 🇳 🇨 🇾 |</b>\n\n"
    contact_block = f"<b>Контакт для связи:</b>\n{contact}\n\n"
    instruction = (
        "<b>Для публикации вакансии\\резюме, напишите заявку в "
        "<a href='https://t.me/Vakansii_GetJob_bot'>бота</a></b>"
    )
    tags = "\n\n#вакансия #парсер"

    pretty_text = balance_html_tags(f"{header}{body_html}\n\n{tech_block}{contact_block}{instruction}{tags}")

    message_link = message.link if hasattr(message, 'link') else None
    sent_ok = send_to_moderation(TARGET_GROUP_ID, REPORT_TOPIC_ID, pretty_text, author_id, message_link, is_special=is_special)
    if sent_ok:
        mark_message_processed(message.chat.id, message.id)


async def main():
    print("🚀 Запуск парсера...")
    await app.start()
    try:
        await app.get_chat(TARGET_GROUP_ID)
        print("✅ Доступ к группе модерации подтвержден")
    except Exception as e:
        print(f"⚠️ Группа {TARGET_GROUP_ID} не доступна: {e}")
    print("📡 Мониторинг запущен.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        app.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("🛑 Парсер остановлен")
    except Exception as e:
        print(f"💥 Критическая ошибка: {e}")
        sys.exit(1)
