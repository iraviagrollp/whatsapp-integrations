# wapp-integrations — the WhatsApp inbox

Every message customers send to the business number **+91 74161 89998**, kept on
this machine and answered from a browser at **https://whatsapp.ialreports.com**.

```
D:\Iravi InHouse\Reports\
  reports\            the report engine           (Python)
  reports-ui\         the reports console          (React)
  wapp-integrations\  this - WhatsApp              (Python)
```

It is a project of its own, like the other two: nothing here imports from them.
The reporting bot ("bills pending to Dhana since 45 days?") will plug into the
same webhook later.

---

## What it does

- **Receives** every message through Meta's webhook — text, photos, PDFs, voice
  notes, locations, reactions — and keeps it, with the files, in `data/`.
  Meta keeps no inbox for a Cloud API number and deletes received files after a
  while: **this is the only copy.**
- **Shows** them as chats, newest first, with unread counts and the sender's
  WhatsApp name.
- **Replies** — text, photos, PDFs — while the person's **24-hour window** is
  open (free). After that only an approved **template** may be sent, and the
  page offers one instead of the text box.
- **Delivery ticks** on everything sent: ✓ sent, ✓✓ delivered, blue ✓✓ read,
  ⚠ failed with Meta's reason. That includes the greetings sent by
  `D:\Customer Interactions\send_whatsapp_template.py`, read from its
  `sent_log.csv`.
- **Alerts** (optional): a WhatsApp to your own phone for each new message, with
  a link to the chat.

## The customer menu bot

When a **customer greets** the business ("Hi", "hii", "Hello sir", "Namaste",
"Good morning"), or taps the button on the services template:

```
<CUSTOMER NAME>! 🙏   How can we help you today?
        [ Ledger ]   [ Balance ]
  Ledger   -> their ledger statement PDF (same as the console's statement)
  Balance  -> "Your outstanding balance ... as on <today> is ₹X" (or advance / settled)
Do you need any more support?   [ Yes ]  [ No ]
  Yes -> the menu again      No -> "It was a pleasure serving you, ..."
```

- **Only a greeting starts it** (`bot.greetings` in `config.json`). Case,
  punctuation, doubled letters and "sir", "anna", "garu" around it are ignored.
  A greeting with a question ("hi, what is the rate of urea?") is a question,
  and goes to a person.
- **"Ledger" or "Balance" is always answered** - tapped on any menu, even an
  old one, or typed as a word - at any point in the conversation, or with none
  open. A bare "1" or "2" counts only as an answer to the menu itself.

- **Who counts as a customer:** their WhatsApp number matches the `MobileNo`
  on an active customer record in the ERP — spaces removed, a leading 91 or 0
  dropped, and exactly ten digits (starting 6–9) left. Records with no mobile
  are never matched. A number on two records is asked which account.
- **Suppliers** (their number on an active supplier record, matched the same
  way) are left to a person in the inbox, and the alert numbers are told.
- **Visitors** - on neither master - who greet get their own menu
  (`bot.visitor` in `config.json`):

  ```
  Welcome to IRAVI AGRO LIFE LLP.  How can we help you?   [ Reach us ]  [ Talk to us ]
    Reach us    -> the office as a WhatsApp location pin
    Talk to us  -> "Feel free to reach out to us on 8977417663 or write to us on ..."
  30 seconds after their first choice, once: the thank-you with the Instagram link
  ```

  Either button keeps working until the chat goes idle. Anything else they
  write goes to a person, and no thank-you is sent if a person replied first.
- **Anything else** is left to a person in the inbox, and the alert numbers are told.
- **The bot never talks over staff:** after someone replies from the inbox, it
  stays out of that chat for `pause_after_human_minutes` (60).
