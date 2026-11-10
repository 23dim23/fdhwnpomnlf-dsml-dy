import sqlite3
import re
import html
import hashlib
from datetime import datetime, timedelta

DB_FILE = 'parser_data.db'

# ==================== ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ ====================

def init_smart_filter_db():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()

        cursor.execute('''CREATE TABLE IF NOT EXISTS blocked_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT UNIQUE NOT NULL,
            username TEXT,
            reason TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS smart_filter_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            tech_comment_enabled INTEGER DEFAULT 1,
            live_mode_enabled INTEGER DEFAULT 0
        )''')

        cursor.execute("SELECT COUNT(*) FROM smart_filter_settings")
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO smart_filter_settings (id, tech_comment_enabled, live_mode_enabled) VALUES (1, 1, 0)"
            )

        cursor.execute('''CREATE TABLE IF NOT EXISTS processed_messages (
            msg_key TEXT PRIMARY KEY,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS vacancy_hashes (
            text_hash TEXT PRIMARY KEY,
            chat_id TEXT,
            message_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS author_cooldowns (
            author_key TEXT PRIMARY KEY,
            last_text_hash TEXT,
            post_count INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        cursor.execute('''CREATE TABLE IF NOT EXISTS worker_state (
            key TEXT PRIMARY KEY,
            val_int INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка инициализации smart_filter_db: {e}")

init_smart_filter_db()


# ==================== НАСТРОЙКИ УМНОГО ФИЛЬТРА ====================

def get_smart_filter_settings():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT tech_comment_enabled, live_mode_enabled FROM smart_filter_settings WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "tech_comment_enabled": bool(row[0]),
                "live_mode_enabled": bool(row[1])
            }
    except Exception as e:
        print(f"⚠️ Ошибка чтения настроек умного фильтра: {e}")
    return {"tech_comment_enabled": True, "live_mode_enabled": False}


def toggle_tech_comment():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("UPDATE smart_filter_settings SET tech_comment_enabled = NOT tech_comment_enabled WHERE id = 1")
        conn.commit()
        cursor.execute("SELECT tech_comment_enabled FROM smart_filter_settings WHERE id = 1")
        val = bool(cursor.fetchone()[0])
        conn.close()
        return val
    except Exception as e:
        print(f"⚠️ Ошибка переключения tech_comment: {e}")
        return True


def toggle_live_mode():
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("UPDATE smart_filter_settings SET live_mode_enabled = NOT live_mode_enabled WHERE id = 1")
        conn.commit()
        cursor.execute("SELECT live_mode_enabled FROM smart_filter_settings WHERE id = 1")
        val = bool(cursor.fetchone()[0])
        conn.close()
        return val
    except Exception as e:
        print(f"⚠️ Ошибка переключения live_mode: {e}")
        return False


# ==================== СОСТОЯНИЕ ВОРКЕРА (LAST_ID И ДЕДУПЛИКАЦИЯ СООБЩЕНИЙ) ====================

def get_worker_last_id(chat_id):
    key = f"last_id_{chat_id}"
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT val_int FROM worker_state WHERE key = ?", (key,))
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"⚠️ Ошибка чтения last_id: {e}")
        return 0


