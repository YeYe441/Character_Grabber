import asyncio
import csv
import logging
import os
import random
import sqlite3
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, LabeledPrice, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
from telegram.constants import ChatType
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes,
    PreCheckoutQueryHandler, filters
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
DB_PATH = os.getenv("DB_PATH", "anime_character_grabber.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("AnimeCharacterGrabber")

RARITIES = ["Common", "Uncommon", "Epic", "Legendary", "Mythical", "Divine", "Limited", "Customization"]
RARITY_WEIGHTS = {
    "Common": 55, "Uncommon": 25, "Epic": 12, "Legendary": 5,
    "Mythical": 2, "Divine": 1
}
CURRENCIES = {"silver", "gold", "diamond"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
THRESHOLDS = [50, 75, 100]


def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    with connect() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',
            silver INTEGER NOT NULL DEFAULT 0, gold INTEGER NOT NULL DEFAULT 0,
            diamond INTEGER NOT NULL DEFAULT 0, role TEXT NOT NULL DEFAULT 'Member',
            daily_at TEXT
        );
        CREATE TABLE IF NOT EXISTS characters(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            rarity TEXT NOT NULL, file_id TEXT, image_path TEXT,
            silver_price INTEGER NOT NULL DEFAULT 0, gold_price INTEGER NOT NULL DEFAULT 0,
            diamond_price INTEGER NOT NULL DEFAULT 0, stars_price INTEGER NOT NULL DEFAULT 0,
            delivery TEXT NOT NULL DEFAULT 'automatic', active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS inventory(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL, acquired_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(character_id) REFERENCES characters(id)
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_user ON inventory(user_id);
        CREATE TABLE IF NOT EXISTS group_state(
            chat_id INTEGER PRIMARY KEY, message_count INTEGER NOT NULL DEFAULT 0,
            next_threshold INTEGER NOT NULL DEFAULT 50
        );
        CREATE TABLE IF NOT EXISTS spawns(
            chat_id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL,
            message_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, user_id INTEGER,
            currency TEXT, amount INTEGER, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS custom_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            telegram_charge_id TEXT, free_prize INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments(
            telegram_charge_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL, total_amount INTEGER NOT NULL,
            currency TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active', created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, ended_at TEXT
        );
        CREATE TABLE IF NOT EXISTS prizes(
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER,
            user_id INTEGER NOT NULL, character_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS gifts(
            id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL, inventory_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS gift_requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT, requester_id INTEGER NOT NULL,
            owner_id INTEGER NOT NULL, character_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS trades(
            id INTEGER PRIMARY KEY AUTOINCREMENT, proposer_id INTEGER NOT NULL,
            target_id INTEGER NOT NULL, proposer_inventory_id INTEGER NOT NULL,
            target_inventory_id INTEGER NOT NULL, currency TEXT,
            amount INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)
        if OWNER_ID:
            c.execute("INSERT OR IGNORE INTO users(user_id, role) VALUES(?, 'Owner')", (OWNER_ID,))
            c.execute("UPDATE users SET role='Owner' WHERE user_id=?", (OWNER_ID,))


def ensure_user(user):
    if not user:
        return
    with connect() as c:
        c.execute("""
            INSERT INTO users(user_id, username, first_name, role)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
              first_name=excluded.first_name
        """, (user.id, user.username or "", user.first_name or "",
              "Owner" if user.id == OWNER_ID else "Member"))


def role_of(user_id):
    if user_id == OWNER_ID:
        return "Owner"
    with connect() as c:
        row = c.execute("SELECT role FROM users WHERE user_id=?", (user_id,)).fetchone()
    return row["role"] if row else "Member"


def is_owner(uid):
    return bool(OWNER_ID and uid == OWNER_ID)


def is_admin(uid):
    return role_of(uid) in ("Owner", "Admin")


def log_tx(actor, uid, currency, amount, reason):
    with connect() as c:
        c.execute("INSERT INTO transactions(actor_id,user_id,currency,amount,reason) VALUES(?,?,?,?,?)",
                  (actor, uid, currency, amount, reason))


def get_char_by_name(name, include_inactive=False):
    with connect() as c:
        condition = "" if include_inactive else " AND active=1"
        return c.execute("SELECT * FROM characters WHERE name=? COLLATE NOCASE" + condition, (name,)).fetchone()


def user_label(uid):
    with connect() as c:
        row = c.execute("SELECT first_name,username FROM users WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return str(uid)
    return ("@" + row["username"]) if row["username"] else (row["first_name"] or str(uid))


def choose_character():
    with connect() as c:
        chars = c.execute("SELECT * FROM characters WHERE active=1 AND rarity!='Customization'").fetchall()
    if not chars:
        return None
    limited = [x for x in chars if x["rarity"] == "Limited"]
    normal = [x for x in chars if x["rarity"] != "Limited"]
    # Limited is selected exactly 0.5% of ordinary spawn attempts if available.
    if limited and random.random() < 0.005:
        return random.choice(limited)
    if not normal:
        return random.choice(limited) if limited else None
    available = {x["rarity"] for x in normal}
    rarities = [r for r in RARITY_WEIGHTS if r in available]
    if not rarities:
        return random.choice(normal)
    rarity = random.choices(rarities, weights=[RARITY_WEIGHTS[r] for r in rarities], k=1)[0]
    pool = [x for x in normal if x["rarity"] == rarity]
    return random.choice(pool) if pool else random.choice(normal)


async def send_spawn(context, chat_id, character=None):
    character = character or choose_character()
    if not character:
        await context.bot.send_message(chat_id, "No characters available. Owner: add characters first.")
        return
    with connect() as c:
        c.execute("""INSERT INTO spawns(chat_id,character_id,message_id) VALUES(?,?,NULL)
        ON CONFLICT(chat_id) DO UPDATE SET character_id=excluded.character_id,
        message_id=NULL,created_at=CURRENT_TIMESTAMP""", (chat_id, character["id"]))
    caption = "✨ A character has appeared!\nUse /w to reveal the exact name, then /grab Character Name."
    try:
        if character["file_id"]:
            sent = await context.bot.send_photo(chat_id, character["file_id"], caption=caption)
        elif character["image_path"] and Path(character["image_path"]).exists():
            with open(character["image_path"], "rb") as f:
                sent = await context.bot.send_photo(chat_id, f, caption=caption)
        else:
            sent = await context.bot.send_message(chat_id, caption + "\n(Image missing.)")
        with connect() as c:
            c.execute("UPDATE spawns SET message_id=? WHERE chat_id=?", (sent.message_id, chat_id))
    except Exception:
        log.exception("Spawn send failed")
        await context.bot.send_message(chat_id, "Could not send spawn. Check bot permissions and image.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user)
    await update.effective_message.reply_text(
        "AnimeCharacterGrabber is online!\n\n"
        "Player commands: /profile /collection /balance /market /buy /daily /w /grab\n"
        "Social: /gift /requestgift /trade /acceptgift /acceptrequest /accepttrade\n"
        "Customization: /custom /claimprize\n"
        "Owner/Admin: /helpadmin"
    )


async def helpadmin(update, context):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.effective_message.reply_text("Admins and Owner only.")
        return
    text = (
        "OWNER / ADMIN COMMANDS\n"
        "/spawn Character Name — manually spawn in current group\n"
        "/addcharacter (reply to photo) Name | Rarity | Silver | Gold | Diamond\n"
        "/importzip (reply to ZIP document) — filenames become names; metadata.csv supported\n"
        "/setadmin USER_ID — Owner only\n"
        "/removeadmin USER_ID — Owner only\n"
        "/mint USER_ID silver|gold|diamond AMOUNT — Owner only\n"
        "/pay USER_ID silver|gold|diamond AMOUNT — Admins transfer own funds\n"
        "/setprice Name | Silver | Gold | Diamond | Stars | automatic/manual — Owner only\n"
        "/disablechar Character Name — Owner only\n"
        "/eventstart Event title /eventend\n"
        "/prize USER_ID | Customization Character Name — active event only\n"
        "/approve REQUEST_ID /reject REQUEST_ID — Owner only\n"
        "/approvegift ID /rejectgift ID — recipient or Owner/Admin\n"
        "/approverequest ID /rejectrequest ID — character owner or Owner/Admin\n"
        "/accepttrade ID /rejecttrade ID — trade target or Owner/Admin\n"
        "/transactions USER_ID — Owner only\n\n"
        "CHARACTER IMPORT: reply to a photo with /addcharacter Name | Rarity | Silver | Gold | Diamond\n"
        "ZIP: send ZIP as a document, then reply /importzip. Optional metadata.csv headers:\n"
        "name,rarity,silver_price,gold_price,diamond_price,stars_price,delivery"
    )
    await update.effective_message.reply_text(text)


async def profile(update, context):
    u = update.effective_user
    ensure_user(u)
    with connect() as c:
        row = c.execute("SELECT silver,gold,diamond,role FROM users WHERE user_id=?", (u.id,)).fetchone()
        count = c.execute("SELECT COUNT(*) n FROM inventory WHERE user_id=?", (u.id,)).fetchone()["n"]
    await update.effective_message.reply_text(
        f"👤 {u.first_name}\nID: {u.id}\nRole: {row['role']}\nCharacters: {count}\n"
        f"Silver: {row['silver']}\nGold: {row['gold']}\nDiamond: {row['diamond']}"
    )


async def collection(update, context):
    u = update.effective_user
    ensure_user(u)
    with connect() as c:
        rows = c.execute("""
            SELECT ch.name,ch.rarity,COUNT(*) qty FROM inventory i
            JOIN characters ch ON ch.id=i.character_id WHERE i.user_id=?
            GROUP BY ch.id ORDER BY ch.rarity,ch.name
        """, (u.id,)).fetchall()
    if not rows:
        await update.effective_message.reply_text("Your collection is empty. Catch one with /grab.")
        return
    text = "📚 Your collection:\n" + "\n".join(f"• {r['name']} [{r['rarity']}] ×{r['qty']}" for r in rows)
    await update.effective_message.reply_text(text[:4000])


async def balance(update, context):
    await profile(update, context)


async def market(update, context):
    """Open the visual Telegram Mini App market in a private chat."""
    msg = update.effective_message
    if not WEBAPP_URL.startswith("https://"):
        await msg.reply_text(
            "The visual market Mini App is not configured yet. Set WEBAPP_URL to your deployed HTTPS address.\n"
            "You can still use /buy Character Name | silver (or gold/diamond)."
        )
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        await msg.reply_text("Open our bot in a private chat and use /market to launch the visual market.")
        return
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🛒 Open Character Market", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await msg.reply_text(
        "🛒 Anime Character Market\nChoose a rarity, filter by currency and budget, then tap a character to buy it.",
        reply_markup=keyboard
    )


async def buy(update, context):
    u = update.effective_user
    ensure_user(u)
    raw = update.effective_message.text.partition(" ")[2].strip()
    parts = [p.strip() for p in raw.rsplit("|", 1)]
    if len(parts) != 2 or parts[1].lower() not in CURRENCIES:
        await update.effective_message.reply_text("Usage: /buy Character Name | silver|gold|diamond")
        return
    name, currency = parts[0], parts[1].lower()
    ch = get_char_by_name(name)
    if not ch:
        await update.effective_message.reply_text("Character not found.")
        return
    price = ch[currency + "_price"]
    if price <= 0:
        await update.effective_message.reply_text(f"{ch['name']} is not for sale with {currency.title()}.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(f"SELECT {currency} FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not row or row[currency] < price:
            c.rollback()
            await update.effective_message.reply_text("You don't have enough currency.")
            return
        c.execute(f"UPDATE users SET {currency}={currency}-? WHERE user_id=?", (price, u.id))
        c.execute("INSERT INTO inventory(user_id,character_id) VALUES(?,?)", (u.id, ch["id"]))
        c.commit()
    log_tx(u.id, u.id, currency, -price, f"Bought {ch['name']}")
    await update.effective_message.reply_text(f"Purchased {ch['name']} for {price} {currency.title()}!")


async def reveal(update, context):
    chat = update.effective_chat
    with connect() as c:
        row = c.execute("SELECT ch.name FROM spawns s JOIN characters ch ON ch.id=s.character_id WHERE s.chat_id=?", (chat.id,)).fetchone()
    await update.effective_message.reply_text(
        f"The exact character name is: {row['name']}" if row else "There is no active character in this chat."
    )


async def grab(update, context):
    u, chat = update.effective_user, update.effective_chat
    ensure_user(u)
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /grab Character Name")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        spawn = c.execute("SELECT character_id FROM spawns WHERE chat_id=?", (chat.id,)).fetchone()
        if not spawn:
            c.rollback()
            await update.effective_message.reply_text("No active character to grab.")
            return
        ch = c.execute("SELECT id,name FROM characters WHERE id=? AND active=1", (spawn["character_id"],)).fetchone()
        if not ch or ch["name"].casefold() != name.casefold():
            c.rollback()
            await update.effective_message.reply_text("Wrong name. Use /w to reveal it.")
            return
        c.execute("INSERT INTO inventory(user_id,character_id) VALUES(?,?)", (u.id, ch["id"]))
        c.execute("DELETE FROM spawns WHERE chat_id=?", (chat.id,))
        c.commit()
    await update.effective_message.reply_text(f"🎉 {u.first_name} grabbed {ch['name']}!")


async def spawn_cmd(update, context):
    u, chat = update.effective_user, update.effective_chat
    if not is_admin(u.id):
        await update.effective_message.reply_text("Admins only.")
        return
    if not chat or chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Use this in the group where you want the spawn.")
        return
    name = " ".join(context.args).strip()
    ch = get_char_by_name(name)
    if not ch:
        await update.effective_message.reply_text("Character not found.")
        return
    await send_spawn(context, chat.id, ch)


async def addcharacter(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_admin(u.id):
        await msg.reply_text("Admins only.")
        return
    replied = msg.reply_to_message
    if not replied or not replied.photo:
        await msg.reply_text("Reply to a photo: /addcharacter Name | Rarity | Silver | Gold | Diamond")
        return
    parts = [x.strip() for x in msg.text.partition(" ")[2].split("|")]
    if len(parts) < 2 or parts[1].title() not in RARITIES:
        await msg.reply_text("Invalid format or rarity.")
        return
    try:
        vals = [max(0, int(parts[i])) if len(parts) > i and parts[i] else 0 for i in range(2, 5)]
    except ValueError:
        await msg.reply_text("Prices must be whole numbers.")
        return
    try:
        with connect() as c:
            c.execute("INSERT INTO characters(name,rarity,file_id,silver_price,gold_price,diamond_price) VALUES(?,?,?,?,?,?)",
                      (parts[0], parts[1].title(), replied.photo[-1].file_id, *vals))
        await msg.reply_text(f"Added {parts[0]} [{parts[1].title()}].")
    except sqlite3.IntegrityError:
        await msg.reply_text("That character name already exists.")


async def importzip(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id):
        await msg.reply_text("Owner only.")
        return
    docmsg = msg.reply_to_message
    if not docmsg or not docmsg.document or not (docmsg.document.file_name or "").lower().endswith(".zip"):
        await msg.reply_text("Upload a ZIP as a document, then reply with /importzip.")
        return
    tgfile = await context.bot.get_file(docmsg.document.file_id)
    with tempfile.TemporaryDirectory() as temp:
        zip_path = os.path.join(temp, "chars.zip")
        await tgfile.download_to_drive(zip_path)
        try:
            with zipfile.ZipFile(zip_path) as z:
                members = [m for m in z.infolist() if not m.is_dir()]
                if any(Path(m.filename).is_absolute() or ".." in Path(m.filename).parts for m in members):
                    await msg.reply_text("ZIP rejected: unsafe path detected.")
                    return
                z.extractall(temp)
        except zipfile.BadZipFile:
            await msg.reply_text("Invalid ZIP file.")
            return
        root = Path(temp)
        metadata = {}
        csvs = list(root.rglob("metadata.csv"))
        if csvs:
            try:
                with open(csvs[0], newline="", encoding="utf-8-sig") as f:
                    for r in csv.DictReader(f):
                        if r.get("name"):
                            metadata[r["name"].strip().casefold()] = r
            except Exception:
                log.exception("Metadata CSV failed")
        imported = skipped = 0
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
                continue
            name = p.stem.strip()
            if not name:
                skipped += 1
                continue
            data = metadata.get(name.casefold(), {})
            rarity = (data.get("rarity") or "Common").strip().title()
            if rarity not in RARITIES:
                rarity = "Common"
            try:
                prices = [max(0, int(data.get(k) or 0)) for k in ("silver_price", "gold_price", "diamond_price", "stars_price")]
            except ValueError:
                prices = [0, 0, 0, 0]
            delivery = (data.get("delivery") or "automatic").strip().lower()
            if delivery not in ("automatic", "manual"):
                delivery = "automatic"
            try:
                if get_char_by_name(name, include_inactive=True):
                    skipped += 1
                    continue
                with open(p, "rb") as f:
                    sent = await context.bot.send_photo(msg.chat_id, f, caption=f"Importing: {name}")
                file_id = sent.photo[-1].file_id
                with connect() as c:
                    c.execute("""INSERT INTO characters(name,rarity,file_id,silver_price,gold_price,
                    diamond_price,stars_price,delivery) VALUES(?,?,?,?,?,?,?,?)""",
                    (name, rarity, file_id, *prices, delivery))
                imported += 1
            except Exception:
                log.exception("Import failed: %s", name)
                skipped += 1
        await msg.reply_text(f"ZIP import complete. Imported: {imported}; skipped: {skipped}.")


async def setadmin(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id):
        await msg.reply_text("Owner only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /setadmin USER_ID")
        return
    try: target = int(context.args[0])
    except ValueError:
        await msg.reply_text("USER_ID must be numeric.")
        return
    with connect() as c:
        c.execute("INSERT INTO users(user_id,role) VALUES(?, 'Admin') ON CONFLICT(user_id) DO UPDATE SET role='Admin'", (target,))
    await msg.reply_text(f"{target} is now an Admin.")


async def removeadmin(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id):
        await msg.reply_text("Owner only.")
        return
    if not context.args:
        await msg.reply_text("Usage: /removeadmin USER_ID")
        return
    try: target = int(context.args[0])
    except ValueError:
        await msg.reply_text("USER_ID must be numeric.")
        return
    if target == OWNER_ID:
        await msg.reply_text("The Owner role cannot be removed.")
        return
    with connect() as c:
        c.execute("UPDATE users SET role='Member' WHERE user_id=?", (target,))
    await msg.reply_text(f"{target} is now a Member.")


async def mint(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id):
        await msg.reply_text("Only the Owner can mint currency.")
        return
    if len(context.args) != 3:
        await msg.reply_text("Usage: /mint USER_ID silver|gold|diamond AMOUNT")
        return
    try: target, currency, amount = int(context.args[0]), context.args[1].lower(), int(context.args[2])
    except ValueError:
        await msg.reply_text("Invalid arguments.")
        return
    if currency not in CURRENCIES or amount <= 0:
        await msg.reply_text("Invalid currency or amount.")
        return
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO users(user_id,role) VALUES(?, 'Member')", (target,))
        c.execute(f"UPDATE users SET {currency}={currency}+? WHERE user_id=?", (amount, target))
    log_tx(u.id, target, currency, amount, "Owner mint")
    await msg.reply_text(f"Minted {amount} {currency.title()} for {target}.")


async def pay(update, context):
    u, msg = update.effective_user, update.effective_message
    ensure_user(u)
    if role_of(u.id) == "Member":
        await msg.reply_text("Only Admins and the Owner can transfer currency.")
        return
    if len(context.args) != 3:
        await msg.reply_text("Usage: /pay USER_ID silver|gold|diamond AMOUNT")
        return
    try: target, currency, amount = int(context.args[0]), context.args[1].lower(), int(context.args[2])
    except ValueError:
        await msg.reply_text("Invalid arguments.")
        return
    if target == u.id or currency not in CURRENCIES or amount <= 0:
        await msg.reply_text("Check target, currency, and positive amount.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        sender = c.execute(f"SELECT {currency} FROM users WHERE user_id=?", (u.id,)).fetchone()
        if not sender or sender[currency] < amount:
            c.rollback()
            await msg.reply_text("Insufficient balance.")
            return
        c.execute("INSERT OR IGNORE INTO users(user_id,role) VALUES(?, 'Member')", (target,))
        c.execute(f"UPDATE users SET {currency}={currency}-? WHERE user_id=?", (amount, u.id))
        c.execute(f"UPDATE users SET {currency}={currency}+? WHERE user_id=?", (amount, target))
        c.commit()
    log_tx(u.id, u.id, currency, -amount, f"Transfer to {target}")
    log_tx(u.id, target, currency, amount, f"Transfer from {u.id}")
    await msg.reply_text(f"Transferred {amount} {currency.title()} to {target}.")


async def setprice(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id):
        await msg.reply_text("Owner only.")
        return
    parts = [x.strip() for x in msg.text.partition(" ")[2].split("|")]
    if len(parts) < 6:
        await msg.reply_text("Usage: /setprice Name | Silver | Gold | Diamond | Stars | automatic/manual")
        return
    try: prices = [max(0, int(x)) for x in parts[1:5]]
    except ValueError:
        await msg.reply_text("Prices must be whole numbers.")
        return
    delivery = parts[5].lower()
    if delivery not in ("automatic", "manual"):
        await msg.reply_text("Delivery must be automatic or manual.")
        return
    with connect() as c:
        cur = c.execute("UPDATE characters SET silver_price=?,gold_price=?,diamond_price=?,stars_price=?,delivery=? WHERE name=? COLLATE NOCASE",
                        (*prices, delivery, parts[0]))
    await msg.reply_text("Character prices updated." if cur.rowcount else "Character not found.")


async def disablechar(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id):
        await msg.reply_text("Owner only.")
        return
    name = " ".join(context.args).strip()
    with connect() as c:
        cur = c.execute("UPDATE characters SET active=0 WHERE name=? COLLATE NOCASE", (name,))
    await msg.reply_text("Character disabled." if cur.rowcount else "Character not found.")


async def daily(update, context):
    u = update.effective_user
    ensure_user(u)
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT daily_at FROM users WHERE user_id=?", (u.id,)).fetchone()
        # SQLite UTC day boundary; one daily reward per UTC calendar day.
        today = c.execute("SELECT date('now') d").fetchone()["d"]
        if row["daily_at"] == today:
            c.rollback()
            await update.effective_message.reply_text("You already claimed today's reward. Try again after the next UTC day.")
            return
        amount = random.randint(50, 100)
        c.execute("UPDATE users SET silver=silver+?,daily_at=? WHERE user_id=?", (amount, today, u.id))
        c.commit()
    log_tx(None, u.id, "silver", amount, "Daily reward")
    await update.effective_message.reply_text(f"You received {amount} Silver!")


async def gift(update, context):
    u, msg = update.effective_user, update.effective_message
    ensure_user(u)
    raw = msg.text.partition(" ")[2].strip()
    parts = [x.strip() for x in raw.split("|", 1)]
    if len(parts) != 2:
        await msg.reply_text("Usage: /gift USER_ID | Character Name")
        return
    try: target = int(parts[0])
    except ValueError:
        await msg.reply_text("USER_ID must be numeric.")
        return
    if target == u.id:
        await msg.reply_text("You cannot gift yourself.")
        return
    ch = get_char_by_name(parts[1], include_inactive=True)
    if not ch:
        await msg.reply_text("Character not found.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        inv = c.execute("SELECT id FROM inventory WHERE user_id=? AND character_id=? ORDER BY id LIMIT 1",
                        (u.id, ch["id"])).fetchone()
        if not inv:
            c.rollback()
            await msg.reply_text("You don't own that character.")
            return
        c.execute("INSERT OR IGNORE INTO users(user_id,role) VALUES(?, 'Member')", (target,))
        c.execute("INSERT INTO gifts(sender_id,recipient_id,inventory_id) VALUES(?,?,?)",
                  (u.id, target, inv["id"]))
        gid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    await msg.reply_text(f"Gift offer #{gid} sent. Recipient must approve with /acceptgift {gid}.")
    try:
        await context.bot.send_message(target, f"{user_label(u.id)} offered you {ch['name']}. Approve with /acceptgift {gid} or decline with /rejectgift {gid}.")
    except Exception:
        pass


async def acceptgift(update, context):
    u, msg = update.effective_user, update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /acceptgift ID")
        return
    try: gid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid ID.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        g = c.execute("SELECT * FROM gifts WHERE id=? AND status='pending'", (gid,)).fetchone()
        if not g or (g["recipient_id"] != u.id and not is_admin(u.id)):
            c.rollback()
            await msg.reply_text("Gift not found or you cannot approve it.")
            return
        inv = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (g["inventory_id"], g["sender_id"])).fetchone()
        if not inv:
            c.execute("UPDATE gifts SET status='cancelled' WHERE id=?", (gid,))
            c.commit()
            await msg.reply_text("Sender no longer owns that copy; gift cancelled.")
            return
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (g["recipient_id"], inv["id"]))
        c.execute("UPDATE gifts SET status='accepted' WHERE id=?", (gid,))
        c.commit()
    await msg.reply_text(f"Gift #{gid} accepted.")


async def rejectgift(update, context):
    u, msg = update.effective_user, update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /rejectgift ID")
        return
    try: gid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid ID.")
        return
    with connect() as c:
        g = c.execute("SELECT * FROM gifts WHERE id=? AND status='pending'", (gid,)).fetchone()
        if not g or (g["recipient_id"] != u.id and not is_admin(u.id)):
            await msg.reply_text("Gift not found or you cannot reject it.")
            return
        c.execute("UPDATE gifts SET status='rejected' WHERE id=?", (gid,))
    await msg.reply_text(f"Gift #{gid} rejected.")


async def requestgift(update, context):
    u, msg = update.effective_user, update.effective_message
    raw = msg.text.partition(" ")[2].strip()
    parts = [x.strip() for x in raw.split("|", 1)]
    if len(parts) != 2:
        await msg.reply_text("Usage: /requestgift OWNER_USER_ID | Character Name")
        return
    try: owner = int(parts[0])
    except ValueError:
        await msg.reply_text("User ID must be numeric.")
        return
    ch = get_char_by_name(parts[1], include_inactive=True)
    if not ch:
        await msg.reply_text("Character not found.")
        return
    with connect() as c:
        c.execute("INSERT INTO gift_requests(requester_id,owner_id,character_id) VALUES(?,?,?)",
                  (u.id, owner, ch["id"]))
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    await msg.reply_text(f"Character request #{rid} sent. The owner must approve it.")
    try:
        await context.bot.send_message(owner, f"{user_label(u.id)} requested {ch['name']}. Approve with /approverequest {rid} or reject with /rejectrequest {rid}.")
    except Exception:
        pass


async def approverequest(update, context):
    u, msg = update.effective_user, update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /approverequest ID")
        return
    try: rid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid ID.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        req = c.execute("SELECT * FROM gift_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
        if not req or (req["owner_id"] != u.id and not is_admin(u.id)):
            c.rollback()
            await msg.reply_text("Request not found or you cannot approve it.")
            return
        inv = c.execute("SELECT id FROM inventory WHERE user_id=? AND character_id=? ORDER BY id LIMIT 1",
                        (req["owner_id"], req["character_id"])).fetchone()
        if not inv:
            c.rollback()
            await msg.reply_text("You no longer own that character.")
            return
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (req["requester_id"], inv["id"]))
        c.execute("UPDATE gift_requests SET status='approved' WHERE id=?", (rid,))
        c.commit()
        ch = c.execute("SELECT name FROM characters WHERE id=?", (req["character_id"],)).fetchone()
    await msg.reply_text(f"Request #{rid} approved.")
    try:
        await context.bot.send_message(req["requester_id"], f"Your request was approved. You received {ch['name']}!")
    except Exception:
        pass


async def rejectrequest(update, context):
    u, msg = update.effective_user, update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /rejectrequest ID")
        return
    try: rid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid ID.")
        return
    with connect() as c:
        req = c.execute("SELECT * FROM gift_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
        if not req or (req["owner_id"] != u.id and not is_admin(u.id)):
            await msg.reply_text("Request not found or you cannot reject it.")
            return
        c.execute("UPDATE gift_requests SET status='rejected' WHERE id=?", (rid,))
    await msg.reply_text(f"Request #{rid} rejected.")


async def trade(update, context):
    u, msg = update.effective_user, update.effective_message
    raw = msg.text.partition(" ")[2].strip()
    parts = [x.strip() for x in raw.split("|")]
    if len(parts) not in (3, 5):
        await msg.reply_text("Usage: /trade USER_ID | Your Character | Their Character [| silver|gold|diamond | amount]")
        return
    try: target = int(parts[0])
    except ValueError:
        await msg.reply_text("Target USER_ID must be numeric.")
        return
    currency, amount = None, 0
    if len(parts) == 5:
        currency = parts[3].lower()
        try: amount = int(parts[4])
        except ValueError:
            await msg.reply_text("Amount must be a whole number.")
            return
        if currency not in CURRENCIES or amount < 0:
            await msg.reply_text("Invalid currency or amount.")
            return
    if target == u.id:
        await msg.reply_text("You cannot trade with yourself.")
        return
    a, b = get_char_by_name(parts[1], True), get_char_by_name(parts[2], True)
    if not a or not b:
        await msg.reply_text("One or both characters were not found.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        inv_a = c.execute("SELECT id FROM inventory WHERE user_id=? AND character_id=? ORDER BY id LIMIT 1", (u.id, a["id"])).fetchone()
        inv_b = c.execute("SELECT id FROM inventory WHERE user_id=? AND character_id=? ORDER BY id LIMIT 1", (target, b["id"])).fetchone()
        if not inv_a or not inv_b:
            c.rollback()
            await msg.reply_text("Both players must own the specified character copies.")
            return
        if currency and amount:
            bal = c.execute(f"SELECT {currency} FROM users WHERE user_id=?", (u.id,)).fetchone()
            if not bal or bal[currency] < amount:
                c.rollback()
                await msg.reply_text("You do not have enough currency to offer.")
                return
        c.execute("INSERT INTO trades(proposer_id,target_id,proposer_inventory_id,target_inventory_id,currency,amount) VALUES(?,?,?,?,?,?)",
                  (u.id, target, inv_a["id"], inv_b["id"], currency, amount))
        tid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
    await msg.reply_text(f"Trade offer #{tid} sent. Target can /accepttrade {tid} or /rejecttrade {tid}.")
    try:
        await context.bot.send_message(target, f"{user_label(u.id)} offered a trade. Review it in the group/chat, then /accepttrade {tid} or /rejecttrade {tid}.")
    except Exception:
        pass


async def accepttrade(update, context):
    u, msg = update.effective_user, update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /accepttrade ID")
        return
    try: tid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid ID.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        t = c.execute("SELECT * FROM trades WHERE id=? AND status='pending'", (tid,)).fetchone()
        if not t or (t["target_id"] != u.id and not is_admin(u.id)):
            c.rollback()
            await msg.reply_text("Trade not found or you cannot accept it.")
            return
        inv_a = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (t["proposer_inventory_id"], t["proposer_id"])).fetchone()
        inv_b = c.execute("SELECT * FROM inventory WHERE id=? AND user_id=?", (t["target_inventory_id"], t["target_id"])).fetchone()
        if not inv_a or not inv_b:
            c.execute("UPDATE trades SET status='cancelled' WHERE id=?", (tid,))
            c.commit()
            await msg.reply_text("Trade cancelled because one player no longer owns the offered copy.")
            return
        if t["currency"] and t["amount"]:
            bal = c.execute(f"SELECT {t['currency']} FROM users WHERE user_id=?", (t["proposer_id"],)).fetchone()
            if not bal or bal[t["currency"]] < t["amount"]:
                c.execute("UPDATE trades SET status='cancelled' WHERE id=?", (tid,))
                c.commit()
                await msg.reply_text("Trade cancelled because proposer no longer has enough currency.")
                return
            c.execute(f"UPDATE users SET {t['currency']}={t['currency']}-? WHERE user_id=?", (t["amount"], t["proposer_id"]))
            c.execute(f"UPDATE users SET {t['currency']}={t['currency']}+? WHERE user_id=?", (t["amount"], t["target_id"]))
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (t["target_id"], inv_a["id"]))
        c.execute("UPDATE inventory SET user_id=? WHERE id=?", (t["proposer_id"], inv_b["id"]))
        c.execute("UPDATE trades SET status='accepted' WHERE id=?", (tid,))
        c.commit()
    if t["currency"] and t["amount"]:
        log_tx(t["proposer_id"], t["proposer_id"], t["currency"], -t["amount"], f"Trade #{tid}")
        log_tx(t["proposer_id"], t["target_id"], t["currency"], t["amount"], f"Trade #{tid}")
    await msg.reply_text(f"Trade #{tid} accepted. Characters exchanged.")


async def rejecttrade(update, context):
    u, msg = update.effective_user, update.effective_message
    if not context.args:
        await msg.reply_text("Usage: /rejecttrade ID")
        return
    try: tid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid ID.")
        return
    with connect() as c:
        t = c.execute("SELECT * FROM trades WHERE id=? AND status='pending'", (tid,)).fetchone()
        if not t or (t["target_id"] != u.id and not is_admin(u.id)):
            await msg.reply_text("Trade not found or you cannot reject it.")
            return
        c.execute("UPDATE trades SET status='rejected' WHERE id=?", (tid,))
    await msg.reply_text(f"Trade #{tid} rejected.")


async def custom(update, context):
    u, msg = update.effective_user, update.effective_message
    ensure_user(u)
    name = " ".join(context.args).strip()
    if not name:
        await msg.reply_text("Usage: /custom Character Name")
        return
    ch = get_char_by_name(name)
    if not ch or ch["rarity"] != "Customization":
        await msg.reply_text("Customization character not found.")
        return
    if ch["stars_price"] <= 0:
        await msg.reply_text("This character has no Stars price configured. Contact the Owner.")
        return
    if msg.chat.type != ChatType.PRIVATE:
        await msg.reply_text("For payment safety, use /custom Character Name in the bot's private chat.")
        return
    await context.bot.send_invoice(
        chat_id=u.id, title=f"Customization: {ch['name']}",
        description=f"Request the Customization character {ch['name']}.",
        payload=f"custom:{u.id}:{ch['id']}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label=ch["name"], amount=ch["stars_price"])]
    )


async def claimprize(update, context):
    u, msg = update.effective_user, update.effective_message
    name = " ".join(context.args).strip()
    if not name:
        await msg.reply_text("Usage: /claimprize Character Name")
        return
    ch = get_char_by_name(name)
    if not ch or ch["rarity"] != "Customization":
        await msg.reply_text("That is not an available Customization character.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        prize = c.execute("SELECT * FROM prizes WHERE user_id=? AND character_id=? AND status='available' ORDER BY id LIMIT 1",
                          (u.id, ch["id"])).fetchone()
        if not prize:
            c.rollback()
            await msg.reply_text("You do not have an available prize for that character.")
            return
        c.execute("UPDATE prizes SET status='claimed' WHERE id=?", (prize["id"],))
        c.execute("INSERT INTO inventory(user_id,character_id) VALUES(?,?)", (u.id, ch["id"]))
        c.commit()
    await msg.reply_text(f"🎁 Prize claimed! {ch['name']} has been added to your collection.")


async def eventstart(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_admin(u.id):
        await msg.reply_text("Admins only.")
        return
    title = msg.text.partition(" ")[2].strip()
    if not title:
        await msg.reply_text("Usage: /eventstart Event title")
        return
    with connect() as c:
        c.execute("UPDATE events SET status='ended',ended_at=CURRENT_TIMESTAMP WHERE status='active'")
        c.execute("INSERT INTO events(title,created_by) VALUES(?,?)", (title, u.id))
        eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    await msg.reply_text(f"Event #{eid} started: {title}")


async def eventend(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_admin(u.id):
        await msg.reply_text("Admins only.")
        return
    with connect() as c:
        cur = c.execute("UPDATE events SET status='ended',ended_at=CURRENT_TIMESTAMP WHERE status='active'")
    await msg.reply_text("Active event ended." if cur.rowcount else "No active event.")


async def prize(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_admin(u.id):
        await msg.reply_text("Admins only.")
        return
    parts = [x.strip() for x in msg.text.partition(" ")[2].split("|", 1)]
    if len(parts) != 2:
        await msg.reply_text("Usage: /prize USER_ID | Customization Character Name")
        return
    try: target = int(parts[0])
    except ValueError:
        await msg.reply_text("USER_ID must be numeric.")
        return
    ch = get_char_by_name(parts[1])
    if not ch or ch["rarity"] != "Customization":
        await msg.reply_text("Choose a Customization character.")
        return
    with connect() as c:
        ev = c.execute("SELECT id FROM events WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        if not ev:
            await msg.reply_text("Start an event first with /eventstart.")
            return
        c.execute("INSERT OR IGNORE INTO users(user_id,role) VALUES(?, 'Member')", (target,))
        c.execute("INSERT INTO prizes(event_id,user_id,character_id) VALUES(?,?,?)", (ev["id"], target, ch["id"]))
        pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    await msg.reply_text(f"Free prize #{pid} assigned to {target}: {ch['name']}. They can use /claimprize {ch['name']}.")
    try:
        await context.bot.send_message(target, f"🎁 Event prize: you can claim {ch['name']} for free with /claimprize {ch['name']}.")
    except Exception:
        pass


async def precheckout(update, context):
    q = update.pre_checkout_query
    payload = q.invoice_payload.split(":")
    try:
        expected_uid, cid = int(payload[1]), int(payload[2])
    except (IndexError, ValueError):
        await q.answer(ok=False, error_message="Invalid invoice. Please contact the bot owner.")
        return
    ch = get_char_by_name(str(cid)) if False else None
    with connect() as c:
        ch = c.execute("SELECT * FROM characters WHERE id=? AND active=1 AND rarity='Customization'", (cid,)).fetchone()
    if payload[0] != "custom" or expected_uid != q.from_user.id or not ch:
        await q.answer(ok=False, error_message="This invoice is invalid.")
        return
    if q.currency != "XTR" or q.total_amount != ch["stars_price"] or ch["stars_price"] <= 0:
        await q.answer(ok=False, error_message="Price mismatch. Please request a new invoice.")
        return
    await q.answer(ok=True)


async def successful_payment(update, context):
    u, msg = update.effective_user, update.effective_message
    payment = msg.successful_payment
    payload = payment.invoice_payload.split(":")
    if len(payload) != 3 or payload[0] != "custom":
        return
    try: expected_uid, cid = int(payload[1]), int(payload[2])
    except ValueError:
        return
    if expected_uid != u.id:
        return
    with connect() as c:
        if c.execute("SELECT 1 FROM payments WHERE telegram_charge_id=?", (payment.telegram_payment_charge_id,)).fetchone():
            await msg.reply_text("This payment was already processed.")
            return
        ch = c.execute("SELECT * FROM characters WHERE id=? AND rarity='Customization'", (cid,)).fetchone()
        if not ch or payment.currency != "XTR" or payment.total_amount != ch["stars_price"]:
            await msg.reply_text("Payment was received but could not be matched. Contact the Owner before retrying.")
            return
        c.execute("INSERT INTO payments(telegram_charge_id,user_id,character_id,total_amount,currency) VALUES(?,?,?,?,?)",
                  (payment.telegram_payment_charge_id, u.id, cid, payment.total_amount, payment.currency))
        status = "delivered" if ch["delivery"] == "automatic" else "pending"
        c.execute("INSERT INTO custom_requests(user_id,character_id,status,telegram_charge_id) VALUES(?,?,?,?)",
                  (u.id, cid, status, payment.telegram_payment_charge_id))
        rid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        if status == "delivered":
            c.execute("INSERT INTO inventory(user_id,character_id) VALUES(?,?)", (u.id, cid))
    if ch["delivery"] == "automatic":
        await msg.reply_text(f"Payment successful. {ch['name']} was added to your collection.")
    else:
        await msg.reply_text(f"Payment successful. Request #{rid} is waiting for Owner approval.")
        if OWNER_ID:
            try:
                await context.bot.send_message(OWNER_ID, f"Paid Customization request #{rid}: user {u.id}, character {ch['name']}. /approve {rid} or /reject {rid}")
            except Exception:
                pass


async def approve(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id) or not context.args:
        await msg.reply_text("Owner only. Usage: /approve REQUEST_ID")
        return
    try: rid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid request ID.")
        return
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        req = c.execute("SELECT * FROM custom_requests WHERE id=? AND status='pending' AND free_prize=0", (rid,)).fetchone()
        if not req:
            c.rollback()
            await msg.reply_text("Pending paid request not found.")
            return
        c.execute("UPDATE custom_requests SET status='delivered' WHERE id=?", (rid,))
        c.execute("INSERT INTO inventory(user_id,character_id) VALUES(?,?)", (req["user_id"], req["character_id"]))
        c.commit()
        ch = c.execute("SELECT name FROM characters WHERE id=?", (req["character_id"],)).fetchone()
    await msg.reply_text(f"Request #{rid} approved.")
    try:
        await context.bot.send_message(req["user_id"], f"Your Customization character {ch['name']} has been delivered!")
    except Exception:
        pass


async def reject(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id) or not context.args:
        await msg.reply_text("Owner only. Usage: /reject REQUEST_ID")
        return
    try: rid = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid request ID.")
        return
    with connect() as c:
        cur = c.execute("UPDATE custom_requests SET status='rejected' WHERE id=? AND status='pending' AND free_prize=0", (rid,))
    await msg.reply_text("Request marked rejected. This command does not issue an automatic Stars refund; use Telegram's supported refund process." if cur.rowcount else "Pending request not found.")


async def transactions(update, context):
    u, msg = update.effective_user, update.effective_message
    if not is_owner(u.id) or not context.args:
        await msg.reply_text("Owner only. Usage: /transactions USER_ID")
        return
    try: target = int(context.args[0])
    except ValueError:
        await msg.reply_text("Invalid user ID.")
        return
    with connect() as c:
        rows = c.execute("SELECT * FROM transactions WHERE user_id=? ORDER BY id DESC LIMIT 20", (target,)).fetchall()
    if not rows:
        await msg.reply_text("No transactions recorded.")
        return
    await msg.reply_text("Recent transactions:\n" + "\n".join(
        f"#{r['id']} {r['currency']} {r['amount']} — {r['reason']} ({r['created_at']})" for r in rows
    )[:4000])


async def count_group_message(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not chat or not user or user.is_bot or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    ensure_user(user)
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO group_state(chat_id,message_count,next_threshold) VALUES(?,0,?)",
                  (chat.id, random.choice(THRESHOLDS)))
        c.execute("UPDATE group_state SET message_count=message_count+1 WHERE chat_id=?", (chat.id,))
        state = c.execute("SELECT * FROM group_state WHERE chat_id=?", (chat.id,)).fetchone()
        active = c.execute("SELECT 1 FROM spawns WHERE chat_id=?", (chat.id,)).fetchone()
        should = state["message_count"] >= state["next_threshold"] and not active
        if should:
            c.execute("UPDATE group_state SET message_count=0,next_threshold=? WHERE chat_id=?",
                      (random.choice(THRESHOLDS), chat.id))
    if should:
        await send_spawn(context, chat.id)


async def error_handler(update, context):
    log.error("Unhandled error: %s", context.error, exc_info=context.error)


async def setup_market_menu(application):
    """Expose the Mini App from Telegram's bot menu when a public HTTPS URL is configured."""
    if WEBAPP_URL.startswith("https://"):
        try:
            await application.bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(text="Market", web_app=WebAppInfo(url=WEBAPP_URL))
            )
            log.info("Telegram bot menu button set to Character Market")
        except Exception:
            log.exception("Could not set the Telegram Mini App menu button")


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and set it.")
    if not OWNER_ID:
        raise SystemExit("OWNER_ID is missing. Set your numeric Telegram user ID in .env.")
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(setup_market_menu).build()
    commands = [
        ("start", start), ("helpadmin", helpadmin), ("profile", profile),
        ("collection", collection), ("inventory", collection), ("balance", balance),
        ("market", market), ("buy", buy), ("w", reveal), ("grab", grab),
        ("spawn", spawn_cmd), ("addcharacter", addcharacter), ("importzip", importzip),
        ("setadmin", setadmin), ("removeadmin", removeadmin), ("mint", mint),
        ("pay", pay), ("setprice", setprice), ("disablechar", disablechar),
        ("daily", daily), ("gift", gift), ("acceptgift", acceptgift), ("rejectgift", rejectgift),
        ("requestgift", requestgift), ("approverequest", approverequest), ("rejectrequest", rejectrequest),
        ("trade", trade), ("accepttrade", accepttrade), ("rejecttrade", rejecttrade),
        ("custom", custom), ("claimprize", claimprize), ("eventstart", eventstart),
        ("eventend", eventend), ("prize", prize), ("approve", approve), ("reject", reject),
        ("transactions", transactions)
    ]
    for name, callback in commands:
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, count_group_message), group=-1)
    app.add_error_handler(error_handler)
    log.info("AnimeCharacterGrabber starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
