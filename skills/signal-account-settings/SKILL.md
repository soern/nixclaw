# Signal Account Settings

Manage the agent's own Signal account settings.

## When to Use

**USE this skill when:**
- User wants to change the agent's Signal profile name or status
- User wants to view or modify account-specific settings
- User wants to check identity trust status
- User wants to perform administrative maintenance on the Signal account
- A user complains they are not recognized as privileged/known despite being in `config/agent.yaml` — Signal may be delivering only their UUID, not their phone number (see "Resolve an Unknown User" below)

**DON'T use this skill when:**
- User wants to send messages to contacts (use normal messaging)
- User wants to create groups or polls for communication
- This is for day-to-day communication needs

## Important Restrictions

- **Administrative use only** - This skill is for making changes to the agent's own Signal account configuration, not for regular communication
- **Privileged users only** - Only users who can execute bash commands can use this skill, as the Signal API calls require direct HTTP requests

## How to Use

1. **Read the configuration** to get the agent's phone number and API URL:
   - Configuration file: `/app/config/agent.yaml`
   - Relevant settings:
     - `signal.phone_number` - The agent's Signal phone number
     - `signal.api_url` - The Signal API base URL (default: `http://localhost:8080`)

2. **Reference the API documentation** at `/app/skills/signal-account-settings/SIGNAL_API.md` for:
   - Available endpoints (Profiles, Identities, Reactions, Search, Contacts, Polls, Configuration)
   - Request/response formats
   - curl command examples

3. **Execute the appropriate curl command** using the agent's phone number from the config

## Example: Change Profile Name

1. Read `/app/config/agent.yaml` to get `signal.phone_number`
2. Use the profile update endpoint:
   ```bash
   curl -X PUT "http://localhost:8080/v1/profiles/{PHONE_NUMBER}" \
     -H "Content-Type: application/json" \
     -d '{"name": "New Display Name", "about": "Status text"}'
   ```

## Resolve an Unknown User (phone number hidden by Signal privacy)

When a user in `auth.privileged_numbers` or `auth.known_numbers` reports that the agent does not recognize them (treats them as UNKNOWN), the most likely cause is that Signal's privacy protocol hid their phone number from us. Modern Signal defaults to **Settings → Privacy → "Who can see my phone number" → "Nobody"**, which means envelopes arrive with only a UUID in the `source` field and no `sourceNumber`. The webhook handler in `src/nixclaw_agent/signal.py` falls back to the UUID as the channel id, so the auth check finds no match.

Note: there is **no recipient-side approved-contacts feature in Signal** — that hypothesis is a red herring. The fix is on the sender's Signal app, or to force Nixclaw's signal-cli to do a Contact Discovery Service (CDS) lookup.

### Step 1: Confirm the user has messaged Nixclaw

```bash
curl -s "http://localhost:8080/v1/contacts/{PHONE_NUMBER}?all_recipients=true" | \
  python3 -c "import json,sys; [print(f'  number={c[\"number\"] or \"<UUID-only>\"}, uuid={c[\"uuid\"]}, name={c[\"profile\"].get(\"given_name\",\"\")}') for c in json.load(sys.stdin)]"
```

Look for an entry whose `number` field is **empty** but whose profile (e.g. given_name, about) plausibly matches the user. That entry has messaged Nixclaw, but signal-cli does not yet know the phone number.

### Step 2: Trigger a contact sync

```bash
curl -X POST "http://localhost:8080/v1/contacts/{PHONE_NUMBER}/sync"
```

This asks signal-cli to perform a CDS lookup and populate phone numbers for known UUIDs. No request body is needed.

### Step 3: Verify the contact now has the phone number

Re-run the command from Step 1. The previously empty `number` field should now contain the phone number, e.g. `"number": "+4900012345678"`.

### Step 4: Confirm a fresh inbound message is recognized

Ask the user to send a new message, then check the agent log:

```bash
docker logs nixclaw --since 1m 2>&1 | grep "TIER_LOOKUP"
```

You should see `event.source=+4900012345678, tier=UserTier.PRIVILEGED` (or KNOWN, matching the config).

### Step 5: Advise the user on a durable fix

For long-term reliability (so the user keeps working even if Nixclaw's contact cache is ever wiped/relinked), tell the user to change **their own** Signal privacy setting:

**Signal → Settings → Privacy → Who can see my phone number → "Everybody"** (or "People you choose" and add Nixclaw's number).

Without this, the user is dependent on Nixclaw having resolved their UUID at least once.

### Step 6 (optional): Verify the number is registered on Signal

If you suspect the user typed the wrong number or their account is deregistered:

```bash
curl -s "http://localhost:8080/v1/search/{PHONE_NUMBER}?numbers=+4900012345678"
# Returns: [{"number":"+4900012345678","registered":true}]
```

A `registered: false` result means the number is not on Signal and the user must fix their setup before any sync will help.

See `/app/skills/signal-account-settings/SIGNAL_API.md` for complete API reference.
