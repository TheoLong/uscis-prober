# USCIS Case Prober

[![License: AGPL v3](https://img.shields.io/badge/license-AGPL--3.0--or--later-blue.svg)](LICENSE)

**Pulls the real USCIS case + location APIs 3×/day and diffs
snapshots** to catch silent `updatedAt` bumps, event-code changes,
and service-center assignments that public status checkers miss.
Self-hosted, one config file.

### vs. other case checkers

- **Real API, not scraped status.** Calls two USCIS endpoints per pull:
  the case endpoint (`/case-service/api/cases/{id}` — full event + notice
  history) and the location endpoint (`/secure-messaging/api/case-service/
  receipt_info/{id}` — service center, subtype, receipt date). Logs
  complete snapshots of both.
- **Tracks service center assignment, not just events.** I-765s typically
  get a service center (e.g. `SCD`, `NSC`) before I-485 / I-131; you'll
  see it the moment USCIS flips the location API from `null` to populated.
- **Authenticates with your USCIS login.** Playwright-driven sign-in;
  MFA code auto-read from your inbox. No manual steps after setup.
- **Pulls 3×/day, keeps the full history.** Every snapshot is appended
  to disk — nothing overwritten. Case + location histories live in
  sibling files so their diffs stay independent.
- **Catches silent updates.** Internal `updatedAt` bumps that never
  show up in the public status, plus location changes (SCD → NSC etc.)
  that are effectively invisible on the public site.
- **Runs on your machine, easily deployable.** `python src/server.py`
  locally, or drop it on a $15/month AWS/Azure VM. No SaaS.

---

## Screenshots

Receipt numbers, applicant / representative names, letter IDs, and
event UUIDs are redacted; everything else (form types, event codes,
service-center codes, timestamps, counters) is exactly what the
dashboard renders. Shots show a single case — the live dashboard
stacks one card per configured receipt.

**Dashboard** — hero metrics, timeline, and the *Current location*
row (`TBD` with info popover until USCIS assigns a service center,
then populated inline e.g. `SCD · 147-C9`). Topbar chip shows the
running build version.

![Dashboard overview](docs/screenshot-dashboard.png)

**Raw JSON** — full USCIS payload with syntax highlighting. Sub-tabs
split between *Case API* and *Location API* streams so each endpoint's
history can be inspected independently.

![Raw JSON view](docs/screenshot-raw-json.png)

**Updates feed** — one row per diff across every case, newest first.
Location diffs carry a pink left-border + `LOCATION API` badge.

![Updates feed](docs/screenshot-updates.png)

---

## Quick start

```bash
git clone https://github.com/<you>/uscis-case-prober.git && cd uscis-case-prober
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.json config.json
# fill in config.json — full field reference + app-password walkthrough
# is in [Setup → Configure](#4-configure)

python src/server.py
# open http://127.0.0.1:8080
```

After cloning, **one file** is all you touch: `config.json` (gitignored).
See [Setup → Configure](#4-configure) for the full field reference and
[Setup → Obtain an app password](#3-obtain-an-app-password) for how to
get the credentials your email provider needs.

---

## How it works

**Core principle: snapshot everything, diff nothing away.**

- **Two APIs pulled per case, snapshots stored separately.** Each pull
  hits both endpoints and writes sibling files:
    - `data/{formNum}_case.json` — case-endpoint response (events,
      notices, flags).
    - `data/{formNum}_location.json` — location-endpoint response
      (service center, subtype, receipt date). Stores the full envelope
      including `{"data": null}` so the moment USCIS flips a case from
      unassigned to assigned is a real diff, not an inference.
- **Each snapshot is the full API payload**, not a summary string.
  One row per pull per endpoint, ISO-8601 timestamped. No row is ever
  deleted or overwritten.
- **Diffs are recomputed on the fly** from that append-only history.
  Restart, reboot, code change — never loses a record, never
  double-counts one. Case-API and location-API diffs are merged into
  one chronological feed, each tagged with its `source` so the UI can
  style location rows distinctly.
- **Every change gets classified.** For the case API: `event` (new
  event code — FTA0, APR0, etc.), `notice` (Request for Evidence /
  receipt / appointment letter), `appointment` (biometrics
  rescheduled), `decision` (`closed` / `actionRequired` flipped),
  `silent_update` (`updatedAt` date advanced with nothing else
  visible), or `same_day_refresh` (same-day re-stamp, sync artifact).
  For the location API: `location_assigned` (null → populated),
  `location_changed` (service center or subtype changed),
  `location_cleared` (populated → null — rare).
- **Email notifications.** One email per new diff per pull, across
  both APIs. Record IDs embed the source (`{receipt}:case:{date}` /
  `{receipt}:location:{date}`) so a same-day case + location diff
  emits two distinct emails, not a collapsed one. Before / after
  diff-ID snapshotting around each pull → no duplicates, no misses,
  survives restarts.
- **One pull → one system-log row.** Each pull produces a single
  consolidated `pull` entry with its 15+ internal steps (auth, case
  fetch, location fetch, snapshot append, notify) nested as `steps[]`.
  Top-level tone = worst-child severity, so an otherwise green pull
  containing a single failed location fetch still shows up as yellow.
- **Every pull is a cold start.** `.uscis_session.json` is wiped at
  the start of every pull and never persisted at the end, so every
  scheduled or manual pull exercises the full OIDC + MFA flow. Login
  regressions surface at the next scheduled fire, not days later when
  a stale cookie expires.
- **Full Playwright trace on failure (or in debug mode).** Every pull
  records a native Playwright `trace.zip` (DOM snapshots, network,
  console, screenshots) in memory. On failure — or on every pull when
  you flip the Debug-mode pill in the topbar — the zip is written to
  `data/full_traces/<ts>_fail_.../` alongside an `mfa_trace/` sidecar
  containing wire-level IMAP events (`events.jsonl`) and every raw
  email considered (`email_<uid>.eml`). Click **Open trace** on any
  pull row to replay it in the self-hosted Playwright viewer; click
  **MFA events** for a modal that decodes the sidecar into a
  filterable table + rendered email previews.
- **Build version visible in the dashboard.** Top-left chip reads
  e.g. `2026-04-23.0032` — the commit's authored time in UTC. String
  comparison between two labels matches chronological order, so you
  can tell at a glance whether what you just pushed has landed on the
  VM. `GET /api/version` returns the same info unauthenticated for
  deploy-verification scripts.
- **One-click export.** `/api/export` (or the "Export data" button)
  bundles every `data/*_case.json`, every `data/*_location.json`, and
  a manifest into a timestamped zip. Useful for sharing with a lawyer
  or archiving.

### Design invariants (don't break these)

- `config.json`, `data/*.json`, `.uscis_session.json`, and
  `.flask_secret` are **gitignored**. Never commit them.
- `uscis_auth.py` is the **only** module allowed to trigger MFA.
  Fetch / extract code paths must never call the login flow directly.
- All timestamps in snapshot logs are ISO-8601 UTC. Display-time
  localisation happens in the browser only.
- Chromium stays out of the Flask process — each pull is a separate
  subprocess so the web server never manages Playwright lifetimes.

---

## Setup

### 1. Prerequisites

- Python 3.10 or newer
- A system that can run headless Chromium (macOS, Linux, WSL)
- Outbound network to `my.uscis.gov` and your email provider's
  IMAP (993) + SMTP (587) endpoints — see the provider table below

```bash
python3 --version            # must be >= 3.10
command -v git >/dev/null    # must exist
```

### 2. Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

Verify:

```bash
python -c "import flask, apscheduler, playwright"   # silent on success
playwright --version                                 # prints a version
```

### 3. Obtain an app password

An **app password** is a scoped credential (usually 16 characters) your
email provider issues for IMAP/SMTP access by third-party programs. It
is *not* your regular account password.

**Why:** once MFA / 2FA is on (effectively mandatory for all modern
providers), your regular password stops working for IMAP/SMTP. The
provider expects an app password instead. Properties:

- Scoped to mail only — can't log into the web UI, read chat, etc.
- Individually revocable — delete just the tracker's password without
  touching anything else.
- Displayed exactly once on creation. Copy it immediately.

The tracker stores it in `config.json` and uses it to:

1. Read the USCIS MFA email over IMAP to get the 6-digit code.
2. Send diff-notification emails over SMTP.

IMAP/SMTP hosts are auto-detected from your email domain (see
`src/providers.py`), so you never enter them. Supported out of the box:

<details>
<summary><strong>Gmail / Google Workspace</strong></summary>

1. Enable 2-Step Verification: <https://myaccount.google.com/security>
   → *2-Step Verification* → turn on.
2. Visit <https://myaccount.google.com/apppasswords>.
3. In *App name* type something like `USCIS tracker`, click *Create*.
4. Copy the 16-character string (spaces are ignored).
5. Enable IMAP in Gmail: *Settings → See all settings →
   Forwarding and POP/IMAP → IMAP access: Enable*.
</details>

<details>
<summary><strong>Outlook / Hotmail / Microsoft 365</strong></summary>

1. Sign in at <https://account.microsoft.com/security>.
2. *Security dashboard → Advanced security options → App passwords →
   Create a new app password*.
3. Copy the generated password.
4. MFA must be on; Microsoft will prompt you to enable it otherwise.
</details>

<details>
<summary><strong>iCloud Mail</strong></summary>

1. Sign in at <https://appleid.apple.com>.
2. *Sign-In and Security → App-Specific Passwords → Generate an
   app-specific password*.
3. Enter a label (e.g. `USCIS tracker`), copy the 16-character password.
4. MFA is required — Apple won't show this option otherwise.
</details>

<details>
<summary><strong>Yahoo Mail</strong></summary>

1. Sign in at <https://login.yahoo.com/account/security>.
2. Enable *2-Step Verification* if off.
3. *Generate app password → Other app*, name it `USCIS tracker`, click
   *Generate*.
4. Copy the password.
</details>

<details>
<summary><strong>Fastmail</strong></summary>

1. <https://app.fastmail.com/settings/security/passwords>.
2. *New App Password*. Scope: *IMAP & SMTP*. Label it `USCIS tracker`.
3. Copy the password.
</details>

Using a provider not listed above? Add one line to `src/providers.py`
with its IMAP/SMTP hosts; no config-schema change needed. Any provider
offering IMAP + SMTP-with-STARTTLS + app passwords will work.

### 4. Configure

```bash
cp config.example.json config.json
```

Fill in `config.json`:

```json
{
  "cases": [
    { "id": "IOE0000000001", "label": "I-485" },
    { "id": "IOE0000000002", "label": "I-765" },
    { "id": "IOE0000000003", "label": "I-131" }
  ],
  "auth": {
    "uscis_email":            "you@example.com",
    "uscis_password":         "your-uscis-password",
    "uscis_mfa_email":        "you@example.com",
    "uscis_mfa_app_password": "16charapppassword",
    "optional_access_code":   "",
    "notification_email":     ""
  },
  "retry": 2,
  "retry_wait_seconds": 180,
  "storage_limit_gb": 1.0
}
```

| Field | Required? | Value |
|---|---|---|
| `cases[].id` | yes | USCIS receipt number (`IOE…`). One entry per case to track. |
| `cases[].label` | yes | Short label shown in the UI (e.g. `I-485`). Must contain a form number. |
| `auth.uscis_email` / `uscis_password` | yes | `my.uscis.gov` login. |
| `auth.uscis_mfa_email` | yes | Inbox where USCIS MFA emails land. Any major provider. |
| `auth.uscis_mfa_app_password` | yes | App password for that inbox (see Step 3). |
| `retry` | yes | Auth-failure retries per scheduled pull (int, ≥0). Start with `2`. Only auth failures retry; timeouts and config errors do not. |
| `retry_wait_seconds` | yes | Wait between retry attempts, in seconds (int, ≥0). `180` is a good default — long enough for a transient anti-bot block to clear. |
| `storage_limit_gb` | yes | Disk budget across `data/` + session/config files. Storage bar in the System tab tracks it; one alert email fires when crossed. Legal range 0.1–10.0. |
| `auth.optional_access_code` | no | Recommended when deployed remotely. When non-empty, dashboard requires this code to view. |
| `auth.notification_email` | no | Override recipient for diff-update emails. Defaults to `uscis_mfa_email`. |
| `trace_successful_pulls` | no | When `true`, every pull preserves its Playwright trace (useful for verifying capture against a green pull). Defaults to `false`; toggle live via the Debug-mode pill in the dashboard. |

Verify:

```bash
python -c "import json; c=json.load(open('config.json')); \
  assert c['auth']['uscis_email'] and c['auth']['uscis_mfa_app_password'], \
  'auth fields are empty'; \
  assert c['cases'], 'no cases configured'; \
  print('config ok:', len(c['cases']), 'case(s)')"
```

---

## Running locally

### 1. Run the tests

```bash
pytest -q
```

Expected: `350+ passed`, 100% line coverage across `src/`. Any failure
here means a dependency / install issue — fix it before moving on.

### 2. First login (one-off; triggers an MFA email)

```bash
python src/session_fetch.py login
```

Headless Chromium signs in to `my.uscis.gov`, polls your inbox for the
MFA code, and saves the session to `.uscis_session.json`. This file is
used only by the `extract` CLI subcommand below for debugging —
scheduled and manual pulls (`run` / `/api/pull`) deliberately wipe it
before each pull so every run exercises the full OIDC + MFA flow.

```bash
ls -la .uscis_session.json        # must exist and be non-empty
```

### 3. Test one pull

```bash
python src/session_fetch.py extract
```

`extract` uses the saved session but **refuses** to re-login — safe to
iterate with without burning more MFA codes. Success writes two rows
per case — one to `data/{formNum}_case.json` (case API), one to
`data/{formNum}_location.json` (location API).

> Note: `extract` is a debug / inspection tool. The production pull
> path is `python src/session_fetch.py run` (called by `/api/pull` and
> the scheduler), which starts from a clean slate every time — the
> saved `.uscis_session.json` is wiped at the start of `run` and not
> re-created at the end.

```bash
ls data/*.json                    # two files per form number + system_log.json
python -c "import json; print('captures:', len(json.load(open('data/485_case.json'))))"
python -c "import json; d=json.load(open('data/765_location.json'));\
  print('765 location latest:', d[-1]['data'])"
```

### 4. Start the dashboard

```bash
python src/server.py
```

```
... INFO server: Scheduler started: daily pulls at 07:00, 14:00, 20:00 (America/New_York)
 * Running on http://127.0.0.1:8080
```

Open <http://127.0.0.1:8080>. Cases render with Overview / Changes /
Raw JSON tabs, plus a global Updates feed and a System tab (storage
breakdown + paginated event log). Pulls run on the schedule; click
**Pull update** for an ad-hoc probe; **Export data** downloads the
full zip archive; **Export log** (inside System) downloads the
system log + every preserved trace.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Missing auth keys in config.json` | A required `auth` field is empty. All four credential fields must be set. |
| `No USCIS MFA code … within 180s` | IMAP didn't see the email. Verify the app password works (paste into any IMAP client), IMAP access is on, and the USCIS email actually arrived. |
| `AuthError: Session is stale and allow_login=False` | Run `python src/session_fetch.py login` to refresh. |
| `HTTP 429` on `/api/login` | Brute-force guard tripped. Wait 5 min per IP, or restart the server to reset. |
| Dashboard shows no cases | Check `data/*_case.json` exists. If empty, the pull step didn't populate them. |

---

## Deployment (Azure VM + Caddy + CI/CD)

Optional — skip if you're running only locally.

### 1. Create the VM

```bash
RG=rg-uscis-checker
az group create --name $RG --location eastus2

ssh-keygen -t ed25519 -f ~/.ssh/uscis_checker -N ""

az vm create \
  --resource-group $RG \
  --name uscis-checker-vm \
  --image Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest \
  --size Standard_B1ms \
  --admin-username azureuser \
  --ssh-key-values ~/.ssh/uscis_checker.pub \
  --public-ip-sku Standard \
  --storage-sku StandardSSD_LRS \
  --os-disk-size-gb 30 \
  --nsg-rule SSH

# Optional friendly DNS
az network public-ip update \
  --resource-group $RG \
  --name uscis-checker-vmPublicIP \
  --dns-name <label>   # → <label>.eastus2.cloudapp.azure.com
```

### 2. First-time provisioning on the VM

```bash
sudo apt-get update
sudo apt-get install -y python3-venv git caddy
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub     # save for step 3

cat >> ~/.ssh/config <<'EOF'
Host github.com
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF

git clone git@github.com:<you>/<repo>.git ~/uscis-checker
cd ~/uscis-checker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp config.example.json config.json
# edit config.json — set auth.optional_access_code to a random string
# (strongly recommended when the server is reachable from the internet)

# systemd unit
sudo tee /etc/systemd/system/uscis-checker.service <<'EOF'
[Unit]
Description=USCIS Case Prober dashboard
After=network-online.target

[Service]
Type=simple
User=azureuser
WorkingDirectory=/home/azureuser/uscis-checker
ExecStart=/home/azureuser/uscis-checker/venv/bin/python src/server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now uscis-checker

# Caddy reverse proxy (automatic HTTPS via Let's Encrypt)
sudo tee /etc/caddy/Caddyfile <<'EOF'
<your-hostname> {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080
}
EOF
sudo systemctl restart caddy
```

Open ports 80 + 443 in the Azure NSG; keep 8080 closed to the public
so traffic only reaches Flask through Caddy.

### 3. Register the VM's deploy key on GitHub

Back on your laptop (`gh` CLI installed):

```bash
gh repo deploy-key add <(ssh $VM 'cat ~/.ssh/github_deploy.pub') \
  --repo <you>/<repo> --title "uscis-checker-vm"
```

### 4. Add GitHub Actions secrets

| Secret | Value |
|---|---|
| `VM_HOST` | Public IP or DNS of the VM |
| `VM_USER` | SSH login (`azureuser` by default) |
| `VM_SSH_KEY` | **Private** SSH key reaching the VM (`~/.ssh/uscis_checker`) |

```bash
gh secret set VM_HOST --repo <you>/<repo> --body "<ip-or-host>"
gh secret set VM_USER --repo <you>/<repo> --body "azureuser"
gh secret set VM_SSH_KEY --repo <you>/<repo> < ~/.ssh/uscis_checker
```

After this, every push to `main` runs tests → SSHes to the VM →
`git reset --hard origin/main` → reinstalls deps if `requirements.txt`
changed → restarts the systemd unit → smoke-checks `/login`.

---

## Reference

### Project layout

```
src/
├── server.py          Flask app + scheduler + build-version resolver.
│                      Owns the consolidated pull-envelope logging:
│                      spawns session_fetch as a subprocess with
│                      USCIS_LOG_JSONL_STDERR=1, collects the child's
│                      events, folds in any server-process events via
│                      thread-local sys_log capture, writes one `pull`
│                      row.
├── session_fetch.py   CLI: `run` / `login` / `extract`. Spawned by
│                      server.py on schedule / button. Writes case +
│                      location snapshots to disk.
├── uscis_auth.py      OpenID Connect + MFA login flow. The *only* module
│                      that burns an MFA code.
├── uscis_api.py       Two endpoints: case (`/cases/{id}`) and location
│                      (`/secure-messaging/api/case-service/receipt_info/
│                      {id}`). Both navigate inside an authenticated tab.
├── mfa_mailbox.py     Polls IMAP for the USCIS MFA code. Provider-
│                      agnostic — host auto-selected from email domain.
├── providers.py       Email-domain → IMAP/SMTP host lookup.
├── diff_utils.py      Pure functions: day-bin, classify case diff,
│                      classify location diff (assigned/changed/cleared),
│                      merge the two feeds chronologically, summarize.
├── access_gate.py     Optional session-cookie gate + brute-force guard.
├── mailer.py          Email formatting + SMTP send (any provider).
│                      Every failure stage emits a categorised smtp_*
│                      event that folds into the pull envelope.
├── system_log.py      Append-only event store with thread-local and
│                      JSONL-stderr capture modes. Powers the System
│                      log tab; paginated 100/page.
└── static/            Dashboard UI (index.html + app.js + style.css).

tests/                 pytest — 350+ tests, 100% line coverage on src/.
data/                  Snapshot logs. Gitignored.
  {num}_case.json      Case-API snapshot history per form.
  {num}_location.json  Location-API snapshot history per form.
  system_log.json      Structured event log (rotates at 5000 entries).
config.json            Your secrets. Gitignored.
config.example.json    Template.
```

### UI & operational features

Beyond the snapshot/diff core:

- **Dashboard views.** Per-case Overview (with the Current Location
  row), Changes (merged case + location diffs), Raw JSON (Case API /
  Location API sub-tabs); a global Updates feed; a System tab with a
  stacked storage bar (per case + system log) and a paginated system
  log; live countdown to the next scheduled pull; build-version chip
  in the topbar; Debug-mode pill next to it that flips
  `trace_successful_pulls` live (next pull preserves its trace
  regardless of outcome).
- **Login isolation.** `uscis_auth.py` is the only module that burns an
  MFA code. `extract` refuses to log in — safe for iterating on
  scraping logic without spamming your inbox. Fresh-session policy in
  `cmd_run` means scheduled and manual pulls always exercise the full
  login + MFA flow; the saved session file is *only* used by the
  `extract` debug subcommand.
- **Comprehensive failure logging.** Every exit/failure point in
  every module emits a categorised `sys_log` event (SMTP stage-by-
  stage, Flask secret I/O, scheduler dispatch, pull thread crashes,
  route exceptions). One pull produces one consolidated envelope even
  when 20+ internal events fire; failures never disappear silently.
- **Optional access gate.** Signed session cookie, constant-time code
  comparison, 5-per-5-min brute-force guard, 30-day lifetime. The
  secret key is derived from `optional_access_code` — rotating the
  code invalidates every existing session on restart.
- **CI/CD.** GitHub Actions runs `pytest` on every push/PR; on `main`
  it SSHes to the VM, `git reset --hard`s, restarts the systemd unit,
  and smoke-checks `/login`. `GET /api/version` returns the running
  commit SHA + authored time — useful for deploy-verification scripts.

### Configuration (`config.json`)

One file. Gitignored. Minimum viable shape:

```json
{
  "cases": [
    { "id": "IOE…", "label": "I-485" }
  ],
  "auth": {
    "uscis_email":            "…",
    "uscis_password":         "…",
    "uscis_mfa_email":        "…",
    "uscis_mfa_app_password": "…"
  },
  "retry": 2,
  "retry_wait_seconds": 180,
  "storage_limit_gb": 1.0
}
```

Required keys: `cases`, `auth` (with all four credential fields),
`retry`, `retry_wait_seconds`, `storage_limit_gb`. Optional runtime
fields — `trace_successful_pulls` (bool), `optional_access_code`,
`notification_email` — default sensibly when absent.

See Setup → Configure for the full field table with optional overrides.

---

## License

**GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later).**
See [LICENSE](LICENSE) for the full text.

Copyright (C) 2026 the USCIS Prober contributors.

Short version: you're free to use, modify, and redistribute this code.
If you deploy a **modified** version — including as a network service
your users reach over the internet — you must make the modified
source available to those users under the same license. Keeping
this project and any derivatives open is the point.

There is **no warranty**. See sections 15 and 16 of the license.
