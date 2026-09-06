# Claire Voyant

**Your personal M365 Senior Administrative Strategist.**

An email-driven executive assistant that runs as an Azure Functions app against
a single Microsoft 365 mailbox. It triages the inbox and drafts replies, sends a
daily agenda, surfaces events buried in mail, keeps a Jira board honest, and
clears out junk once a week — and it only ever emails you when something
actually needs a decision.

Every action that changes anything is a button in an email. Nothing is sent,
booked, moved or deleted without a click.

---

## What it does

| | When | What arrives |
|---|---|---|
| **Inbox triage** | Hourly, 07:00–16:00, Mon–Fri | One recap of mail that owes a reply, each with a drafted response and Approve / Edit / Discard buttons |
| **Daily agenda** | 06:00 for today, 15:00 for tomorrow | Meetings, free-time chips you can claim, travel flags, and your open tasks |
| **Events digest** | Noon Tuesday and Friday | Events found in two weeks of inbox *and* junk — accept, tentative, or never show me again |
| **Junk sweep** | Reviewed noon Friday, emptied 17:00 Friday | The junk that isn't junk, with rescue / rescue-and-calendar / block buttons |
| **Task digest** | 07:00 Monday | Everything open on your board. Reply in plain English and Jira updates itself |
| **Team digest** | 15:00 Sunday, 08:00 Monday | Per-person finished and open lists, plus written summaries |
| **Contacts sweep** | 08:30 Monday | Adds everyone you *replied to* by hand, so type-ahead works |
| **Health check** | 05:45 daily | Proves the credentials still work by using them |

Two features need configuration before they do anything: the **team digest**
needs `TEAM_ROSTER`, and anything Jira needs a token. The rest works with a
mailbox and an Anthropic key.

### Design rules it holds to

- **Silence is the default.** No mail is sent unless something needs you.
- **GET renders, POST acts.** Clicking a button in an email opens a
  confirmation page; the action happens on that page. Link scanners follow every
  URL in your inbox within seconds of delivery, so a GET that sent mail would be
  sent by the scanner, not by you.
- **Nothing is destroyed.** Every delete is a move to Deleted Items.
- **It never marks your mail as read.** Unread is your signal, not its.
- **Availability is never written by a model.** Free times are read from the
  calendar and inserted by code; an invented slot costs a real double-booking.
- **Email content is data, never instruction.** Mail containing text aimed at an
  AI is classified suspicious, gets no draft, and is quarantined in its own
  section of the recap.

---

## Requirements

