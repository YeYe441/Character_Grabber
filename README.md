# AnimeCharacterGrabber — Complete Build

This package contains the broad feature-complete build based on the agreed project specification. It is designed to run in a Python environment such as GitHub Codespaces, Termux + Acode, Linux, macOS, or Windows.

**Important:** “Complete” here means the agreed gameplay features are implemented in the project, not that the software has been independently audited or proven production-ready. Run the checklist below with a test bot and test group before putting it into a large public group or taking real payments.

## Included features

- Per-group automatic spawning after randomly selected 50, 75, or 100 eligible human messages.
- Owner/Admin manual spawning.
- Eight rarities: Common, Uncommon, Epic, Legendary, Mythical, Divine, Limited, Customization.
- Limited rarity is selected at a 0.5% chance per normal spawn attempt when Limited characters exist.
- `/w` reveals the exact name; `/grab Character Name` atomically awards the character to the first correct user.
- Character upload by replying to a Telegram photo.
- ZIP image import; filenames become character names; optional `metadata.csv`.
- Persistent SQLite data and multiple copies of the same character.
- Profiles, collections, currency balances, a visual Telegram Mini App market with eight rarity filters, currency choice, budget filtering, sorting, pagination, and Silver/Gold/Diamond purchases.
- Owner-only currency minting and Admin/Owner transfers limited to their own balance.
- Daily Silver reward with a once-per-UTC-day cooldown.
- Gifts with recipient approval; character requests with owner approval.
- Character-to-character trades, optional currency sweetener, and target approval.
- Customization requests paid with Telegram Stars, configurable automatic/manual delivery.
- Events and free Customization prizes assigned to event participants.
- Admin roles, transaction history, character disabling, request moderation.

## 1. Set up your Telegram bot

1. Open Telegram and contact `@BotFather`.
2. Use `/newbot` and follow its instructions.
3. Keep the bot token private.
4. Obtain your numeric Telegram user ID.

## 2. Configure the project

Copy `.env.example` to `.env`, then set `BOT_TOKEN` and `OWNER_ID`. For the visual market, also set `WEBAPP_URL` to the public HTTPS URL where `webapp.py` is hosted. Telegram Mini Apps require HTTPS; a localhost address will not work for real users.

Do not commit `.env` to GitHub. `.gitignore` excludes it.

## 3. Run in Codespaces, Linux, macOS, or Windows

Python 3.10+ recommended.

```bash
python -m venv .venv
```

Activate it:
- Linux/macOS: `source .venv/bin/activate`
- Windows PowerShell: `.venv\Scripts\Activate.ps1`

Then:

```bash
python -m pip install -r requirements.txt
# Terminal 1: visual market server (use the same project folder and .env)
uvicorn webapp:app --host 0.0.0.0 --port 8000
# Terminal 2: Telegram bot
python bot.py
```

Codespaces stops when the codespace is stopped or times out. It is suitable for testing but is not automatically a permanent 24/7 host.

## 4. Run on Android with Acode

Acode is an editor; it does not itself guarantee a Python runtime. Use Termux or another compatible Python environment. Keep the project folder accessible to both apps.

```bash
pkg update
pkg install python
python -m pip install -r requirements.txt
# Terminal 1: visual market server (use the same project folder and .env)
uvicorn webapp:app --host 0.0.0.0 --port 8000
# Terminal 2: Telegram bot
python bot.py
```

Android may stop background processes due to battery management. For 24/7 operation, use an always-on host and persistent storage.

## 5. Add the bot to a group

- Give it permission to send messages and photos.
- To count regular group messages, configure BotFather `/setprivacy` → select the bot → Disable, then remove/re-add it to the group if Telegram requests it.
- The bot will not spawn another character while a spawn is active in the same group; a correct `/grab` clears the active spawn.
- Each group has its own counter.

## Commands

### Players
- `/start`
- `/profile`
- `/balance`
- `/collection` or `/inventory`
- `/market` — opens the visual Mini App market in private chat (or use the Market menu button).
- `/buy Character Name | silver` (or `gold` / `diamond`) — command fallback.
- `/daily`
- `/w`
- `/grab Character Name`
- `/gift USER_ID | Character Name` — sends a gift offer; recipient uses `/acceptgift ID` or `/rejectgift ID`.
- `/requestgift OWNER_USER_ID | Character Name` — asks the current owner; owner uses `/approverequest ID` or `/rejectrequest ID`.
- `/trade USER_ID | Your Character | Their Character` — optional currency: `/trade USER_ID | Your Character | Their Character | silver | amount`. Target uses `/accepttrade ID` or `/rejecttrade ID`.
- `/custom Character Name` — use in private chat to purchase a Customization character with Stars.
- `/claimprize Character Name` — claims a free event prize assigned by an Admin/Owner.

