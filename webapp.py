"""HTTPS-hosted Telegram Mini App backend for AnimeCharacterGrabber.
Run alongside bot.py, using the same .env and SQLite database.
"""
import hashlib
import hmac
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "anime_character_grabber.db")
BASE_DIR = Path(__file__).resolve().parent
RARITIES = ["Common", "Uncommon", "Epic", "Legendary", "Mythical", "Divine", "Limited", "Customization"]
PRICE_COLUMNS = {"silver": "silver_price", "gold": "gold_price", "diamond": "diamond_price"}

app = FastAPI(title="AnimeCharacterGrabber Market")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

def connect():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    return c


def verify_init_data(init_data: str) -> dict:
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token is not configured on the server.")
    if not init_data:
        raise HTTPException(401, "Open this market from the Telegram bot.")
    parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
    supplied_hash = parsed.pop("hash", [""])[0]
    if not supplied_hash:
        raise HTTPException(401, "Telegram authentication data is missing.")
    pairs = sorted((k, v[0]) for k, v in parsed.items())
    data_check_string = "\n".join(f"{k}={v}" for k, v in pairs)
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, supplied_hash):
        raise HTTPException(401, "Telegram authentication failed.")
    try:
        auth_date = int(parsed.get("auth_date", ["0"])[0])
        user = json.loads(parsed.get("user", ["{}"])[0])
        user_id = int(user["id"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        raise HTTPException(401, "Invalid Telegram user data.")
    if auth_date > int(time.time()) + 60 or int(time.time()) - auth_date > 86400:
        raise HTTPException(401, "Telegram login expired. Close and reopen the market.")
    with connect() as c:
        c.execute("""INSERT INTO users(user_id, username, first_name) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
            (user_id, user.get("username", ""), user.get("first_name", "")))
    return {"id": user_id, "username": user.get("username", ""), "first_name": user.get("first_name", "")}


def current_user(x_telegram_init_data: str = Header(default="")) -> dict:
    return verify_init_data(x_telegram_init_data)


@app.get("/", response_class=HTMLResponse)
def home():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/me")
def me(user: dict = __import__("fastapi").Depends(current_user)):
    with connect() as c:
        row = c.execute("SELECT silver,gold,diamond FROM users WHERE user_id=?", (user["id"],)).fetchone()
        count = c.execute("SELECT COUNT(*) n FROM inventory WHERE user_id=?", (user["id"],)).fetchone()["n"]
    return {"user": user, "balances": dict(row) if row else {"silver": 0, "gold": 0, "diamond": 0}, "collection_count": count}


@app.get("/api/characters")
def characters(
    rarity: str = Query(default="All"),
    currency: str = Query(default="silver"),
    max_price: Optional[int] = Query(default=None, ge=0),
    sort: str = Query(default="cheapest"),
    page: int = Query(default=1, ge=1),
    user: dict = __import__("fastapi").Depends(current_user),
):
    if currency not in PRICE_COLUMNS:
        raise HTTPException(400, "Choose Silver, Gold, or Diamond.")
    if rarity != "All" and rarity not in RARITIES:
        raise HTTPException(400, "Unknown rarity.")
    col = PRICE_COLUMNS[currency]
    where = ["active=1", f"{col}>0"]
    params = []
    if rarity != "All":
        where.append("rarity=?")
        params.append(rarity)
    if max_price is not None:
        where.append(f"{col}<=?")
        params.append(max_price)
    order = f"{col} ASC, name COLLATE NOCASE ASC" if sort == "cheapest" else (f"{col} DESC, name COLLATE NOCASE ASC" if sort == "expensive" else "id DESC")
    with connect() as c:
        total = c.execute("SELECT COUNT(*) n FROM characters WHERE " + " AND ".join(where), params).fetchone()["n"]
        rows = c.execute(f"SELECT id,name,rarity,silver_price,gold_price,diamond_price,stars_price FROM characters WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT 8 OFFSET ?", params + [(page - 1) * 8]).fetchall()
    return {"items": [dict(r) for r in rows], "page": page, "pages": max(1, (total + 7) // 8), "total": total, "currency": currency}


class PurchaseRequest(BaseModel):
    character_id: int = Field(gt=0)
    currency: str


@app.post("/api/buy")
def buy(body: PurchaseRequest, user: dict = __import__("fastapi").Depends(current_user)):
    if body.currency not in PRICE_COLUMNS:
        raise HTTPException(400, "Only Silver, Gold, and Diamond purchases are supported in the market.")
    col = PRICE_COLUMNS[body.currency]
    uid = user["id"]
    with connect() as c:
        c.execute("BEGIN IMMEDIATE")
        ch = c.execute(f"SELECT id,name,rarity,{col} AS price FROM characters WHERE id=? AND active=1", (body.character_id,)).fetchone()
        if not ch or ch["price"] <= 0:
            c.rollback()
            raise HTTPException(404, "This character is no longer available for this currency.")
        wallet = c.execute(f"SELECT {body.currency} AS balance FROM users WHERE user_id=?", (uid,)).fetchone()
        if not wallet or wallet["balance"] < ch["price"]:
            c.rollback()
            raise HTTPException(400, "Not enough currency for this purchase.")
        c.execute(f"UPDATE users SET {body.currency}={body.currency}-? WHERE user_id=?", (ch["price"], uid))
        c.execute("INSERT INTO inventory(user_id,character_id) VALUES(?,?)", (uid, ch["id"]))
        c.execute("INSERT INTO transactions(actor_id,user_id,currency,amount,reason) VALUES(?,?,?,?,?)",
                  (uid, uid, body.currency, -ch["price"], f"Mini App purchase: {ch['name']}"))
        c.commit()
    return {"ok": True, "name": ch["name"], "rarity": ch["rarity"], "price": ch["price"], "currency": body.currency}


@app.get("/api/image/{character_id}")
def character_image(character_id: int, user: dict = __import__("fastapi").Depends(current_user)):
    with connect() as c:
        ch = c.execute("SELECT file_id,image_path FROM characters WHERE id=? AND active=1", (character_id,)).fetchone()
    if not ch:
        raise HTTPException(404, "Character image not found.")
    if ch["image_path"]:
        path = Path(ch["image_path"]).expanduser()
        if path.is_file():
            return FileResponse(path)
    if not ch["file_id"] or not BOT_TOKEN:
        raise HTTPException(404, "No image is available for this character.")
    try:
        req = urllib.request.Request(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={urllib.parse.quote(ch['file_id'])}")
        with urllib.request.urlopen(req, timeout=12) as response:
            payload = json.loads(response.read().decode())
        if not payload.get("ok"):
            raise RuntimeError("Telegram did not return file details")
        file_path = payload["result"]["file_path"]
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        with urllib.request.urlopen(file_url, timeout=20) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "image/jpeg")
        return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=300"})
    except Exception:
        raise HTTPException(502, "Could not load this character image from Telegram.")
