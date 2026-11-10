import os
import sqlite3
import asyncio
import re
import requests
import html
import sys
import traceback
from telethon import TelegramClient
from telethon.extensions import html as tl_html
from telethon.tl.types import MessageEntityTextUrl, MessageEntityMention
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
    get_worker_last_id,
    set_worker_last_id,
)

load_dotenv()

# --- НАСТРОЙКИ ---
API_ID = int(os.getenv('API_ID', 33481567))
API_HASH = os.getenv('API_HASH', "93d073404049ef77e94be613d29fb57d")
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в переменных окружения!")

TARGET_GROUP_ID = -1003455134116
REPORT_TOPIC_ID = None
DB_FILE = 'parser_data.db'
PROBLEM_CHAT_ID = -1001156193082

HARDCODED_SPECIAL_CHATS = [
    -1001156193082,
]

client = TelegramClient('telethon_session', API_ID, API_HASH)


# ==================== КОНВЕРТАЦИЯ MARKDOWN И TELETHON HTML БЕЗ ВШИТЫХ ССЫЛОК ====================
def markdown_to_html(text):
    """Конвертирует базовый Markdown в HTML для Telegram, удаляя вшитые ссылки [текст](url) -> текст."""
    if not text:
        return text

    # 1. Ссылки [текст](url) -> оставляем только видимый текст без <a href>
    text = re.sub(r'\[([^\]]+)\]\(([^\)]+)\)', r'\1', text)

    # 2. Жирный текст **текст**
    text = re.sub(r'\*\*([^\*]+)\*\*', r'<b>\1</b>', text)

    # 3. Курсив __текст__
    text = re.sub(r'__([^_]+)__', r'<i>\1</i>', text)

    # 4. Зачёркнутый ~~текст~~
    text = re.sub(r'~~([^~]+)~~', r'<s>\1</s>', text)

    # 5. Моноширинный `текст`
    text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)

    # 6. Удаляем любые оставшиеся <a href="..."> и балансируем теги
    return strip_html_links_keep_formatting(text)


def extract_telethon_html(msg):
    """Извлекает HTML-верстку из сообщения Telethon (entities + markdown) без вшитых ссылок."""
    if not msg:
        return ""
    raw = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or ""
    if not raw:
        return ""
    try:
        entities = getattr(msg, "entities", None)
        if entities:
            html_str = tl_html.unparse(raw, entities)
        else:
            html_str = html.escape(raw)
    except Exception:
        html_str = html.escape(raw)
    return markdown_to_html(html_str)


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


