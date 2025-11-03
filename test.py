import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, timezone
import os
import logging
import sys
import time
import threading
import requests

from flask import Flask, request

import telebot
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv
from pathlib import Path
# اضافه: psycopg2
import psycopg2
from psycopg2 import sql
from psycopg2 import extras
from psycopg2.pool import ThreadedConnectionPool


env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)
# ---------------- Config / Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------------- Environment / Self-ping config ----------------
API_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
if not API_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN environment variable is not set")

SELF_URL = os.getenv("SELF_URL")
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "300"))
PING_SECRET = os.getenv("PING_SECRET")
FLASK_PORT = int(os.getenv("PORT", "5000"))

# Self-ping verify option: "1" (default) => verify SSL, "0" => don't verify (for testing)
SELF_PING_VERIFY = os.getenv("SELF_PING_VERIFY", "1") != "0"

# SMTP env vars (for start email)
SMTP_HOST = os.getenv("SMTP_HOST")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
EMAIL_TO = os.getenv("EMAIL_TO")

CHANNEL_ID = os.getenv("CHANNEL_ID", "-1002984288636")
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "https://t.me/channelforfrinds")

bot = telebot.TeleBot(API_TOKEN)
ping_app = Flask(__name__)

CATEGORIES = [
    "mylf", "step sis", "step mom", "work out", "russian",
    "big ass", "big tits", "free us", "Sweetie Fox R", "foot fetish", "arab", "asian", "anal", "BBC", "وطنی", "None"
]

user_categories = {}
user_pagination = {}
user_lucky_search = {}

# ---------------- Postgres (Threaded pool) ----------------
_db_pool = None

def init_db_pool():
    global _db_pool
    if _db_pool:
        return

    # Prefer DATABASE_URL if provided (common on Liara)
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        # psycopg2 can accept a URL directly
        try:
            _db_pool = ThreadedConnectionPool(1, 10, dsn=database_url)
            logger.info("Postgres pool created from DATABASE_URL")
            return
        except Exception as e:
            logger.error(f"Couldn't create pool from DATABASE_URL: {e}")
            raise

    # Otherwise build from individual env vars
    pg_host = os.getenv("PG_HOST")
    pg_port = os.getenv("PG_PORT", "5432")
    pg_db = os.getenv("PG_DB")
    pg_user = os.getenv("PG_USER")
    pg_pass = os.getenv("PG_PASS")
    pg_sslmode = os.getenv("PG_SSLMODE", None)  # e.g. "require" or None

    if not (pg_host and pg_db and pg_user and pg_pass):
        raise RuntimeError("Postgres connection info not fully provided (set DATABASE_URL or PG_HOST/PG_DB/PG_USER/PG_PASS)")

    conn_str_parts = [
        f"host={pg_host}",
        f"port={pg_port}",
        f"dbname={pg_db}",
        f"user={pg_user}",
        f"password={pg_pass}"
    ]
    if pg_sslmode:
        conn_str_parts.append(f"sslmode={pg_sslmode}")
    conn_str = " ".join(conn_str_parts)

    try:
        _db_pool = ThreadedConnectionPool(1, 10, dsn=conn_str)
        logger.info("Postgres pool created from PG_* env vars")
    except Exception as e:
        logger.error(f"Couldn't create Postgres pool: {e}")
        raise

def get_conn():
    global _db_pool
    if _db_pool is None:
        init_db_pool()
    conn = _db_pool.getconn()
    # use autocommit=False and we will commit manually where needed
    return conn

def put_conn(conn, close=False):
    global _db_pool
    if _db_pool is None:
        return
    try:
        if close:
            conn.close()
        else:
            _db_pool.putconn(conn)
    except Exception as e:
        logger.debug(f"Error returning connection to pool: {e}")

