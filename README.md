# mbaxAi — Storeroom monitor on Supabase

One website for your phone and laptop. Supabase stores the stock, sensor readings,
settings, and scans, so your laptop can be off. The ESP32 sends events over HTTPS;
a Python server, MQTT broker, and paid VM are no longer required for this version.

## Set it up

You need a Supabase project and a static website host. Supabase and Cloudflare
Pages offer free plans, subject to their current quotas and inactivity policies.
Free hosting does not provide an uptime guarantee. The steps below configure your
own accounts; this repository does not create or deploy a project automatically.

### 1. Create the Supabase backend

Create a project at [Supabase](https://supabase.com/). In its SQL editor, run
[`supabase/migrations/202610290001_storeroom.sql`](supabase/migrations/202610290001_storeroom.sql)
**once**. It creates the tables, checked stock operations, membership protection,
and live-update signal. Do not turn off row-level security.

Create your email/password user in Supabase Authentication with a confirmed email.
Disable public sign-ups for this private application. Then run this SQL, replacing
only the email with the account you created:

```sql
insert into public.members(user_id)
select id from auth.users where lower(email) = lower('YOUR_EMAIL_ADDRESS')
on conflict (user_id) do nothing;
```

Confirm that one membership exists. Repeat for other people you want to allow.
Signing in alone does not grant access: the account must also be in `members`.
Remove its membership to revoke access. Use Supabase's account-management tools
for password resets; the website does not offer public registration.

### 2. Deploy the device endpoint

Install Node.js **22.12 or newer** on your computer. From this project directory:

```sh
npx supabase login
npx supabase link --project-ref YOUR_PROJECT_REF
```

The project ref is the identifier in your Supabase project URL. Login and database
credentials are entered through the CLI's secure prompts, never in this repository.
If you used the SQL editor in step 1, do not also apply the same migration with
`db push`. For CLI-managed migrations instead, use `npx supabase db push` in place
of running the migration manually in the SQL editor.

Generate a long random device token and keep it securely:

```sh
node -e "console.log(require('node:crypto').randomBytes(32).toString('base64url'))"
```

Copy `supabase/.env.example` to `supabase/.env.local`. Set `DEVICE_TOKEN` to that
value (at least 32 characters). To receive Telegram stock alerts, also set your existing `TG_TOKEN` and
`TG_CHAT_ID`; otherwise leave those two values empty. This file is ignored by Git.
On macOS/Linux, restrict access with `chmod 600 supabase/.env.local`.

```sh
npx supabase secrets set --env-file supabase/.env.local
npx supabase functions deploy ingest-device --use-api --no-verify-jwt
npx supabase functions deploy process-alerts --use-api --no-verify-jwt
```

`--no-verify-jwt` is intentional: both functions perform their own authorization.
`--use-api` bundles on Supabase, so deployment does not require local Docker.
The device endpoint checks the private device token. Alert processing accepts that
same token or a verified signed-in member. Supabase provides the backend's service
key automatically; never put it in frontend configuration or ESP32 firmware.

### 3. Publish the website

In [Cloudflare Pages](https://pages.cloudflare.com/), connect this GitHub repository
and select the branch containing the Supabase version. Configure:

| Setting | Value |
| --- | --- |
| Build command | `npm ci && npm run build` |
| Output directory | `dist` |
| Environment variable `NODE_VERSION` | `24` |
| `VITE_SUPABASE_URL` | Your Supabase project URL |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | Your **publishable** key (legacy `anon` key also works) |

Find the URL and publishable key in your project's API/connect settings. These
are public browser configuration; row-level security and membership protect the
data. **Never use a secret/service-role key, device token, or Telegram token as a
`VITE_` value.** The build rejects known secret-key formats.

Deploy and open the resulting HTTPS website on your phone or laptop. Sign in with
the member account from step 1. Rebuild the website after changing its build
variables. Serve only `dist/`, which contains the website; do not publish the whole
checkout or any database/export files as a static directory.

### 4. Connect the ESP32

Update the device to POST readings and scans to:

```text
https://YOUR_PROJECT_REF.supabase.co/functions/v1/ingest-device
```

Send `Content-Type: application/json` and the private `X-Device-Token` header.
Examples of request bodies:

```json
{"type":"climate","t":22.5,"h":50}
```

```json
{"type":"scan","uid":"04:A1:B2:C3","event_id":"de305d54-75b4-431b-adb2-eb6b9e546014"}
```

Generate a fresh UUID for each physical scan. **Retry the same scan with the same
UUID** after a connection failure; it will reduce stock only once. Keep pending
scan payloads in persistent device storage until a 2xx response, including across
reboots. Network errors, 429, and 5xx can be retried with backoff. Fix invalid data
or credentials on 400/401/403 before retrying. Requests are limited to 64 KiB.

[`firmware/StoreroomSupabase.h`](firmware/StoreroomSupabase.h) provides a small
Arduino ESP32 HTTPS helper; [its guide](firmware/README.md) shows where to connect
it to your sensor/RFID code. Your actual firmware was not provided, so the helper
must be integrated there. Use verified TLS with the current trusted CA and an
NTP-synchronized clock. Do not use `setInsecure()`.

Send climate/status events at least once a minute so the dashboard can tell that
the device is online; a device becomes offline after 90 seconds without events.
Keep the ESP32 powered and connected to Wi-Fi when the laptop is off. The old MQTT
firmware will not feed the Supabase website until this device change is made.

## Existing stock and backups

The old SQLite snapshots remain in the repository and are not imported or changed
automatically. Stop the old Python server, then export the database with its WAL
and SHM sidecars together:

```sh
python3 scripts/export_sqlite.py --source storeroom.db --output stock-import.sql
```

Run the generated SQL in your Supabase project's SQL editor **after the schema
migration and before adding new stock or device events**. It imports products,
readings, scans, ignored tags, and settings in one transaction. It refuses a
nonempty target and does not send historical low-stock alerts. Review any invalid
legacy data reported by PostgreSQL rather than disabling constraints.

The export contains your private stock data: do not commit or publish it. It is
ignored by Git. Review the original already-tracked SQLite snapshots before making
this repository public. The former Python/Docker application is available in Git
history if needed; it is no longer the runtime for this version.

Use the dashboard's CSV downloads for reports. They export all available rows using
pagination, not just the latest page. For a complete database backup, use a
PostgreSQL dump or your provider's backup tools; CSVs do not replace a full backup.
Historical climate readings older than 365 days are pruned during new climate
uploads. Device-event IDs are retained so old scan retries remain idempotent.
Monitor storage and request usage against your project's free-plan limits.

## Alerts and live updates

The dashboard shows low/out-of-stock warnings without Telegram. Optional Telegram
messages are queued transactionally when a product first becomes low or out of
stock, and when its severity worsens. Restocking resets the alert level. Deleting
an item or clearing stock cancels pending warnings.

Device uploads and dashboard stock changes attempt to deliver pending messages.
Delivery failures leave them queued for retry after later activity (with a short
backoff). There is no separate always-running alert worker: without later device
or dashboard activity, a failed notification remains pending. Telegram delivery is
at least once; a network interruption after sending can cause a duplicate message.

Live updates use a member-protected change signal, with polling as a fallback.
Inventory rows are fetched through protected database operations. Signing out
clears the displayed stock and cancels stale exports/responses.

## Develop and test

```sh
npm ci
cp .env.example .env.local
# Set your project URL and PUBLIC publishable key in .env.local.
npm run dev
```

The dev server binds to the local machine. Without configuration, the website
shows a setup message. The production build is `npm run build`; static output is
in `dist/`. `index.html` and `dashboard.html` both open the same app.

```sh
npm test
npm run build
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/test_database.py
npx --yes deno@2.1.14 test --frozen --config supabase/functions/deno.json supabase/functions/_shared/validation_test.ts
npx --yes deno@2.1.14 check --frozen --config supabase/functions/deno.json supabase/functions/ingest-device/index.ts supabase/functions/process-alerts/index.ts
```

The PostgreSQL test requires Docker and creates/removes its own disposable local
container. It never connects to your Supabase project. It exercises the real
migration, roles, membership checks, stock/alert transactions, concurrent scan
retries, and the SQLite import. Browser/Edge tests use fixtures and mocks; a real
hosted-project check still needs your project configuration and device.