# ==================== ОБРАБОТКА СООБЩЕНИЯ ====================
async def process_message(message, target_chat_id=PROBLEM_CHAT_ID):
    try:
        # Если сообщение уже было успешно отправлено на модерацию — пропускаем
        if is_message_processed(target_chat_id, message.id):
            return

        raw_content = getattr(message, "raw_text", None) or message.text or ""
        content = raw_content
        reply_msg = None

        if message.reply_to_msg_id:
            try:
                reply_msg = await message.get_reply_message()
                if reply_msg:
                    reply_raw = getattr(reply_msg, "raw_text", None) or reply_msg.text or ""
                    if reply_raw:
                        content = f"{content}\n\n{reply_raw}" if content else reply_raw
            except Exception as e:
                print(f"⚠️ Не удалось получить цитату: {e}")

        if not content:
            return

        # Собираем все ссылки и упоминания из сущностей Telethon (включая цитату) до фильтрации
        extra_ids = []
        extra_usernames = []
        entity_contact = None

        def collect_msg_entities(msg_obj):
            nonlocal entity_contact
            if not msg_obj or not getattr(msg_obj, "entities", None):
                return
            msg_raw = getattr(msg_obj, "raw_text", None) or ""
            for ent in msg_obj.entities:
                if isinstance(ent, MessageEntityTextUrl) and ent.url:
                    if "t.me/" in ent.url or "tg://user" in ent.url:
                        extra_usernames.append(ent.url)
                        if not entity_contact:
                            entity_contact = ent.url
                elif isinstance(ent, MessageEntityMention) and msg_raw:
                    mention = msg_raw[ent.offset:ent.offset + ent.length]
                    if mention and not mention.lower().endswith("bot"):
                        extra_usernames.append(mention)
                        if not entity_contact:
                            entity_contact = mention

        collect_msg_entities(message)
        if reply_msg:
            collect_msg_entities(reply_msg)

        sender = await message.get_sender()
        is_sender_bot = bool(getattr(sender, 'bot', False)) if sender else False
        raw_sender_uname = getattr(sender, 'username', None) if sender else None
        if raw_sender_uname and raw_sender_uname.lower().endswith('bot'):
            is_sender_bot = True

        # Игнорируем ID и @username сервисного бота чата, чтобы бот не блокировал чат и не подставлялся вместо контакта
        author_id = getattr(sender, 'id', None) if (sender and not is_sender_bot and getattr(sender, 'id', None) != target_chat_id) else None
        sender_username = raw_sender_uname if (raw_sender_uname and not is_sender_bot) else None
        sender_first_name = getattr(sender, 'first_name', None) if (sender and not is_sender_bot) else None

        if author_id:
            extra_ids.append(author_id)
        if sender_username:
            extra_usernames.append(sender_username)

        if reply_msg:
            try:
                reply_sender = await reply_msg.get_sender()
                if reply_sender:
                    is_reply_bot = bool(getattr(reply_sender, 'bot', False))
                    r_uname = getattr(reply_sender, 'username', None)
                    if r_uname and r_uname.lower().endswith('bot'):
                        is_reply_bot = True
                    r_id = getattr(reply_sender, 'id', None)
                    if not is_reply_bot and r_id != target_chat_id:
                        if r_id:
                            extra_ids.append(r_id)
                            if not author_id:
                                author_id = r_id
                        if r_uname:
                            extra_usernames.append(r_uname)
                            if not sender_username:
                                sender_username = r_uname
            except Exception:
                pass

        # Ранняя проверка ЧС по реальному отправителю и сущностям
        if is_user_blocked_smart(
            user_id=author_id,
            username=sender_username,
            extra_ids=extra_ids,
            extra_usernames=extra_usernames,
            raw_text=content
        ):
            print(f"🚫 Пропущено (ЧС в telethon_worker): ID={author_id}, @{sender_username}")
            return

        channels_db, keywords, stop_words = get_data_from_db()
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

        is_special = is_chat_special(target_chat_id)

        # Собираем HTML-верстку сообщения и цитаты без вшитых ссылок
        main_html = extract_telethon_html(message)
        if reply_msg:
            reply_html = extract_telethon_html(reply_msg)
            if reply_html:
                main_html = f"{main_html}\n\n{reply_html}" if main_html else reply_html

        # Извлекаем все контакты из текста и HTML
        text_usernames, text_ids, primary_text_contact = extract_all_contacts_and_ids(
            raw_text=content,
            html_text=main_html
        )
        extra_usernames.extend([u for u in text_usernames if not u.lower().endswith('bot')])
        extra_ids.extend(list(text_ids))

        contact = "Не указан"
        if sender_username:
            contact = f"@{sender_username}"
        elif primary_text_contact and not primary_text_contact.lower().endswith('bot'):
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
        else:
            # Резервный поиск email или телефона, как в основном парсере
            email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'
            phone_pattern = r'\+?[\d\s\-\(\)]{10,}'
            email_match = re.search(email_pattern, content)
            phone_match = re.search(phone_pattern, content)
            if email_match:
                contact = email_match.group()
            elif phone_match:
                contact = phone_match.group()
            elif not is_special and sender_first_name:
                contact = html.escape(sender_first_name)
            else:
                contact = "<i>⚠️ Контакт не найден (укажите @username или добавьте контакт в текст)</i>"

        # Полная проверка ЧС
        if is_user_blocked_smart(
            user_id=author_id,
            username=contact if "⚠️" not in contact else None,
            extra_ids=extra_ids,
            extra_usernames=extra_usernames,
            raw_text=content,
            html_text=main_html
        ):
            print(f"🚫 Пропущено (ЧС): ID={author_id}, контакт={contact}")
            return

        # Применяем текстовые фильтры, удаляем вшитые ссылки и клише безопасности
        body_html = apply_filters(main_html)
        body_html = strip_html_links_keep_formatting(body_html)
        body_html = hard_remove_cliche(body_html)

        # ========== УМНЫЙ ФИЛЬТР ВАКАНСИЙ (ТЕСТОВЫЙ ИЛИ БОЕВОЙ РЕЖИМ) ==========
        eval_res = evaluate_vacancy_smart(
            raw_text=content,
            contact=contact,
            author_id=author_id,
            chat_id=target_chat_id,
            message_id=message.id
        )
        sf_settings = get_smart_filter_settings()

        if sf_settings["live_mode_enabled"] and not eval_res["passed"]:
            print(f"🛡️ Отклонено умным фильтром (БОЕВОЙ РЕЖИМ): {'; '.join(eval_res['reasons'])}")
            return

        tech_block = ""
        if sf_settings["tech_comment_enabled"]:
            tech_block = f"{eval_res['tech_comment']}\n\n"
        # =======================================================================

        header = "<b>| 🇻 🇦 🇨 🇦 🇳 🇨 🇾 |</b>\n\n"
        contact_block = f"<b>Контакт для связи:</b>\n{contact}\n\n"
        instruction = (
            "<b>Для публикации вакансии\\резюме, напишите заявку в "
            "<a href='https://t.me/Vakansii_GetJob_bot'>бота</a></b>"
        )
        tags = "\n\n#вакансия #парсер"
        pretty_text = balance_html_tags(f"{header}{body_html}\n\n{tech_block}{contact_block}{instruction}{tags}")

        message_link = None
        if str(target_chat_id).startswith("-100"):
            chat_id_for_link = str(target_chat_id)[4:]
            message_link = f"https://t.me/c/{chat_id_for_link}/{message.id}"

        sent_ok = send_to_moderation(TARGET_GROUP_ID, REPORT_TOPIC_ID, pretty_text, author_id, message_link, is_special=is_special)
        if sent_ok:
            mark_message_processed(target_chat_id, message.id)
            print(f"✅ Отправлено в модерацию из {target_chat_id}: msg_id={message.id} (особый режим: {is_special})")

    except Exception as e:
        print(f"❌ Ошибка в process_message: {e}")
        traceback.print_exc()