# ---------------- Email helper ----------------
def send_start_email(user):
    """
    user: telebot.types.User object (message.from_user)
    ارسال ایمیل شامل username (اگر موجود باشد) یا نام و id، و زمان استارت
    """
    smtp_host = SMTP_HOST
    smtp_port = SMTP_PORT
    smtp_user = SMTP_USER
    smtp_pass = SMTP_PASS
    email_to = EMAIL_TO

    if not (smtp_host and smtp_user and smtp_pass and email_to):
        logger.warning("SMTP یا EMAIL_TO تنظیم نشده‌اند — ارسال ایمیل غیرفعال است.")
        return

    # اطلاعات کاربر
    username = getattr(user, 'username', None)
    first_name = getattr(user, 'first_name', '')
    last_name = getattr(user, 'last_name', '')
    user_id = getattr(user, 'id', None)

    if username:
        user_ident = f"@{username}"
    else:
        user_ident = f"{first_name} {last_name} (id: {user_id})"

    # زمان با timezone محلی به صورت ISO
    start_time = datetime.now(timezone.utc).astimezone().isoformat()

    subject = f"ربات: کاربر جدید استارت زد — {user_ident}"
    body = f"""یک کاربر ربات را استارت کرد.

کاربر: {user_ident}
آی‌دی کاربر: {user_id}
زمان استارت: {start_time}

این پیام توسط ربات ارسال شده است.
"""

    try:
        msg = EmailMessage()
        msg["From"] = smtp_user
        msg["To"] = email_to
        msg["Subject"] = subject
        msg.set_content(body)

        # اگر پورت 465: SSL، در غیر این صورت از STARTTLS استفاده می‌کنیم
        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                try:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                except Exception:
                    logger.debug("STARTTLS failed or not supported, trying plain login")
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)

        logger.info(f"Start email sent for user {user_ident}")
    except Exception as e:
        logger.error(f"خطا در ارسال ایمیل برای کاربر {user_ident}: {e}")


# ---------- Database (Postgres) ----------
def create_table():
    """
    ایجاد جدول videos در Postgres با همان ساختار.
    از ThreadedConnectionPool استفاده می‌کنیم تا در تردها امن باشد.
    """
    init_db_pool()
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        # create safe category list for CHECK
        # توجه: در SQL از علامت ' استفاده می‌کنیم، امن شد با sql.Literal در psycopg2.sql
        # اما برای سادگی و چون CATEGORIES تحت کنترل ماست، از joining امن استفاده می‌کنیم.
        cat_list_sql = ",".join([f"'{c}'" for c in CATEGORIES])
        create_sql = f'''
            CREATE TABLE IF NOT EXISTS videos
            (
                video_id TEXT PRIMARY KEY,
                user_id BIGINT,
                category TEXT CHECK (category IN ({cat_list_sql})),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        '''
        cur.execute(create_sql)
        conn.commit()
        cur.close()
        logger.info("Postgres table 'videos' ensured.")
    except Exception as e:
        logger.error(f"Error creating table: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if conn:
            put_conn(conn)


# ---------- Helpers for callback-safe category codes ----------
def encode_category_for_callback(cat_text: str) -> str:
    # replace spaces with double underscore to keep a reversible safe token
    return "cat" + cat_text.replace(" ", "__")


def decode_category_from_callback(cat_code: str) -> str:
    if cat_code.startswith("cat"):
        return cat_code[3:].replace("__", " ")
    return cat_code


# ---------- Channel join helpers ----------
def is_member(user_id):
    try:
        user_info = bot.get_chat_member(CHANNEL_ID, user_id)
        return user_info.status in ['creator', 'administrator', 'member']
    except Exception as e:
        logger.error(f"Error checking membership for user {user_id}: {e}")
        return False


def create_join_channel_keyboard():
    markup = InlineKeyboardMarkup(row_width=1)
    join_button = InlineKeyboardButton('📢 عضویت در کانال', url=CHANNEL_LINK)
    check_button = InlineKeyboardButton('✅ بررسی عضویت', callback_data='check_membership')
    markup.add(join_button, check_button)
    return markup

def create_video_keyboard():
    """
    ایجاد اینلاین کیبورد برای ویدیوها با اسم و آدرس ربات
    """
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("bylmax", url="https://t.me/bylmax_bot"))
    return markup