def set_worker_last_id(chat_id, last_id):
    key = f"last_id_{chat_id}"
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO worker_state (key, val_int, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET val_int = excluded.val_int, updated_at = CURRENT_TIMESTAMP
        ''', (key, int(last_id)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка сохранения last_id: {e}")


def is_message_processed(chat_id, message_id):
    """Проверяет, было ли сообщение (chat_id, message_id) уже отправлено на модерацию."""
    msg_key = f"{chat_id}_{message_id}"
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM processed_messages WHERE msg_key = ?", (msg_key,))
        exists = cursor.fetchone() is not None
        conn.close()
        return exists
    except Exception as e:
        print(f"⚠️ Ошибка проверки is_message_processed: {e}")
        return False


def mark_message_processed(chat_id, message_id):
    """Помечает сообщение (chat_id, message_id) как успешно обработанное и отправленное."""
    msg_key = f"{chat_id}_{message_id}"
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO processed_messages (msg_key) VALUES (?)", (msg_key,))
        cursor.execute("DELETE FROM processed_messages WHERE created_at < datetime('now', '-7 days')")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Ошибка записи mark_message_processed: {e}")


def mark_and_check_message_processed(chat_id, message_id):
    """Оставлено для обратной совместимости: проверяет, было ли сообщение уже отправлено."""
    return is_message_processed(chat_id, message_id)


# ==================== ОЧИСТКА HTML И УДАЛЕНИЕ ВШИТЫХ ССЫЛОК ====================

def balance_html_tags(html_str):
    """Балансирует открытые и закрытые HTML-теги, удаляя сиротские закрывающие теги."""
    if not html_str:
        return ""

    allowed_tags = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'blockquote', 'a'}
    tag_pattern = re.compile(r'<(/?)([a-zA-Z0-9]+)(?:\s+[^>]*?)?>')

    result = []
    last_idx = 0
    open_tags = []

    for match in tag_pattern.finditer(html_str):
        start, end = match.span()
        result.append(html_str[last_idx:start])
        last_idx = end

        is_closing = bool(match.group(1))
        tag_name = match.group(2).lower()
        full_tag = match.group(0)

        if tag_name not in allowed_tags:
            continue

        if not is_closing:
            open_tags.append(tag_name)
            result.append(full_tag)
        else:
            if tag_name in open_tags:
                # Закрываем все вложенные теги до нужного
                while open_tags:
                    popped = open_tags.pop()
                    result.append(f"</{popped}>")
                    if popped == tag_name:
                        break
            else:
                # Сиротский закрывающий тег — пропускаем
                pass

    result.append(html_str[last_idx:])
    for tag in reversed(open_tags):
        result.append(f"</{tag}>")

    return "".join(result)


def strip_html_links_keep_formatting(html_text):
    """
    Сохраняет HTML-верстку (<b>, <i>, <u>, <s>, <code>, <pre>, <blockquote>),
    но УДАЛЯЕТ все вшитые ссылки <a href="...">текст</a> -> текст,
    а также markdown-ссылки [текст](url) -> текст.
    """
    if not html_text:
        return ""

    # 1. Заменяем <a href="...">текст</a> на просто внутренний текст
    text = re.sub(r'<a\s+[^>]*>(.*?)</a>', r'\1', html_text, flags=re.IGNORECASE | re.DOTALL)
    # На случай незакрытых/одиночных <a ...> или </a>
    text = re.sub(r'</?a(?:\s+[^>]*?)?>', '', text, flags=re.IGNORECASE)

    # 2. На случай если в тексте остались markdown-ссылки [анкор](https://...)
    text = re.sub(r'\[([^\]]+)\]\((?:https?://|t\.me/|tg://)[^\)]+\)', r'\1', text, flags=re.IGNORECASE)

    # 3. Балансируем оставшиеся теги форматирования
    return balance_html_tags(text)


def strip_tech_comment(text):
    """Удаляет технический комментарий фильтра из текста перед публикацией или редактированием."""
    if not text:
        return ""
    # Удаляем блок в <blockquote> или без него
    text = re.sub(
        r'\n*<blockquote>\s*(?:⚠️|✅)\s*<b>\[ТЕСТ ФИЛЬТРА\].*?</blockquote>\n*',
        '\n\n',
        text,
        flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(
        r'\n*(?:⚠️|✅)\s*(?:<b>)?\[ТЕСТ ФИЛЬТРА\].*?(?=\n\n<b>Контакт для связи:|\n\nКонтакт для связи:|$)',
        '',
        text,
        flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# ==================== ЧЁРНЫЙ СПИСОК (ЧС) ====================

IGNORED_USERNAMES = {
    'vakansii_getjob_bot',
    'getjob_videoedit',
    'getjob_smm',
    'getjob_it',
    'getjob_copyright',
    'getjob_manager',
    'getjob_design',
    'gmail',
    'yandex',
    'mail',
    'icloud',
    'outlook',
    'yahoo',
    'proton',
    'protonmail',
    'bk',
    'inbox',
    'list',
}


def normalize_identifier(val):
    """
    Принимает числовой ID, @username, ссылку t.me/..., tg://user?id=...
    Возвращает кортеж: (norm_user_id: str | None, norm_username: str | None)
    """
    if val is None:
        return None, None
    s = str(val).strip()
    if not s:
        return None, None

    # tg://user?id=123456
    if 'tg://user?id=' in s.lower():
        m = re.search(r'tg://user\?id=(\d+)', s, flags=re.IGNORECASE)
        if m:
            return m.group(1), None

    # Ссылка t.me/
    if 't.me/' in s.lower():
        part = re.split(r't\.me/', s, flags=re.IGNORECASE)[-1]
        part = part.split('?')[0].split('#')[0].strip('/')
        parts = [p for p in part.split('/') if p]
        if parts:
            first = parts[0]
            if first.lower() == 'c' and len(parts) >= 2 and parts[1].isdigit():
                # Это ссылка вида t.me/c/1234567/890 на сообщение в чате, НЕ пользователь
                return None, None
            if first.startswith('+') or first.lower() == 'joinchat':
                return None, None
            clean_u = first.lstrip('@').lower()
            if clean_u and re.match(r'^[a-z0-9_]{3,32}$', clean_u):
                return None, clean_u
        return None, None

    # Числовой ID
    if s.lstrip('-').isdigit():
        return s, None

    # @username или просто username
    clean_u = s.lstrip('@').strip().lower()
    if re.match(r'^[a-z0-9_]{3,32}$', clean_u):
        return None, clean_u

    return None, None


def extract_all_contacts_and_ids(raw_text="", html_text="", source_username=None):
    """
    Извлекает ВСЕ @username, ссылки t.me/username и tg://user?id=... из текста и HTML.
    Возвращает (usernames_set, user_ids_set, primary_contact_display).
    """
    usernames = set()
    user_ids = set()
    primary_contact = None

    src_u = source_username.lstrip('@').lower() if source_username else None

    combined = f"{raw_text or ''}\n{html_text or ''}"

    # 1. Ищем tg://user?id=...
    for m in re.finditer(r'tg://user\?id=(\d+)', combined, flags=re.IGNORECASE):
        uid = m.group(1)
        user_ids.add(uid)

    # 2. Ищем t.me/username
    for m in re.finditer(r'(?:https?://)?(?:www\.)?t\.me/([a-zA-Z0-9_]{4,32})(?![a-zA-Z0-9_/])', combined, flags=re.IGNORECASE):
        u = m.group(1).lower()
        if u in ('c', 'joinchat', 'addstickers', 'share', 'proxy', 'socks') or u in IGNORED_USERNAMES:
            continue
        if src_u and u == src_u:
            continue
        usernames.add(u)
        if not primary_contact:
            primary_contact = f"@{m.group(1)}"

    # 3. Ищем @username (исключая email вида name@domain.ru)
    for m in re.finditer(r'(?<![a-zA-Z0-9._%+-])@([a-zA-Z0-9_]{4,32})\b', raw_text or ""):
        u = m.group(1).lower()
        if u in IGNORED_USERNAMES:
            continue
        if src_u and u == src_u:
            continue
        usernames.add(u)
        if not primary_contact:
            primary_contact = f"@{m.group(1)}"

    return usernames, user_ids, primary_contact


def get_protected_source_identifiers():
    """
    Возвращает (protected_ids, protected_usernames) всех отслеживаемых чатов/каналов
    из таблиц channels и special_chats, чтобы сам чат-источник никогда не мог попасть в ЧС
    и заблокировать парсинг всего чата.
    """
    protected_ids = {"-1001156193082", "1156193082", "-1156193082", "-1003455134116", "3455134116"}
    protected_usernames = set(IGNORED_USERNAMES)
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT chat_id FROM special_chats")
            for (cid,) in cursor.fetchall():
                s = str(cid).strip()
                abs_s = s.lstrip('-')
                protected_ids.update([s, abs_s, f"-{abs_s}", f"-100{abs_s}"])
                if abs_s.startswith("100") and len(abs_s) > 5:
                    short_s = abs_s[3:]
                    protected_ids.update([short_s, f"-{short_s}", f"-100{short_s}"])
        except Exception:
            pass

        try:
            cursor.execute("SELECT url FROM channels")
            for (url,) in cursor.fetchall():
                clean = str(url).strip().lower()
                clean = clean.replace('https://t.me/', '').replace('http://t.me/', '').replace('@', '').strip('/')
                parts = [p for p in clean.split('/') if p]
                if not parts:
                    continue
                target = parts[1] if (parts[0] == 'c' and len(parts) >= 2) else parts[0]
                if target.lstrip('-').isdigit():
                    abs_s = target.lstrip('-')
                    protected_ids.update([target, abs_s, f"-{abs_s}", f"-100{abs_s}"])
                    if abs_s.startswith("100") and len(abs_s) > 5:
                        short_s = abs_s[3:]
                        protected_ids.update([short_s, f"-{short_s}", f"-100{short_s}"])
                else:
                    protected_usernames.add(target.lstrip('@').lower())
        except Exception:
            pass
        conn.close()
    except Exception:
        pass
    return protected_ids, protected_usernames


def add_blocked_user_smart(user_id=None, username=None, reason=None):
    """
    Надежное добавление пользователя в ЧС.
    Корректно обрабатывает ID, @username или оба сразу.
    Защищает ID/username самих отслеживаемых чатов-источников от случайного бана.
    """
    try:
        protected_ids, protected_usernames = get_protected_source_identifiers()

        norm_id_1, norm_u_1 = normalize_identifier(user_id)
        norm_id_2, norm_u_2 = normalize_identifier(username)

        final_id = norm_id_1 or norm_id_2
        final_username = norm_u_2 or norm_u_1

        if final_id and str(final_id) in protected_ids:
            print(f"⚠️ Защита от бана источника: ID {final_id} принадлежит отслеживаемому чату, игнорируем")
            final_id = None
        if final_username and final_username.lower() in protected_usernames:
            print(f"⚠️ Защита от бана источника: @{final_username} принадлежит отслеживаемому чату, игнорируем")
            final_username = None

        if not final_id and not final_username:
            return False

        # В таблице blocked_users колонка user_id является UNIQUE NOT NULL.
        # Если числового ID нет, храним там нижний регистр username без @.
        db_user_id = str(final_id) if final_id else final_username

        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()

        # Проверяем существующую запись по ID или по username
        if final_id and final_username:
            cursor.execute(
                "SELECT id, user_id, username FROM blocked_users WHERE user_id = ? OR LOWER(user_id) = ? OR LOWER(username) = ?",
                (str(final_id), final_username, final_username)
            )
        elif final_id:
            cursor.execute("SELECT id, user_id, username FROM blocked_users WHERE user_id = ?", (str(final_id),))
        else:
            cursor.execute(
                "SELECT id, user_id, username FROM blocked_users WHERE LOWER(user_id) = ? OR LOWER(username) = ?",
                (final_username, final_username)
            )

        existing = cursor.fetchone()
        if existing:
            row_id, ex_uid, ex_uname = existing
            new_uid = str(final_id) if final_id else ex_uid
            new_uname = final_username or ex_uname
            cursor.execute(
                "UPDATE blocked_users SET user_id = ?, username = ?, reason = COALESCE(?, reason) WHERE id = ?",
                (new_uid, new_uname, reason, row_id)
            )
            conn.commit()
            conn.close()
            return True

        cursor.execute(
            "INSERT INTO blocked_users (user_id, username, reason) VALUES (?, ?, ?)",
            (db_user_id, final_username, reason)
        )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"❌ Ошибка add_blocked_user_smart: {e}")
        return False


def is_user_blocked_smart(user_id=None, username=None, extra_ids=None, extra_usernames=None, raw_text=None, html_text=None, source_username=None):
    """
    Универсальная проверка ЧС по всем возможным идентификаторам:
    - ID отправителя / пересланного сообщения / реплая
    - Username отправителя
    - Контакт для связи и ВСЕ упомянутые @username / t.me/... / tg://user?id=... внутри текста
    Автоматически исключает ID/username самих отслеживаемых чатов-источников.
    """
    try:
        protected_ids, protected_usernames = get_protected_source_identifiers()
        if source_username:
            protected_usernames.add(source_username.lstrip('@').lower())

        ids_to_check = set()
        usernames_to_check = set()

        def collect(val):
            if not val:
                return
            nid, nu = normalize_identifier(val)
            if nid and str(nid) not in protected_ids:
                ids_to_check.add(nid)
            if nu and nu not in protected_usernames:
                usernames_to_check.add(nu)

        collect(user_id)
        collect(username)

        if extra_ids:
            for x in extra_ids:
                collect(x)
        if extra_usernames:
            for x in extra_usernames:
                collect(x)

        if raw_text or html_text:
            ext_u, ext_id, _ = extract_all_contacts_and_ids(raw_text or "", html_text or "", source_username=source_username)
            for u in ext_u:
                if u not in protected_usernames:
                    usernames_to_check.add(u)
            for i in ext_id:
                if str(i) not in protected_ids:
                    ids_to_check.add(i)

        if not ids_to_check and not usernames_to_check:
            return False

        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()

        # Расширяем числовые ID вариантами (-100..., без -100 и т.д.)
        id_candidates = set()
        for item in ids_to_check:
            s_id = str(item).strip()
            if s_id in protected_ids:
                continue
            id_candidates.add(s_id)
            if s_id.lstrip('-').isdigit():
                abs_str = s_id.lstrip('-')
                id_candidates.update([abs_str, f"-{abs_str}", f"-100{abs_str}"])
                if abs_str.startswith('100') and len(abs_str) > 5:
                    raw_id = abs_str[3:]
                    id_candidates.update([raw_id, f"-{raw_id}", f"-100{raw_id}"])

        id_candidates -= protected_ids

        if id_candidates:
            placeholders = ','.join('?' for _ in id_candidates)
            cursor.execute(f"SELECT user_id, username FROM blocked_users WHERE user_id IN ({placeholders})", list(id_candidates))
            row = cursor.fetchone()
            if row:
                conn.close()
                return True

        if usernames_to_check:
            uname_candidates = set()
            for u in usernames_to_check:
                clean_u = u.lstrip('@').lower()
                if clean_u in protected_usernames:
                    continue
                uname_candidates.add(clean_u)
                uname_candidates.add(f"@{clean_u}")

            if uname_candidates:
                placeholders = ','.join('?' for _ in uname_candidates)
                params = list(uname_candidates) + list(uname_candidates)
                cursor.execute(
                    f"SELECT user_id, username FROM blocked_users WHERE LOWER(username) IN ({placeholders}) OR LOWER(user_id) IN ({placeholders})",
                    params
                )
                row = cursor.fetchone()
                if row:
                    conn.close()
                    return True

        conn.close()
        return False
    except Exception as e:
        print(f"❌ Ошибка проверки ЧС (is_user_blocked_smart): {e}")
        return False


# ==================== НОРМАЛИЗАЦИЯ ОМОГЛИФОВ (АНТИ-ОБХОД) ====================

LATIN_TO_CYRILLIC = str.maketrans({
    'a': 'а', 'c': 'с', 'e': 'е', 'o': 'о', 'p': 'р', 'x': 'х', 'y': 'у',
    'k': 'к', 'm': 'м', 'h': 'н', 'b': 'в', 't': 'т'
})


def normalize_for_analysis(text):
    """
    Убирает невидимые символы, приводит к нижнему регистру и заменяет латинские буквы-двойники
    внутри кириллических слов, чтобы спамеры не обходили стоп-слова.
    """
    if not text:
        return ""
    # Удаляем zero-width и управляющие символы
    cleaned = re.sub(r'[\u200b\u200c\u200d\u2060\ufeff\u00ad]', '', text)
    cleaned = cleaned.lower()

    # Если в слове есть хотя бы одна кириллическая буква — переводим латинские омоглифы в кириллицу
    def fix_mixed_word(match):
        word = match.group(0)
        if re.search(r'[а-яё]', word):
            return word.translate(LATIN_TO_CYRILLIC)
        return word

    cleaned = re.sub(r'[a-zа-яё0-9_]+', fix_mixed_word, cleaned)
    return cleaned


# ==================== ЭВРИСТИКА СКАМ-КОНТАКТОВ ====================

def is_contact_scammy(username):
    if not username:
        return False
    _, clean = normalize_identifier(username)
    if not clean:
        clean = str(username).lstrip('@').strip().lower()
    if len(clean) < 4:
        return False

    digits = sum(c.isdigit() for c in clean)
    if digits >= 6 or (digits / len(clean)) > 0.55:
        return True

    scam_Account_words = [
        'crypto', 'invest', 'p2p', 'arbitr', 'casino', 'bet', 'drop',
        'dark', 'escort', 'kassa', 'obmen', 'signals', 'pump', 'airdrop',
        'zarabotok', 'dengi', 'vyplaty', 'temka', 'shema'
    ]
    for sw in scam_Account_words:
        if sw in clean:
            return True

    # Случайный набор согласных подряд (6+ согласных без гласных)
    consonants = set("bcdfghjklmnpqrstvwxz")
    max_cons = 0
    curr_cons = 0
    for ch in clean:
        if ch in consonants:
            curr_cons += 1
            max_cons = max(max_cons, curr_cons)
        else:
            curr_cons = 0
    if max_cons >= 6:
        return True

    return False


# ==================== УМНЫЙ СКОРИНГ ВАКАНСИЙ (АНТИ-СКАМ / АНТИ-РЕЗЮМЕ / ДУБЛИ) ====================

# 1. Жесткие скам/серые схемы (мгновенный бан в умном фильтре)
HARD_SCAM_PATTERNS = [
    r'пушкинск[а-яё]+\s+карт',
    r'верификац[а-яё]+\s+(?:аккаунт|кошел|бирж|авито|avito|binance|bybit|okx)',
    r'аренд[а-яё]+\s+(?:аккаунт|авито|avito|ватсап|whatsapp|кабинет|карт)',
    r'(?:дроп|дроповод|залив|обнал|процессинг|п2п|p2p\s*связк|арбитраж\s*крипт)',
    r'(?:казино|букмекер|ставк[аи]\s+на\s+спорт|гемблинг|gambling|betting|1win|1xbet)',
    r'(?:эскорт|вебкам|webcam|onlyfans|онлифанс|массаж\s+с\s+продолжением|интим)',
    r'(?:написан[а-яё]+|оставля[а-яё]+|накрутк[а-яё]+)\s+отзыв[а-яё]+\s+(?:на\s+)?(?:авито|яндекс|гугл|2гис|wildberries|ozon)',
    r'отзыв[а-яё]+\s+за\s+\d+\s*(?:р|руб|₽)',
    r'легкие\s+деньги\s+без\s+вложений',
    r'пассивн[а-яё]+\s+доход\s+от\s+\d+',
    r'обучени[ея]\s+с\s+нуля\s+до\s+результата\s+бесплатно',
    r'переходи\s+в\s+(?:шапку\s+профиля|био|лс\s+и\s+пиши\s+\+)',
    r'пиши\s+в\s+лс\s+слово\s+["«]?[а-яa-z0-9]+["»]?',
    r'пиши\s+["«]?\+["»]?\s+в\s+лс',
]

# 2. Подозрительные фразы легкого заработка (штрафные баллы)
SOFT_SCAM_PHRASES = [
    'без опыта и вложений',
    'выплаты каждый день',
    'ежедневные выплаты на карту',
    'работа с телефона 2-3 часа',
    'уделять 1-2 часа в день',
    'доход не ограничен',
    'гарантированный доход с первого дня',
    'не продажи и не косметика',
    'легальный заработок в интернете',
    'возраст от 16 лет',
    'возраст не важен',
    'пишите + в личку',
]

# 3. Маркеры резюме / саморекламы фрилансеров ("я ищу работу", а не "мы ищем сотрудника")
RESUME_PATTERNS = [
    r'(?:^|\n|\s)ищу\s+(?:работу|подработку|заказы|проекты|клиентов|команду)(?:\s|$|[.,!])',
    r'(?:^|\n|\s)в\s+поиске\s+(?:работы|подработки|заказов|проектов|клиентов)(?:\s|$|[.,!])',
    r'(?:^|\n|\s)(?:возьму|беру)\s+(?:на\s+монтаж|проекты|заказы|канал\s+под\s+ключ|в\s+работу)',
    r'(?:^|\n|\s)(?:сделаю|выполню|смонтирую|напишу|сверстаю|оформлю|нарисую)\s+(?:для\s+вас|быстро|качественно|видео|рилс|reels|шортс|дизайн|превью|лендинг|сайт|бота)',
    r'(?:^|\n|\s)(?:предлагаю|предоставляю|оказываю)\s+услуги\b',
    r'(?:^|\n|\s)(?:моё|мое)\s+портфолио\b',
    r'(?:^|\n|\s)мой\s+(?:прайс|стек|опыт\s+работы)\b',
    r'(?:^|\n|\s)обо\s+мне\s*:',
    r'(?:^|\n|\s)открыт(?:а)?\s+к\s+(?:предложениям|сотрудничеству|новым\s+проектам)',
    r'(?:^|\n|\s)помогу\s+(?:вам|вашему\s+бизнесу|с\s+монтажом|с\s+продвижением|с\s+дизайном)',
    r'#резюме\b',
    r'#ищуработу\b',
    r'#поискработы\b',
]

# 4. Положительные маркеры реального работодателя / вакансии
EMPLOYER_STRONG_MARKERS = [
    r'\b(?:ищем|мы\s+ищем|разыскиваем|приглашаем)\b',
    r'\b(?:требуется|требуются)\b',
    r'\b(?:нужен|нужна|нужны)\s+[а-яёa-z]+',
    r'\bоткрыта\s+(?:вакансия|позиция)\b',
    r'\bв\s+(?:поиске|команду\s+нужен|команду\s+требуется|штат\s+требуется)\b',
    r'#вакансия\b',
]

EMPLOYER_STRUCTURE_MARKERS = [
    r'\b(?:обязанности|задачи|что\s+нужно\s+делать|предстоит\s+делать)\s*[:\-—]',
    r'\b(?:требования|ожидания|что\s+мы\s+ждем|от\s+вас|навыки)\s*[:\-—]',
    r'\b(?:условия|мы\s+предлагаем|что\s+предлагаем|от\s+нас)\s*[:\-—]',
    r'\b(?:оплата|зарплата|зп|бюджет|ставка|оклад|вознаграждение|гонорар|фикс)\s*[:\-—]?',
    r'\b(?:тестовое\s+задание|отклик|присылайте\s+портфолио|присылайте\s+резюме|форма\s+для\s+отклика)\b',
    r'\b(?:удаленка|удаленно|полная\s+занятость|частичная\s+занятость|проектная\s+работа|фуллтайм|full-?time|part-?time)\b',
]


def compute_text_fingerprint(text):
    """Создает нормализованный отпечаток текста вакансии для поиска кросс-пост дубликатов."""
    norm = normalize_for_analysis(text)
    # Убираем ссылки, упоминания, цифры и знаки препинания
    norm = re.sub(r'(?:https?://|t\.me/)\S+', '', norm)
    norm = re.sub(r'@[a-z0-9_]+', '', norm)
    norm = re.sub(r'[^а-яёa-z]', '', norm)
    if len(norm) < 25:
        return None
    return hashlib.sha256(norm.encode('utf-8')).hexdigest()


def check_and_record_dedup(text, author_key=None, chat_id=None, message_id=None, record=True):
    """
    Проверяет:
    1) Не приходил ли уже точно такой же текст (кросс-постинг по чатам) за последние 48 часов.
    2) Не спамит ли один и тот же автор/контакт слишком часто (более 2 постов за 6 часов).
    Возвращает (is_dup_or_spam: bool, reason: str | None)
    """
    fp = compute_text_fingerprint(text)
    if not fp:
        return False, None

    try:
        conn = sqlite3.connect(DB_FILE, timeout=30.0)
        cursor = conn.cursor()

        # Очистка старых хэшей (> 3 дней) и кулдаунов (> 12 часов)
        cursor.execute("DELETE FROM vacancy_hashes WHERE created_at < datetime('now', '-3 days')")
        cursor.execute("DELETE FROM author_cooldowns WHERE updated_at < datetime('now', '-12 hours')")

        # 1. Проверка глобального дубликата текста
        cursor.execute("SELECT chat_id, message_id FROM vacancy_hashes WHERE text_hash = ?", (fp,))
        dup_row = cursor.fetchone()
        if dup_row:
            conn.close()
            return True, "Дубликат текста (уже публиковалось в другом чате)"

        # 2. Проверка частоты публикаций от одного автора/контакта
        clean_author = None
        if author_key and "⚠️" not in str(author_key) and str(author_key) != "Не указан":
            clean_author = str(author_key).lstrip('@').strip().lower()

        if clean_author and len(clean_author) >= 3:
            cursor.execute(
                "SELECT last_text_hash, post_count FROM author_cooldowns WHERE author_key = ? AND updated_at > datetime('now', '-6 hours')",
                (clean_author,)
            )
            arow = cursor.fetchone()
            if arow:
                last_hash, post_count = arow
                if last_hash == fp:
                    conn.close()
                    return True, f"Повторная рассылка от автора ({clean_author})"
                if post_count >= 3:
                    conn.close()
                    return True, f"Слишком частая рассылка от одного контакта ({clean_author}: {post_count + 1}-й пост за 6ч)"

        # Записываем в БД
        if record:
            cursor.execute(
                "INSERT OR IGNORE INTO vacancy_hashes (text_hash, chat_id, message_id) VALUES (?, ?, ?)",
                (fp, str(chat_id or ""), int(message_id or 0))
            )
            if clean_author and len(clean_author) >= 3:
                cursor.execute('''
                    INSERT INTO author_cooldowns (author_key, last_text_hash, post_count, updated_at)
                    VALUES (?, ?, 1, CURRENT_TIMESTAMP)
                    ON CONFLICT(author_key) DO UPDATE SET
                        last_text_hash = excluded.last_text_hash,
                        post_count = author_cooldowns.post_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                ''', (clean_author, fp))
            conn.commit()

        conn.close()
        return False, None
    except Exception as e:
        print(f"⚠️ Ошибка дедупликации: {e}")
        return False, None


def evaluate_vacancy_smart(raw_text, contact=None, author_id=None, chat_id=None, message_id=None):
    """
    Комплексная оценка сообщения:
    Возвращает словарь:
    {
        "passed": bool,           # Прошла ли вакансия умный фильтр
        "score": int,             # Итоговый балл качества вакансии
        "reasons": list[str],     # Причины отклонения или плюсы/минусы
        "tech_comment": str       # Готовый HTML-блок технического комментария
    }
    """
    reasons = []
    score = 0

    if not raw_text or len(raw_text.strip()) < 45:
        reasons.append("Слишком короткий текст (< 45 символов)")
        return _build_eval_result(False, -10, reasons)

    norm_text = normalize_for_analysis(raw_text)

    # 1. Проверка контакта на скам-эвристику
    if contact and "⚠️" not in contact and contact != "Не указан":
        if is_contact_scammy(contact):
            reasons.append(f"Подозрительный скам-контакт ({contact})")
            return _build_eval_result(False, -15, reasons)

    # 2. Проверка на жесткий скам / серые схемы / казино / отзывы / крипто-связки
    for pattern in HARD_SCAM_PATTERNS:
        m = re.search(pattern, norm_text, flags=re.IGNORECASE)
        if m:
            matched_str = m.group(0).strip()
            reasons.append(f"Скам/серая схема («{matched_str}»)")
            return _build_eval_result(False, -20, reasons)

    # 3. Подсчет маркеров работодателя vs резюме/саморекламы
    employer_strong_hits = 0
    for pat in EMPLOYER_STRONG_MARKERS:
        if re.search(pat, norm_text, flags=re.IGNORECASE):
            employer_strong_hits += 1
            score += 3

    structure_hits = 0
    for pat in EMPLOYER_STRUCTURE_MARKERS:
        if re.search(pat, norm_text, flags=re.IGNORECASE):
            structure_hits += 1
            score += 2

    resume_hits = []
    for pat in RESUME_PATTERNS:
        m = re.search(pat, norm_text, flags=re.IGNORECASE)
        if m:
            resume_hits.append(m.group(0).strip())
            score -= 5

    # Если есть явные признаки резюме ("ищу работу", "возьму на монтаж", "мое портфолио") и нет структуры вакансии
    if resume_hits and employer_strong_hits == 0:
        sample = ", ".join(f"«{r}»" for r in resume_hits[:2])
        reasons.append(f"Похоже на резюме/саморекламу исполнителя ({sample})")
        return _build_eval_result(False, score, reasons)

    if len(resume_hits) >= 2 and structure_hits == 0:
        sample = ", ".join(f"«{r}»" for r in resume_hits[:2])
        reasons.append(f"Самореклама услуг ({sample})")
        return _build_eval_result(False, score, reasons)

    # 4. Проверка на размытые клише "легкого заработка"
    soft_scam_hits = []
    for phrase in SOFT_SCAM_PHRASES:
        if phrase in norm_text:
            soft_scam_hits.append(phrase)
            score -= 4

    if len(soft_scam_hits) >= 2:
        sample = ", ".join(f"«{s}»" for s in soft_scam_hits[:2])
        reasons.append(f"Признаки спам-объявления о легком заработке ({sample})")
        return _build_eval_result(False, score, reasons)

    # 5. Проверка КАПСА и избытка эмодзи
    letters = [c for c in raw_text if c.isalpha()]
    if len(letters) > 30:
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.65:
            score -= 4
            reasons.append("Злоупотребление CAPS LOCK")

    # 6. Оценка длины и содержательности
    clean_len = len(raw_text.strip())
    if clean_len >= 120:
        score += 2
    elif clean_len < 75 and structure_hits == 0 and employer_strong_hits == 0:
        score -= 3
        reasons.append("Мало конкретики / нет признаков вакансии")

    # Если совсем нет ни маркеров работодателя, ни структуры вакансии, и балл низкий
    if employer_strong_hits == 0 and structure_hits == 0 and score <= 0:
        reasons.append("Отсутствуют признаки вакансии (нет задач/условий/поиска сотрудника)")
        return _build_eval_result(False, score, reasons)

    if score < 2:
        if not reasons:
            reasons.append(f"Низкий рейтинг структуры вакансии (балл: {score})")
        return _build_eval_result(False, score, reasons)

    # 7. Глобальная дедупликация и анти-спам кулдаун автора
    # ВАЖНО: кулдаун по автору применяем только к реальным личным @username (не к ID канала/бота)
    author_key = None
    if contact and "⚠️" not in contact and contact != "Не указан":
        clean_c = str(contact).strip()
        if clean_c.startswith('@') and not clean_c.lower().endswith('bot'):
            author_key = clean_c

    is_dup, dup_reason = check_and_record_dedup(
        raw_text,
        author_key=author_key,
        chat_id=chat_id,
        message_id=message_id,
        record=True
    )
    if is_dup:
        reasons.append(dup_reason)
        return _build_eval_result(False, score, reasons)

    return _build_eval_result(True, score, reasons)


def _build_eval_result(passed, score, reasons):
    reason_str = "; ".join(reasons) if reasons else "Структура вакансии в норме"
    if passed:
        tech_comment = (
            f"<blockquote>✅ <b>[ТЕСТ ФИЛЬТРА]: Вакансия прошла проверку</b> "
            f"(Оценка: +{score})</blockquote>"
        )
    else:
        tech_comment = (
            f"<blockquote>⚠️ <b>[ТЕСТ ФИЛЬТРА]: Эта вакансия БЫ НЕ ПРИШЛА!</b>\n"
            f"❌ <b>Причина:</b> {html.escape(reason_str)}</blockquote>"
        )
    return {
        "passed": passed,
        "score": score,
        "reasons": reasons,
        "tech_comment": tech_comment
    }