### Owner / Admin
- `/helpadmin`
- `/spawn Character Name` — in a group.
- `/addcharacter` — reply to a photo with `Name | Rarity | Silver | Gold | Diamond`.
- `/importzip` — Owner replies to an uploaded ZIP document with this command.
- `/setadmin USER_ID` and `/removeadmin USER_ID` — Owner only.
- `/mint USER_ID silver|gold|diamond AMOUNT` — Owner only.
- `/pay USER_ID silver|gold|diamond AMOUNT` — Admin/Owner may transfer only from their own balance.
- `/setprice Name | Silver | Gold | Diamond | Stars | automatic/manual` — Owner only.
- `/disablechar Character Name` — Owner only.
- `/eventstart Event title` and `/eventend`.
- `/prize USER_ID | Customization Character Name` — assign a free prize during an active event.
- `/approve REQUEST_ID` / `/reject REQUEST_ID` — paid Customization requests.
- `/transactions USER_ID` — Owner only.

## Visual market Mini App setup

The Mini App is in `miniapp/index.html`; its API is `webapp.py`. The bot and web server must use the same persistent SQLite file (`DB_PATH`) and the same `.env`. Deploy the web server on a host that provides a public HTTPS URL and persistent storage, then set `WEBAPP_URL` to that exact HTTPS origin (for example, `https://market.example.com`). Start the web server with `uvicorn webapp:app --host 0.0.0.0 --port 8000` and start `bot.py` separately. When the bot starts, it attempts to set Telegram's bot menu button to **Market**; `/market` also sends an Open Market button in private chat.

Telegram does not let bots add a custom permanent button directly beside the user's attachment/paperclip button. The closest supported experience is the bot's **Menu** button near the message box, plus the `/market` launch button. Configure HTTPS and persistent hosting before using this with real players. The Mini App verifies Telegram `initData` on its API and never trusts a user ID sent by the browser. The visual market currently supports in-game currency purchases (Silver, Gold, Diamond); Stars-based Customization purchases continue through `/custom`.

**Important deployment note:** if the bot and web app run on separate hosts, they must share the same database/storage; otherwise balances and purchases will not stay synchronized. Do not expose the database publicly or share the bot token.

## Character ZIP format

The image filename (without extension) becomes the character name, e.g. `Rukia Kuchiki.png`. Supported formats: JPG, JPEG, PNG, WEBP, GIF. The ZIP is uploaded to Telegram as a document, then reply to that ZIP with `/importzip`.

Optional `metadata.csv` headers:

```csv
name,rarity,silver_price,gold_price,diamond_price,stars_price,delivery
Rukia Kuchiki,Legendary,1000,0,0,0,automatic
Original Hero,Customization,0,0,0,150,manual
```

The import sends images to Telegram to obtain reusable Telegram file IDs, so the import can take time. Split large archives into smaller ZIPs. Duplicate names are skipped.

## Telegram Stars and real payments

- Stars invoices use Telegram currency code `XTR`.
- The bot validates invoice owner, character, currency, price, and duplicate Telegram charge IDs.
- Automatic delivery grants the character after a successful payment update. Manual delivery records a pending request for Owner approval.
- Rejecting a paid request does not automatically refund Stars. Refunds, disputes, chargebacks, and any platform obligations must be handled through Telegram's current supported payment process.
- Test with a test bot and review the current Telegram Stars/payment requirements before taking real money. Do not assume a test payment guarantees production readiness.

## Database / backups

SQLite is stored at `anime_character_grabber.db` by default. It is persistent on the same disk. Back up this file periodically and keep backups private. Do not delete it if you want to keep collections and balances.

## Before launch checklist

1. Test all commands with Owner, Admin, and Member accounts.
2. Test simultaneous correct `/grab` attempts.
3. Restart the bot and verify character data, inventories, and balances persist.
4. Test ZIP imports with duplicate names and metadata.
5. Test gifts/trades when an owner has multiple copies and when the offered copy has been transferred.
6. Use a Telegram test bot for Stars payment tests.
7. Keep logs and database backups; rotate the bot token if it leaks.
8. Use a suitable always-on host for 24/7 operation. Codespaces and local Android processes may stop.

This build is a strong starting point but has not been externally audited. Keep it private until the checklist passes.