# ---------- Start / Home ----------
@bot.message_handler(commands=['start'])
def start_handler(message):
    user_id = message.from_user.id

    if not is_member(user_id):
        bot.send_message(
            message.chat.id,
            '👋 سلام!\n\n'
            'برای استفاده از ربات، لطفاً در کانال ما عضو شوید:\n'
            'پس از عضویت، دکمه "بررسی عضویت" را بزنید.',
            reply_markup=create_join_channel_keyboard()
        )
        return

    # ارسال ایمیل در یک ترد جداگانه (تا بلوک نشه)
    try:
        threading.Thread(target=send_start_email, args=(message.from_user,), daemon=True).start()
    except Exception as e:
        logger.warning(f"Couldn't start email thread: {e}")

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add('تماشای فیلم ها 🎥', '🎲 تماشای شانسی', '/home 🏠')
    bot.send_message(message.chat.id, "سلام 👋\nبه ربات bylmax خوش اومدی ", reply_markup=markup)


@bot.callback_query_handler(func=lambda call: call.data == 'check_membership')
def check_membership_callback(call):
    user_id = call.from_user.id
    if is_member(user_id):
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=(
                    '🎉 عالی!\n\n'
                    '✅ عضویت شما تأیید شد.\n'
                    'اکنون میتوانید از امکانات ربات استفاده کنید.'
                )
            )
        except Exception as e:
            logger.warning(f"Couldn't edit message for membership check: {e}")

        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add('تماشای فیلم ها 🎥', '🎲 تماشای شانسی', '/home 🏠')
        bot.send_message(call.message.chat.id, 'خوش آمدید! از امکانات ربات لذت ببرید. 😊', reply_markup=markup)
    else:
        bot.answer_callback_query(call.id, '❌ هنوز در کانال عضو نشدید! لطفاً ابتدا عضو شوید.', show_alert=True)


