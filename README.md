# Telegram Paid-Channel Access Bot

Built with **Python 3.10+**, **python-telegram-bot 20.6**, **asyncpg** and **Neon PostgreSQL** (polling mode).

Customers pay outside Telegram. A helper confirms the payment inside the bot and chooses which private
channels the customer gets. The customer sees every channel as a button (🔒 locked, ✅ unlocked), and the bot
approves only that customer's join request.

## How it works

1. The customer pays you outside Telegram and tells a helper.
2. The helper **forwards the customer's message or screenshot to the bot** (or runs `/grant @username`).
   The bot reads the customer ID and shows a card: tick the channels → **Approve** → enter the amount.
3. The customer opens the bot (`/start`) and sees all channels. Paid ones are ✅, the rest 🔒.
   Tapping ✅ gives a join-request link. The bot, being admin, **auto-approves that user**. Anyone else who
   uses the link is declined.
4. **If a forward hides the customer's ID**: the helper tells the customer to send the screenshot to the bot.
   The screenshot goes to the helper chat; the helper decides whether it is genuine, picks the channels
   and approves, or rejects. A rejected (or unrecognised) customer is told to contact your support contact.
5. By `/grant @username`: if the customer never opened the bot, access is stored and unlocks when they first
   open it.
6. Helpers can **revoke** a channel (`/revoke @username` or the 🗑 button on a card). The customer is removed
   from the channel. There is no expiry.
7. The **master log** chat receives everything: new customers, screenshots, grant cards opened, grants (with
   screenshot, channels, amount, helper, time), invite links issued, joins, declined joins, pending access claimed,
   revokes, rejections, helpers added/removed and support-contact changes. `/admin` → Recent activity shows the same feed.

## Three interfaces

| Interface | Who | What they see |
|---|---|---|
| **Admin** | The Telegram ID in `OWNER_ID` (.env) | 🛠 Admin panel: 👥 Helpers (add / remove), 📈 Helper stats, 📊 Sales report, 🧾 Recent activity, 📞 Support contact, 👀 Customer view |
| **Helper** | Anyone the admin adds | Panel: ➕ Grant access, 🗑 Revoke access, 📈 My stats, 👀 Customer view. Also forwards a customer's message to open a grant card, and approves/rejects screenshots in the helper chat |
| **Customer** | Everyone else | Channel buttons 🔒 locked / ✅ unlocked, and sends the payment screenshot |

Everyone just sends `/start` and gets the interface for their role; everything is done with buttons.
The admin can also act on cards in the helper chat if needed. To add a helper: Admin panel → Helpers →
Add helper, then send their numeric ID, their @username (they must have opened the bot once) or forward a
message from them. Slash commands (`/grant`, `/revoke`, `/mystats`, `/report`, `/admin`) still work as shortcuts.

## Channels, links and categories (admin panel)

- **📡 Channels:** make the bot an admin in your channel/group, then **Add channel / group**. It shows up in a
  list to pick from (or enter the chat ID manually). You can then **set a join-request link** (the one customers
  will get; it must have "Request admin approval" ON) or skip it and the bot creates a private link per customer.
  Whenever a paid customer requests to join, the bot approves that exact user automatically; everyone else is declined.
  You can also rename or remove a channel.
- **🗂 Categories:** create a category (e.g. *Gold Plan*) and tick which channels belong to it.
- **Helper card:** besides one button per channel, helpers get a **Category** button (unlocks the whole bundle),
  and a **🌐 Approve ALL channels** button (selects every channel and goes straight to the amount step).

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Get your numeric ID from [@userinfobot](https://t.me/userinfobot).
3. Create two chats: a **helper chat** (screenshots arrive here, add your helpers) and a **master log** chat.
   Add the bot to both. Get their IDs (they look like `-100…`).
4. Start the bot, then add your channels from **Admin panel → 📡 Channels** (no code editing needed). `SEED_CHANNELS` at the top of `features.py` is optional and only used on the very first start.
5. Make the bot an **admin in every private channel** with **Invite Users via Link** and **Ban Users**
   (Ban Users is used to remove people on revoke). The bot logs a warning at start-up if something is missing.
6. Copy `.env.example` to `.env` and fill it in (see below).
7. Run:
   ```
   pip install -r requirements.txt
   python main.py
   ```

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | yes | Token from BotFather |
| `DATABASE_URL` | yes | Neon connection string |
| `OWNER_ID` | yes | Your numeric Telegram ID |
| `HELPER_CHAT_ID` | yes | Chat where customer screenshots are verified |
| `MASTERLOG_CHAT_ID` | yes | Chat for the activity log |
| `SUPPORT_CONTACT` | no | Contact shown to rejected / unrecognised customers (also editable in `/admin`) |
| `CURRENCY` | no | Currency symbol, default `₹` |
| `TIMEZONE` | no | Default `Asia/Kolkata` |

## Good to know (Telegram limits)

- A bot can't look up an arbitrary @username. It only knows people who opened the bot or whose messages were
  forwarded to it. That is why `/grant @username` waits for the customer's first message to the bot.
- Forwards from users with forward privacy on don't reveal the ID. The screenshot route covers this.
- Join-request links can't be limited to one use. Security comes from the bot approving only users who paid.
  Do not use other "request to join" links in these channels, because the bot declines join requests from
  people without access.
- In a group, the bot only sees helper replies if group privacy is off, or the helper replies to the bot's
  amount prompt (the prompt is a reply request, so it works with privacy on). Helpers can also work in a
  private chat with the bot.

## Deploying (Railway / VPS)

**Use Python 3.12 (or 3.10-3.12).** `python-telegram-bot==20.6` crashes on Python 3.13 with
`'Updater' object has no attribute '_Updater__polling_cleanup_cb'`. The repo includes `.python-version` pinning 3.12
(do not add an exact patch version such as 3.12.7: the builder can fail to verify it). If your host still picks 3.13, set the variable `RAILPACK_PYTHON_VERSION=3.12`
(Railway's current builder) or `NIXPACKS_PYTHON_VERSION=3.12` (older builder).


Upload the project to GitHub, create a Railway service from it and add the environment variables above.
The `Procfile` starts the bot as a worker (`python main.py`). Don't upload `.env`.

## Database

Tables are created automatically: `transactions` (sales with amount and helper), `entitlements` (who owns which
channel), `pending_grants`, `drafts`, `helpers`, `users`, `settings`, `activity_log`, `inputs`. Older tables from the
previous version (`sessions`, `helper_selections`) are not used any more and are left alone.

## Files

- `main.py` - static engine: env checks, database pool, start-up, polling
- `features.py` - all handlers, DB schema, admin panel (edit `CHANNELS` here)
- `requirements.txt`, `.env.example`, `.gitignore`, `Procfile`
