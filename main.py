# main.py
import os
import re
import json
import asyncio
import logging
from typing import List

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import Message

# --- Optional: OpenAI for translation ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
_openai_client = None
if OPENAI_API_KEY:
    try:
        from openai import OpenAI
        _openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"[WARN] OpenAI init failed: {e}")
        _openai_client = None

# --- Required env vars (Render: Environment tab) ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")

SOURCE = os.getenv("SOURCE_CHANNEL", "")
TARGET = os.getenv("TARGET_CHANNEL", "")

LANG_TO = os.getenv("TRANSLATE_TO", "uz")
CATCH_UP = int(os.getenv("CATCH_UP", "20"))
CHECK_INTERVAL_MS = int(os.getenv("CHECK_INTERVAL_MS", "400"))

SIGNATURE = os.getenv("SIGNATURE", "").strip()
KEEP_HASHTAGS = os.getenv("KEEP_HASHTAGS", "1") == "1"

RETRY_COUNT = int(os.getenv("RETRY_COUNT", "3"))
RETRY_DELAY_SEC = int(os.getenv("RETRY_DELAY_SEC", "2"))

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Logging ---
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tg-autorepost")

# --- State (for de-dup) ---
STATE_FILE = "state.json"

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                st = json.load(f)
                if isinstance(st, dict) and "last_id" in st:
                    return st
    except Exception as e:
        log.warning("Failed to load state: %s", e)
    return {"last_id": 0}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Failed to save state: %s", e)

state = load_state()

# --- Client ---
if not (API_ID and API_HASH and STRING_SESSION and SOURCE and TARGET):
    raise RuntimeError(
        "Missing env vars. Required: API_ID, API_HASH, STRING_SESSION, "
        "SOURCE_CHANNEL, TARGET_CHANNEL"
    )

client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

# --- Utils: hashtag preservation for translation ---
def _preserve_hashtags(text: str):
    if not text:
        return "", []
    # Unicode word characters + underscores
    tags = re.findall(r"(#[\w\p{L}\p{N}_]+)", text, flags=re.UNICODE)
    t = text
    placeholders = []
    for i, tag in enumerate(tags):
        ph = f"__HTAG_{i}__"
        t = t.replace(tag, ph)
        placeholders.append(tag)
    return t, placeholders

def _restore_hashtags(text: str, placeholders: List[str]) -> str:
    t = text
    for i, tag in enumerate(placeholders):
        t = t.replace(f"__HTAG_{i}__", tag)
    return t

def _normalize(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"[ \t]+", " ", text).strip()

# --- Translation ---
async def translate_text(text: str, target_lang: str) -> str:
    """Translate with OpenAI if key provided; otherwise passthrough."""
    if not text:
        return text

    base = text.strip()
    ph = []
    if KEEP_HASHTAGS:
        base, ph = _preserve_hashtags(base)

    if _openai_client:
        try:
            prompt = (
                f"Quyidagi matnni mazmunini buzmasdan, emoji/formatini saqlagan holda "
                f"{target_lang} tiliga tabiiy va silliq ohangda tarjima qil. "
                f"Agar matn allaqachon shu tilda bo‘lsa — engil stil tahriri bilan qaytar.\n\n"
                f"Matn:\n{base}"
            )
            resp = _openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
            )
            out = resp.choices[0].message.content.strip()
        except Exception as e:
            log.error("Translation failed, passthrough used: %s", e)
            out = base
    else:
        out = base

    if KEEP_HASHTAGS:
        out = _restore_hashtags(out, ph)

    if SIGNATURE:
        out = f"{out}\n\n{SIGNATURE}" if out else SIGNATURE

    return out

# --- Safe send helpers with retry ---
async def safe_send_file(files, caption):
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            return await client.send_file(TARGET, files, caption=caption)
        except FloodWaitError as fw:
            wait = int(getattr(fw, "seconds", 5)) + 2
            log.warning("FloodWait: sleeping %ss", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            log.error("send_file error (%s/%s): %s", attempt, RETRY_COUNT, e)
            if attempt < RETRY_COUNT:
                await asyncio.sleep(RETRY_DELAY_SEC)
            else:
                raise

async def safe_send_message(text):
    for attempt in range(1, RETRY_COUNT + 1):
        try:
            return await client.send_message(TARGET, text)
        except FloodWaitError as fw:
            wait = int(getattr(fw, "seconds", 5)) + 2
            log.warning("FloodWait: sleeping %ss", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            log.error("send_message error (%s/%s): %s", attempt, RETRY_COUNT, e)
            if attempt < RETRY_COUNT:
                await asyncio.sleep(RETRY_DELAY_SEC)
            else:
                raise

# --- Reposting logic ---
async def repost_single_message(msg: Message):
    if msg.id <= state.get("last_id", 0):
        return

    caption_src = msg.text or msg.message or ""
    caption_tr = _normalize(await translate_text(caption_src, LANG_TO))

    if msg.media:
        content = await client.download_media(msg, file=bytes)
        await safe_send_file(content, caption_tr)
    else:
        if caption_tr:
            await safe_send_message(caption_tr)

    state["last_id"] = msg.id
    save_state(state)
    log.info("Reposted msg id=%s", msg.id)

async def repost_media_group(messages: List[Message]):
    messages = sorted(messages, key=lambda m: m.id)
    last_msg = messages[-1]
    if last_msg.id <= state.get("last_id", 0):
        return

    caption_src = last_msg.text or last_msg.message or ""
    caption_tr = _normalize(await translate_text(caption_src, LANG_TO))

    files = []
    for m in messages:
        if m.media:
            content = await client.download_media(m, file=bytes)
            files.append(content)

    if files:
        await safe_send_file(files, caption_tr)
    elif caption_tr:
        await safe_send_message(caption_tr)

    state["last_id"] = last_msg.id
    save_state(state)
    log.info("Reposted media group up to msg id=%s", last_msg.id)

async def handle_new_message(event):
    # If this message is part of an album, wait briefly and collect the group
    if event.message.grouped_id:
        await asyncio.sleep(max(0.1, CHECK_INTERVAL_MS / 1000))
        gid = event.message.grouped_id
        grp = []
        async for m in client.iter_messages(SOURCE, reverse=True, limit=40):
            if m.grouped_id == gid:
                grp.append(m)
        if grp:
            await repost_media_group(grp)
    else:
        await repost_single_message(event.message)

# --- Telethon event ---
@client.on(events.NewMessage(chats=SOURCE))
async def on_new_post(event):
    try:
        await handle_new_message(event)
    except Exception as e:
        log.exception("on_new_post failure: %s", e)

# --- Initial catch-up (optional) ---
async def initial_catchup():
    if CATCH_UP <= 0:
        return
    try:
        msgs = []
        async for m in client.iter_messages(SOURCE, limit=CATCH_UP, reverse=True):
            msgs.append(m)

        for m in msgs:
            try:
                if m.grouped_id:
                    gid = m.grouped_id
                    grp = []
                    async for x in client.iter_messages(SOURCE, reverse=True, limit=CATCH_UP + 40):
                        if x.grouped_id == gid:
                            grp.append(x)
                    if grp:
                        await repost_media_group(grp)
                else:
                    await repost_single_message(m)
            except Exception as e:
                log.exception("catchup failed for msg %s: %s", m.id, e)
    except Exception as e:
        log.exception("initial_catchup error: %s", e)

# --- Main ---
async def main():
    log.info("Autopilot started: %s → %s | lang=%s | catch_up=%s",
             SOURCE, TARGET, LANG_TO, CATCH_UP)
    await initial_catchup()
    await client.run_until_disconnected()

if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