- An Azure subscription, and a Microsoft 365 mailbox you control
- Python 3.11
- [Azure Functions Core Tools v4](https://learn.microsoft.com/azure/azure-functions/functions-run-local)
  and the Azure CLI
- An [Anthropic API key](https://console.anthropic.com/)
- Optional: a Jira Cloud site and API token

---

## Setup

### 1. Create the Azure resources

```bash
RG=my-assistant-rg
LOCATION=eastus
APP=my-assistant-app                 # must be globally unique
STORAGE=myassistantstore             # 3-24 lowercase letters and digits
VAULT=my-assistant-vault             # must be globally unique

az group create -n $RG -l $LOCATION
az storage account create -n $STORAGE -g $RG -l $LOCATION --sku Standard_LRS
az functionapp create -g $RG -n $APP --storage-account $STORAGE \
  --flexconsumption-location $LOCATION --runtime python --runtime-version 3.11
az keyvault create -n $VAULT -g $RG -l $LOCATION --enable-rbac-authorization true
```

Give the app a managed identity and let it read secrets:

```bash
az functionapp identity assign -g $RG -n $APP
PRINCIPAL=$(az functionapp identity show -g $RG -n $APP --query principalId -o tsv)
VAULT_ID=$(az keyvault show -n $VAULT --query id -o tsv)
az role assignment create --assignee $PRINCIPAL \
  --role "Key Vault Secrets User" --scope $VAULT_ID
```

You will also need **Key Vault Secrets Officer** on the vault for yourself, to
write the secrets in the next steps.

### 2. Register the application

In the Entra admin centre, create an app registration:

- **Redirect URI**, type *Web*: `http://localhost:8400/callback`
- **Certificates & secrets** → new client secret; copy the value now
- **API permissions** → Microsoft Graph → *Delegated*:

  | Permission | Why |
  |---|---|
  | `offline_access` | Refresh tokens, so it keeps working |
  | `Mail.ReadWrite` | Read the inbox, create and move drafts |
  | `Mail.Send` | Send an approved reply |
  | `Calendars.Read` | The agenda, and real availability |
  | `Contacts.ReadWrite` | Add people you reply to |
  | `Calendars.ReadWrite` | *Only* if you want the block-time and add-to-calendar buttons |

Grant admin consent. Note the tenant ID and client ID.

> **`Calendars.ReadWrite` is opt-in on purpose.** Scopes are bound to the token,
> not the request: asking for a scope the consent does not cover makes *every*
> Graph call fail, not just the calendar ones. Set `GRAPH_CALENDAR_WRITE=true`
> only after consenting to it, and re-run the token script when you do.

### 3. Get a refresh token

```bash
export GRAPH_TENANT_ID=<tenant-id>
export GRAPH_CLIENT_ID=<client-id>

python scripts/get_refresh_token.py url          # open the URL it prints, sign in
python scripts/get_refresh_token.py exchange "<the full localhost URL you land on>"
```

Sign in as the mailbox owner, not an admin. The script writes the refresh token
straight into Key Vault and never prints it.

### 4. Store the secrets

```bash
az keyvault secret set --vault-name $VAULT --name graph-client-secret --value '<secret>'
az keyvault secret set --vault-name $VAULT --name anthropic-api-key   --value '<key>'
az keyvault secret set --vault-name $VAULT --name action-signing-key \
  --value "$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
# optional
az keyvault secret set --vault-name $VAULT --name jira-api-token --value '<token>'
```

`action-signing-key` is what makes the buttons in your email trustworthy. It
must be at least 32 characters, and rotating it invalidates every button in
every recap already sitting in your mailbox.

### 5. Configure the app

```bash
HOST=$(az functionapp show -g $RG -n $APP --query properties.defaultHostName -o tsv)
KV="@Microsoft.KeyVault(VaultName=$VAULT;SecretName="

az functionapp config appsettings set -g $RG -n $APP --settings \
  "ASSISTANT_EMAIL=you@example.org" \
  "ASSISTANT_USER_NAME=Alex" \
  "ASSISTANT_TIMEZONE=America/New_York" \
  "WEBSITE_TIME_ZONE=Eastern Standard Time" \
  "ACTION_BASE_URL=https://$HOST/api" \
  "GRAPH_TENANT_ID=<tenant-id>" \
  "GRAPH_CLIENT_ID=<client-id>" \
  "GRAPH_CLIENT_SECRET=${KV}graph-client-secret)" \
  "ANTHROPIC_API_KEY=${KV}anthropic-api-key)" \
  "ACTION_SIGNING_KEY=${KV}action-signing-key)"
```

> **A Key Vault reference is resolved once and cached.** Restarting the app does
> *not* pick up a new secret value. To rotate one, set the new value and then
> point the app setting at the explicit new version — two steps, always.

`WEBSITE_TIME_ZONE` is what makes "6 AM" mean 6 AM where you live. Without it
every schedule in this app runs in UTC.

### 6. Deploy

```bash
zip -r ../claire-voyant.zip . -x '*/__pycache__/*' '.venv/*' '.local/*' '.git/*'
az functionapp deployment source config-zip -g $RG -n $APP \
  --src ../claire-voyant.zip --build-remote true
az functionapp function list -g $RG -n $APP --query "[].name" -o tsv
```

You should see fifteen functions. Then check an endpoint is alive — a `400` is
the right answer here, because the token is deliberately junk:

```bash
curl -si "https://$HOST/api/draft?t=x" | head -1
```

---

## Configuration reference

### Required

| Setting | What it is |
|---|---|
| `ASSISTANT_EMAIL` | The mailbox it works in, and the only address it emails |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` | Your app registration |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `ACTION_SIGNING_KEY` | 32+ random characters. Without it the buttons are omitted |

### Worth setting

| Setting | Default | What it does |
|---|---|---|
| `ASSISTANT_USER_NAME` | `Alex` | The name greetings use |
| `ASSISTANT_TIMEZONE` | `America/New_York` | IANA zone for agendas and availability |
| `WEBSITE_TIME_ZONE` | — | Windows zone name; what the *schedules* run on |
| `ACTION_BASE_URL` | from `WEBSITE_HOSTNAME` | Where the buttons point |
| `BOOKING_LINK` | empty | Your scheduling page, appended to meeting replies |
| `ASSISTANT_PERSONA_NAME` | `Claire Voyant` | Rename the assistant |
| `AVAILABILITY_DAYS` | `10` | How far ahead meeting replies offer times |
| `GRAPH_CALENDAR_WRITE` | `false` | Enables block-time and add-to-calendar. Consent first |
| `CREATE_OUTLOOK_DRAFTS` | `false` | Also mirror drafts into the Drafts folder |
| `OWNER_ADDRESSES` | empty | Comma-separated aliases that are also you |
| `EXTRA_HOLIDAYS`, `IGNORED_HOLIDAYS` | — | `YYYY-MM-DD` lists. Note the agenda does *not* skip holidays — meetings happen on them |

### Jira (optional)

| Setting | What it does |
|---|---|
| `JIRA_BASE_URL` | e.g. `https://your-site.atlassian.net` |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira Cloud credentials |
| `JIRA_PROJECT_KEY` | Where new tasks are created |
| `JIRA_ASSIGNEES` | JSON of `{"first name": "account-id"}` — needed to assign anything |
| `JIRA_DEFAULT_ASSIGNEE` | Which of those gets unattributed new work |
| `TEAM_ROSTER` | JSON list of `{"name", "jql"}`. Empty means no team digests |

Find an account id at `/rest/api/3/user/search?query=<email>` on your own site.

```
TEAM_ROSTER=[{"name":"Sam","jql":"project = 'ST'"},
             {"name":"Priya","jql":"project = 'OPS' AND assignee = '712020:...'"}]
```

Filter on `statusCategory`, not status names — those differ per project.

### Models

`TRIAGE_MODEL` (default Haiku, one call per batch of eight messages) and
`DRAFT_MODEL`, `PLANNING_MODEL`, `EVENT_MODEL`, `JUNK_MODEL`, `REPLY_MODEL`
(default Sonnet). Classification is constant and cheap; drafting is rare and
worth the better model.

---

## Running locally

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp local.settings.json.example local.settings.json    # then fill it in
func start
```

With no `AzureWebJobsStorage`, every state store falls back to JSON files under
`.local/`, which is gitignored. Timer triggers do not fire on start, so call the
underlying functions directly:

```python
import datetime, function_app as fa
fa.run_triage("manual")
fa.run_agenda(datetime.date.today(), is_today=True)
fa.run_event_scan()
fa.run_junk_review()
```

### Tests

Each test file is a standalone script; no pytest required.

```bash
for f in tests/test_*.py; do python "$f" || exit 1; done
```

447 tests, no network calls, no credentials needed.

---

## How it is put together

```
function_app.py           every trigger and HTTP endpoint
shared/
  graph_client.py         Microsoft Graph: mail, calendar, contacts
  jira_client.py          Jira Cloud REST v3
  triage.py               classification and reply drafting
  recap.py                the inbox recap email
  agendamail.py           the daily agenda email
  events.py / eventmail.py    events found in mail
  junkreview.py / junkmail.py the Friday junk sweep
  taskmail.py             task and team digests
  taskreply.py            plain-English replies applied to Jira
  planning.py             day plans and team summaries
  actions.py              signed, expiring, single-use button tokens
  pages.py                the confirmation pages behind the buttons
  availability.py         free/busy maths
  contacts.py             contacts built from who you reply to
  persona.py              who the assistant says it is
  pending.py / state.py / digests.py / token_store.py    state
scripts/                  one-off setup and backfill tools
```

Every state store has a file backend for local runs and an Azure Table backend
in production, chosen automatically from `AzureWebJobsStorage`.

### Security notes

- Button tokens are HMAC-SHA256 signed, expire after seven days, and are
  single-use — enforced at the state store, since a token cannot know it has
  been spent.
- Anyone who can read the mailbox can click the buttons. That is the same trust
  boundary as the mailbox itself. A leaked *signing key* is a different matter,
  which is why it lives in Key Vault and never as a literal app setting.
- All prompts frame email and Jira content as untrusted data.
- Jira credentials are proved before any result is trusted: a dead token returns
  `200` with an empty list from `/search/jql` while `/myself` returns `401`, so
  "nobody finished anything this week" would otherwise be reported as fact.

---

## Licence

MIT — see [LICENSE](LICENSE).

Published as a reference implementation. It ships with no roster, no account
ids, no booking link and no credentials; you supply all of them.