# ==================== ПОЛЛИНГ ====================
async def main():
    print("🚀 Запуск Telethon-воркера для особых/проблемных чатов...")

    try:
        await client.start()
        print("✅ Клиент Telethon запущен")
    except Exception as e:
        print(f"❌ Ошибка запуска клиента: {e}")
        traceback.print_exc()
        return

    entities_cache = {}
    last_ids = {}

    for cid in get_special_chats():
        saved_id = get_worker_last_id(cid)
        last_ids[cid] = saved_id
        if saved_id > 0:
            print(f"📌 Чат {cid}: восстановлен last_id из БД = {saved_id}")

    while True:
        try:
            target_chats = get_special_chats()
            for chat_id in target_chats:
                if chat_id not in entities_cache:
                    try:
                        entities_cache[chat_id] = await client.get_entity(chat_id)
                        print(f"✅ Доступ к чату {chat_id} получен")
                    except Exception as e:
                        print(f"❌ Нет доступа к чату {chat_id}: {e}")
                        continue

                entity = entities_cache[chat_id]
                last_id = last_ids.get(chat_id, get_worker_last_id(chat_id))

                # Запрашиваем последние 20 сообщений без min_id (надежнее в Telethon)
                messages = await client.get_messages(entity, limit=20)
                if not messages:
                    continue

                max_msg_id_in_chat = max(m.id for m in messages if m and getattr(m, 'id', None))
                # Защита от залипания last_id, если в чате удалили сообщения или сбросилась нумерация
                if last_id > max_msg_id_in_chat:
                    print(f"⚠️ В чате {chat_id} сохраненный last_id ({last_id}) больше текущего ({max_msg_id_in_chat}), корректируем")
                    last_id = max(0, max_msg_id_in_chat - 5)

                for msg in reversed(messages):
                    if msg.id > last_id:
                        await process_message(msg, target_chat_id=chat_id)
                        last_id = max(last_id, msg.id)
                        last_ids[chat_id] = last_id
                        set_worker_last_id(chat_id, last_id)

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