- After the goodbye, the next greeting (or a tap on the template's button)
  starts the menu again at once; a "thank you" is no greeting, so it is left
  for a person. A chat idle for `idle_minutes` (30) starts again from the menu.
- **It needs the reports service** (`reports\scripts\reports_web.py`, on
  127.0.0.1:8787) running: it finds the customer and makes the PDF there. If the
  service is down, the customer is told the ledger will follow and the alert
  numbers are told why.
- Every bot message shows in the inbox, marked 🤖 Bot.

Switch it off with `"bot": {"enabled": false}` in `config.json` and restart.

## How it is put together

```
 Customer ──► Meta ──► https://whatsapp.ialreports.com/webhook ──┐  Meta's signature checked
                                                                 │  (Access bypassed: Meta can't log in)
 You ──► Cloudflare Access ──► https://whatsapp.ialreports.com ──┤  your Google / email login
                                                                 ▼
                     cloudflared (the tunnel reports.ialreports.com already uses)
                                                                 ▼
                     this service on 127.0.0.1:8790  ──►  data\inbox.db + data\media\
```

| File | What |
|---|---|
| `src/wapp/webhook.py` | Meta's deliveries → stored messages and receipts; signature check |
| `src/wapp/store.py` | The SQLite record (`data/inbox.db`) |
| `src/wapp/graph.py` | Meta's Graph API: send, upload, download, templates |
| `src/wapp/templates.py` | What a template needs filled in, and filling it |
| `src/wapp/app.py` | The web service: webhook, inbox API, access check, alerts |
| `web/` | The page — plain HTML/CSS/JS, no build step |
| `config/config.json` | This machine's settings (git-ignored) |
| `config/app_secret.txt` | The Meta app secret (git-ignored) |

---

## Setting it up

Done already: the Python environment (`.venv`) and `config/config.json`, with a
random `verify_token` and the token read from `D:\Customer Interactions\Token.txt`.

### 1. The app secret

developers.facebook.com → **IRAVI HELPER BOT** → **App settings → Basic → App
secret → Show**. Put it, alone on the first line, in:

```
D:\Iravi InHouse\Reports\wapp-integrations\config\app_secret.txt
```

Without it the webhook refuses everything (the page shows a yellow warning).

### 2. Start the service

```powershell
cd "D:\Iravi InHouse\Reports\wapp-integrations"
.\start.cmd
```

Open <http://127.0.0.1:8790> on this machine. You should see the chats from
the greeting run.

### 3. Cloudflare Access — before the tunnel carries anything

Zero Trust → **Access → Applications → Add an application → Self-hosted**, twice:

1. **The inbox** — domain `whatsapp.ialreports.com`, policy **Allow** with your
   email(s). Same as `reports.ialreports.com`.
2. **The public paths** — domain `whatsapp.ialreports.com`, path `webhook`;
   add a second destination with path `privacy`. Policy action **Bypass**,
   include **Everyone**. Meta's servers cannot log in, so these two paths must be
   open; the service itself rejects any webhook not signed with the app secret.

Optional but recommended: copy the inbox application's **AUD tag** (Application
→ Overview) and your team name (Zero Trust → Settings → Custom pages, the part
before `.cloudflareaccess.com`) into `access_aud` and `access_team_domain` in
`config.json`. The service then checks every Access login itself as well —
a second lock, for the day a policy is changed by mistake.

### 4. Add the hostname to the tunnel

In `C:\Users\Iravi\.cloudflared\config.yml`, add the middle entry — **above**
the catch-all `http_status:404`:

```yaml
ingress:
  - hostname: reports.ialreports.com
    service: http://127.0.0.1:8080
  - hostname: whatsapp.ialreports.com
    service: http://127.0.0.1:8790
  - service: http_status:404
```

Then:

```powershell
cloudflared tunnel route dns a884cfe0-6ae3-44b0-87f8-99b6a11beacb whatsapp.ialreports.com
Restart-Service cloudflared        # or stop and start "cloudflared tunnel run"
```

Check: <https://whatsapp.ialreports.com> asks you to sign in;
<https://whatsapp.ialreports.com/privacy> opens without signing in.

### 5. Point Meta at it

developers.facebook.com → the app → **Connect on WhatsApp → Step 2. Production
setup → Configure Webhooks**:

- **Callback URL:** `https://whatsapp.ialreports.com/webhook`
- **Verify token:** the `verify_token` value in `config/config.json`
- **Verify and save**, then subscribe to the **messages** field (WhatsApp →
  Configuration → Webhook fields).

### 6. Publish the app

Dashboard → **Publish**. Meta asks for a **privacy policy URL**:
`https://whatsapp.ialreports.com/privacy`. Until the app is published, Meta sends
only test webhooks — real customers' messages do not arrive.

Then send "hi" to 74161 89998 from your phone: it should be in the inbox
within seconds.

### 7. Keep it running

```powershell
schtasks /create /tn "Iravi WhatsApp inbox" /sc onlogon /rl highest ^
  /tr "\"D:\Iravi InHouse\Reports\wapp-integrations\.venv\Scripts\python.exe\" \"D:\Iravi InHouse\Reports\wapp-integrations\scripts\serve.py\""
```

If the service is down, Meta retries each failed delivery, less and less often,
for a limited time (its documentation has said up to 7 days) — so a restart
loses nothing, but do not rely on the retries to cover a PC that is off for days.

---

## Settings worth knowing (`config/config.json`)

| Key | Meaning |
|---|---|
| `alert_numbers` | e.g. `["9701491148"]` — WhatsApp alert per new message. Free while you have messaged the business number in the last 24 h; otherwise needs `alert_template`. |
| `alert_template` | A **utility** template with two slots — who wrote, and what — used when the free window to the alert number is closed. Optional. |
| `send_read_receipts` | Opening a chat shows the customer blue ticks. `false` to stop that. |
| `send_logs` | The greeting script's log, so its sends show in the chats. |
| `allowed_emails` | Only these Access logins may use the inbox. |
| `privacy_contact` | Printed on the public privacy page. |

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

They run against a fake Meta — nothing is sent and no token is needed.

## Things to know

- **The PC is the system**, as with the reports console: off, asleep or
  disconnected for long, and messages are lost.
- **Back up `data/`.** It is the only record of the conversations.
- **Anyone Access lets in can read every chat and reply as the firm.** Keep the
  Access policy to the people who should.
- **Replies are as the business.** The page notes who sent each reply (from the
  Access login), but the customer sees only *Iravi Agro Life LLP*.