@bot.message_handler(commands=['home', 'home 🏠'])
def home(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add('تماشای فیلم ها 🎥', '🎲 تماشای شانسی', '/home 🏠')
    bot.send_message(message.chat.id, "به خانه خوش آمدید", reply_markup=markup)


def home_from_id(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add('تماشای فیلم ها 🎥', '🎲 تماشای شانسی', '/home 🏠')
    bot.send_message(chat_id, "به خانه خوش آمدید", reply_markup=markup)


# ---------- Lucky (random) ----------
@bot.message_handler(func=lambda message: message.text == '🎲 تماشای شانسی')
def lucky_search(message):
    user_id = message.from_user.id
    if not is_member(user_id):
        bot.send_message(message.chat.id, '⚠️ برای استفاده از این قابلیت باید در کانال عضو باشید.',
                         reply_markup=create_join_channel_keyboard())
        return

    # حذف ویدیوهای قبلی اگر وجود داشته باشند
    if user_id in user_lucky_search and 'message_ids' in user_lucky_search[user_id]:
        delete_messages(message.chat.id, user_lucky_search[user_id]['message_ids'])

    random_videos = get_random_videos(5)
    if not random_videos:
        bot.reply_to(message, "❌ هنوز هیچ ویدیویی در سیستم وجود ندارد!")
        return

    user_lucky_search[user_id] = {'current_videos': random_videos, 'message_ids': [], 'chat_id': message.chat.id}
    for i, video in enumerate(random_videos):
        try:
            sent_msg = send_protected_video(message.chat.id, video[0], caption=f"ویدیو شانسی {i + 1}")
            user_lucky_search[user_id]['message_ids'].append(sent_msg.message_id)
        except Exception as e:
            logger.error(f"خطا در ارسال ویدیو: {e}")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎲 شانس مجدد", callback_data="lucky_again"))
    sent_msg = bot.send_message(message.chat.id, "۵ ویدیوی تصادفی برای شما نمایش داده شد!", reply_markup=markup)
    user_lucky_search[user_id]['message_ids'].append(sent_msg.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "lucky_again")
def handle_lucky_again(call):
    user_id = call.from_user.id
    if not is_member(user_id):
        bot.answer_callback_query(call.id, "⚠️ باید ابتدا در کانال عضو شوید.", show_alert=True)
        return

    # حذف ویدیوهای قبلی
    if user_id in user_lucky_search and 'message_ids' in user_lucky_search[user_id]:
        delete_messages(call.message.chat.id, user_lucky_search[user_id]['message_ids'])

    random_videos = get_random_videos(5)
    if not random_videos:
        bot.answer_callback_query(call.id, "❌ هیچ ویدیویی در سیستم وجود ندارد!")
        return

    user_lucky_search[user_id] = {'current_videos': random_videos, 'message_ids': [], 'chat_id': call.message.chat.id}
    for i, video in enumerate(random_videos):
        try:
            # استفاده از تابع send_protected_video برای consistency
            sent_msg = send_protected_video(call.message.chat.id, video[0], caption=f"ویدیو شانسی {i + 1}")
            user_lucky_search[user_id]['message_ids'].append(sent_msg.message_id)
        except Exception as e:
            logger.error(f"خطا در ارسال ویدیو: {e}")

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎲 شانس مجدد", callback_data="lucky_again"))
    sent_msg = bot.send_message(call.message.chat.id, "۵ ویدیوی تصادفی جدید برای شما نمایش داده شد!",
                                reply_markup=markup)
    user_lucky_search[user_id]['message_ids'].append(sent_msg.message_id)
    bot.answer_callback_query(call.id)


def get_random_videos(limit=5):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT video_id FROM videos ORDER BY RANDOM() LIMIT %s', (limit,))
        videos = cur.fetchall()
        cur.close()
        return videos
    except Exception as e:
        logger.error(f"Error fetching random videos: {e}")
        return []
    finally:
        if conn:
            put_conn(conn)


# ---------- Upload flow ----------
@bot.message_handler(func=lambda message: message.text == '📤 ارسال محتوا')
def request_video(message):
    user_id = message.from_user.id
    if not is_member(user_id):
        bot.send_message(message.chat.id, '⚠️ برای ارسال ویدیو باید در کانال عضو باشید.',
                         reply_markup=create_join_channel_keyboard())
        return

    if user_id in user_categories:
        category = user_categories[user_id]
        bot.reply_to(message, f"دسته‌بندی فعلی: {category}. لطفاً ویدیوی خود را ارسال کنید:")
    else:
        show_category_selection(message)


@bot.message_handler(func=lambda message: message.text == '🔄 تغییر دسته‌بندی')
def change_category(message):
    show_category_selection(message)


def show_category_selection(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(*CATEGORIES)
    markup.add('/home')
    msg = bot.reply_to(message, "لطفاً دسته‌بندی ویدیو را انتخاب کنید:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_category_selection)


def process_category_selection(message):
    if message.text == '/home':
        home(message)
        return

    chosen = message.text
    if chosen in CATEGORIES:
        user_categories[message.from_user.id] = chosen
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add('🔄 تغییر دسته‌بندی', '/home 🏠')
        bot.send_message(message.chat.id,
                         f"✅ دسته‌بندی {chosen} انتخاب شد. اکنون می‌توانید ویدیوی خود را ارسال کنید.",
                         reply_markup=markup)
    else:
        bot.reply_to(message, "❌ دسته‌بندی نامعتبر است. لطفاً یکی از گزینه‌های موجود را انتخاب کنید:")
        show_category_selection(message)


# ---------- Viewing videos (global per-category + pagination) ----------
@bot.message_handler(func=lambda message: message.text == 'تماشای فیلم ها 🎥')
def show_my_videos(message):
    user_id = message.from_user.id
    if not is_member(user_id):
        bot.send_message(message.chat.id, '⚠️ برای مشاهده ویدیوها باید در کانال عضو باشید.',
                         reply_markup=create_join_channel_keyboard())
        return

    # نمایش دسته‌بندی‌ها برای مشاهده
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
    markup.add(*CATEGORIES)
    markup.add( '/home')
    msg = bot.reply_to(message,
                       "لطفاً دسته‌بندی مورد نظر برای مشاهده ویدیوها را انتخاب کنید (ویدیوهای تمام کاربران نمایش داده می‌شوند):",
                       reply_markup=markup)
    bot.register_next_step_handler(msg, process_category_for_viewing)


def process_category_for_viewing(message):
    if message.text == '/home':
        home(message)
        return

    user_id = message.from_user.id

    # حذف پیام‌های قبلی اگر وجود داشته باشند
    if user_id in user_pagination and 'message_ids' in user_pagination[user_id]:
        delete_messages(message.chat.id, user_pagination[user_id]['message_ids'])

    user_pagination[user_id] = {'page': 0, 'category': None, 'all_videos': False, 'message_ids': [],
                                'chat_id': message.chat.id}

    if message.text == '📋 همه ویدیوها':
        user_pagination[user_id]['all_videos'] = True
        videos = get_user_videos(user_id)
        if videos:
            send_videos_paginated(user_id, message.chat.id, videos, page=0, page_size=5)
        else:
            bot.reply_to(message, "❌ هنوز ویدیویی ارسال نکرده‌اید")
            home(message)
    else:
        chosen = message.text
        if chosen in CATEGORIES:
            user_pagination[user_id]['category'] = chosen
            videos = get_videos_by_category(chosen)  # returns (video_id, user_id)
            if videos:
                send_videos_paginated(user_id, message.chat.id, videos, page=0, page_size=5, category=chosen,
                                      global_category=True)
            else:
                bot.reply_to(message, f"❌ ویدیویی در دسته‌بندی {chosen} موجود نیست")
                home(message)
        else:
            bot.reply_to(message, "❌ لطفاً یکی از دسته‌بندی‌های موجود را انتخاب کنید:")
            show_my_videos(message)


def send_videos_paginated(user_id, chat_id, videos, page=0, page_size=5, category=None, global_category=False):
    if not videos:
        return

    total_videos = len(videos)
    total_pages = (total_videos + page_size - 1) // page_size
    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total_videos)

    # حذف پیام‌های قبلی اگر وجود داشته باشند (برای همه صفحات)
    if user_id in user_pagination and 'message_ids' in user_pagination[user_id]:
        delete_messages(chat_id, user_pagination[user_id]['message_ids'])
        user_pagination[user_id]['message_ids'] = []

    for i in range(start_idx, end_idx):
        video_info = videos[i]
        video_id = None
        caption_parts = []
        if isinstance(video_info, tuple):
            if len(video_info) >= 2:
                second = video_info[1]
                if isinstance(second, int):
                    video_id = video_info[0]
                    if len(video_info) > 2:
                        caption_parts.append(f"دسته‌بندی: {video_info[2]}")
                else:
                    video_id = video_info[0]
                    caption_parts.append(f"دسته‌بندی: {second}")
            else:
                video_id = video_info[0]
        else:
            video_id = video_info

        caption = " - ".join(caption_parts) if caption_parts else (f"دسته‌بندی: {category}" if category else "")
        try:
            sent_msg = send_protected_video(chat_id, video_id, caption=caption or None)
            user_pagination[user_id]['message_ids'].append(sent_msg.message_id)
        except Exception as e:
            logger.error(f"خطا در ارسال ویدیو: {e}")
            error_msg = bot.send_message(chat_id, f"خطا در نمایش ویدیو: {video_id}")
            user_pagination[user_id]['message_ids'].append(error_msg.message_id)

    if end_idx < total_videos:
        markup = types.InlineKeyboardMarkup()
        if category:
            encoded = encode_category_for_callback(category)
            next_cb = f"next|{encoded}|{page + 1}"
            next_button = types.InlineKeyboardButton("➡️ ویدیوهای بعدی", callback_data=next_cb)
            markup.add(next_button)
            page_info = f"\n\nصفحه {page + 1} از {total_pages} - نمایش {start_idx + 1} تا {end_idx} از {total_videos} ویدیو"
            info_msg = bot.send_message(chat_id, f"ویدیوهای دسته‌بندی {category}{page_info}", reply_markup=markup)
            user_pagination[user_id]['message_ids'].append(info_msg.message_id)
        else:
            next_cb = f"next|all|{page + 1}"
            next_button = types.InlineKeyboardButton("➡️ ویدیوهای بعدی", callback_data=next_cb)
            markup.add(next_button)
            page_info = f"\n\nصفحه {page + 1} از {total_pages} - نمایش {start_idx + 1} تا {end_idx} از {total_videos} ویدیو"
            info_msg = bot.send_message(chat_id, f"همه ویدیوها{page_info}", reply_markup=markup)
            user_pagination[user_id]['message_ids'].append(info_msg.message_id)
    else:
        page_info = f"\n\nصفحه {page + 1} از {total_pages} - نمایش {start_idx + 1} تا {end_idx} از {total_videos} ویدیو"
        if category:
            end_msg = bot.send_message(chat_id, f"✅ تمام ویدیوهای دسته‌بندی {category} نمایش داده شد.{page_info}")
            user_pagination[user_id]['message_ids'].append(end_msg.message_id)
        else:
            end_msg = bot.send_message(chat_id, f"✅ تمام ویدیوها نمایش داده شد.{page_info}")
            user_pagination[user_id]['message_ids'].append(end_msg.message_id)
        home_from_id(chat_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith('next|'))
def handle_next_button(call):
    user_id = call.from_user.id
    parts = call.data.split('|')
    if len(parts) != 3:
        bot.answer_callback_query(call.id, "داده نامعتبر.")
        return

    _, category_code, page_str = parts
    try:
        page = int(page_str)
    except ValueError:
        bot.answer_callback_query(call.id, "داده نامعتبر.")
        return

    # حذف پیام فعلی (دکمه)
    try:
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except Exception as e:
        logger.debug(f"خطا در حذف پیام دکمه: {e}")

    # حذف پیام‌های ویدیوهای قبلی
    if user_id in user_pagination and 'message_ids' in user_pagination[user_id]:
        delete_messages(call.message.chat.id, user_pagination[user_id]['message_ids'])

    user_pagination[user_id]['page'] = page

    if category_code == 'all':
        videos = get_user_videos(user_id)
        user_pagination[user_id]['all_videos'] = True
        user_pagination[user_id]['category'] = None
        send_videos_paginated(user_id, call.message.chat.id, videos, page=page, page_size=5)
    else:
        category = decode_category_from_callback(category_code)
        if category not in CATEGORIES:
            bot.answer_callback_query(call.id, "دسته‌بندی نامعتبر.")
            return
        videos = get_videos_by_category(category)  # global
        user_pagination[user_id]['all_videos'] = False
        user_pagination[user_id]['category'] = category
        send_videos_paginated(user_id, call.message.chat.id, videos, page=page, page_size=5, category=category,
                              global_category=True)

    bot.answer_callback_query(call.id)


# ---------- Video content handler ----------
@bot.message_handler(content_types=['video'])
def get_video(message):
    user_id = message.from_user.id
    if not is_member(user_id):
        bot.send_message(message.chat.id, '⚠️ برای ارسال ویدیو باید در کانال عضو باشید.',
                         reply_markup=create_join_channel_keyboard())
        return

    video_id = message.video.file_id

    if user_id in user_categories:
        category = user_categories[user_id]
        if save_video_to_db(user_id, video_id, category):
            current_category = user_categories.get(user_id, "تعیین نشده")
            bot.reply_to(message,
                         f"✅ ویدیو در دسته‌بندی {category} ذخیره شد!\n\nدسته‌بندی فعلی: {current_category}\nبرای تغییر دسته‌بندی از دکمه '🔄 تغییر دسته‌بندی' استفاده کنید.")
        else:
            bot.reply_to(message, "❌ خطا در ذخیره‌سازی ویدیو")
    else:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add('تماشای فیلم ها 🎥', '🎲 تماشای شانسی', '/home 🏠')
        bot.send_message(message.chat.id, "❌ لطفاً ابتدا دسته‌بندی مورد نظر را انتخاب کنید.", reply_markup=markup)
        show_category_selection(message)


def save_video_to_db(user_id, video_id, category):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO videos (video_id, user_id, category)
            VALUES (%s, %s, %s)
            ON CONFLICT (video_id) DO UPDATE
              SET user_id = EXCLUDED.user_id,
                  category = EXCLUDED.category,
                  timestamp = CURRENT_TIMESTAMP
        ''', (video_id, user_id, category))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        logger.error(f"خطا در ذخیره‌سازی: {e}")
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn:
            put_conn(conn)


# ---------- DB query helpers ----------
def get_videos_by_category(category):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT video_id, user_id FROM videos WHERE category = %s ORDER BY timestamp DESC', (category,))
        videos = cur.fetchall()
        cur.close()
        return videos
    except Exception as e:
        logger.error(f"Error in get_videos_by_category: {e}")
        return []
    finally:
        if conn:
            put_conn(conn)


def get_user_videos(user_id):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT video_id, category FROM videos WHERE user_id = %s ORDER BY timestamp DESC', (user_id,))
        videos = cur.fetchall()
        cur.close()
        return videos
    except Exception as e:
        logger.error(f"Error in get_user_videos: {e}")
        return []
    finally:
        if conn:
            put_conn(conn)


def get_user_videos_by_category(user_id, category):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT video_id, category FROM videos WHERE user_id = %s AND category = %s ORDER BY timestamp DESC', (user_id, category))
        videos = cur.fetchall()
        cur.close()
        return videos
    except Exception as e:
        logger.error(f"Error in get_user_videos_by_category: {e}")
        return []
    finally:
        if conn:
            put_conn(conn)


def get_video_info(video_id):
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT user_id, category FROM videos WHERE video_id = %s', (video_id,))
        video = cur.fetchone()
        cur.close()
        return video
    except Exception as e:
        logger.error(f"Error in get_video_info: {e}")
        return None
    finally:
        if conn:
            put_conn(conn)


# ---------- Helper function to delete messages ----------
def delete_messages(chat_id, message_ids):
    """حذف پیام‌های قبلی بر اساس لیست message_ids"""
    for msg_id in message_ids:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logger.debug(f"خطا در حذف پیام {msg_id}: {e}")


# ---------- Admin ----------
@bot.message_handler(commands=['admin_control_for_manage_videos_and_more_text_for_Prevention_Access_normal_user'])
def admin(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add('📤 ارسال ویدیو', '🔄 تغییر دسته‌بندی')
    bot.send_message(message.chat.id, "به ربات مدیریت ویدیو خوش آمدید!", reply_markup=markup)


# ---------- Generic "catch-all" message handler ----------
@bot.message_handler(func=lambda message: True)
def handle_all_messages(message):
    user_id = message.from_user.id
    if not is_member(user_id):
        bot.send_message(message.chat.id, '⚠️ برای استفاده از ربات باید در کانال عضو باشید.',
                         reply_markup=create_join_channel_keyboard())
        return

    elif message.text == '📋 همه ویدیوها':
        user_pagination[user_id]['all_videos'] = True
        videos = get_user_videos(user_id)
        if videos:
            send_videos_paginated(user_id, message.chat.id, videos, page=0, page_size=5)
        else:
            bot.reply_to(message, "❌ هنوز ویدیویی ارسال نکرده‌اید")
            home(message)
    else:
        chosen = message.text
        if chosen in CATEGORIES:
            user_pagination[user_id]['category'] = chosen
            videos = get_videos_by_category(chosen)  # returns (video_id, user_id)
            if videos:
                send_videos_paginated(user_id, message.chat.id, videos, page=0, page_size=5, category=chosen,
                                      global_category=True)
            else:
                bot.reply_to(message, f"❌ ویدیویی در دسته‌بندی {chosen} موجود نیست")
                home(message)
        else:
            bot.reply_to(message, "❌ لطفاً یکی از دسته‌بندی‌های موجود را انتخاب کنید:")
            show_my_videos(message)


# ----------------- بوت راه‌اندازی -----------------
create_table()


# ---------- Flask / ping endpoint ----------
@ping_app.route("/ping", methods=["GET"])
def ping():
    if PING_SECRET:
        header_secret = request.headers.get("X-Ping-Secret")
        query_secret = request.args.get("secret")
        if header_secret == PING_SECRET or query_secret == PING_SECRET:
            return "pong", 200
        else:
            return "forbidden", 403
    return "pong", 200


def run_flask():
    try:
        ping_app.run(host="0.0.0.0", port=FLASK_PORT)
    except Exception as e:
        logger.error(f"Flask failed to start: {e}")


# ---------- Self-ping loop ----------
def self_ping_loop():
    if not SELF_URL:
        logger.info("SELF_URL not set. Self-ping disabled.")
        return

    ping_url = SELF_URL.rstrip("/") + "/ping"
    logger.info(f"[self-ping] starting. pinging {ping_url} every {PING_INTERVAL} seconds (verify={SELF_PING_VERIFY})")
    headers = {}
    if PING_SECRET:
        headers["X-Ping-Secret"] = PING_SECRET

    while True:
        try:
            resp = requests.get(ping_url, timeout=10, headers=headers, params={}, verify=SELF_PING_VERIFY)
            logger.info(f"[self-ping] {ping_url} -> {resp.status_code}")
        except Exception as e:
            logger.error(f"[self-ping] error: {e}")
        time.sleep(PING_INTERVAL)


# --- helper wrapper for protected video sending ---
def send_protected_video(chat_id, video_id, caption=None, **kwargs):
    """
    ارسال ویدیو با قابلیت فروارد و اینلاین کیبورد
    """
    try:
        # استفاده از protect_content=False برای اجازه فروارد
        return bot.send_video(
            chat_id,
            video_id,
            caption=caption,
            protect_content=False,  # تغییر به False برای اجازه فروارد
            reply_markup=create_video_keyboard(),  # اضافه کردن اینلاین کیبورد
            **kwargs
        )
    except TypeError as e:
        # اگر protect_content پشتیبانی نمی‌شود
        logger.warning(f"bot.send_video doesn't accept protect_content param: {e}. Falling back to plain send_video.")
        return bot.send_video(
            chat_id,
            video_id,
            caption=caption,
            reply_markup=create_video_keyboard(),  # اضافه کردن اینلاین کیبورد
            **kwargs
        )
    except Exception as e:
        logger.error(f"Error sending video: {e}")
        raise


# ----------------- main -----------------
def main():
    try:
        logger.info("Starting bot with self-ping and ping endpoint...")
        print("🤖 ربات فعال شد!")

        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        logger.info("Flask ping endpoint started in background thread.")

        ping_thread = threading.Thread(target=self_ping_loop, daemon=True)
        ping_thread.start()
        logger.info("Self-ping thread started.")

        # Remove any existing webhook before starting polling to avoid 409 conflicts
        try:
            bot.remove_webhook()
            logger.info("Removed existing webhook (if any). Starting long polling.")
        except Exception as e:
            logger.warning(f"Couldn't remove webhook (maybe none): {e}")

        while True:
            try:
                bot.infinity_polling(timeout=60, long_polling_timeout=60)
            except Exception as e:
                logger.error(f"Polling error: {e}")
                print(f"🔁 تلاش مجدد پس از 15 ثانیه... خطا: {e}")
                time.sleep(15)

    except Exception as e:
        logger.error(f"Bot crashed: {e}")
        print(f"❌ خطا در اجرای ربات: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()