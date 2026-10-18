# Signal Account Settings API Reference

Base URL: `http://localhost:8080`
Content-Type: `application/json`

**Note:** This document includes only commonly used commands. For the full command set, see: https://bbernhard.github.io/signal-cli-rest-api/src/docs/swagger.json

---

## Configuration

### Get REST API Configuration

```
GET /v1/configuration
```

**Response:**
```json
{
  "logging": {
    "Level": "INFO"
  }
}
```

---

### Set REST API Configuration

```
POST /v1/configuration
```

**Request:**
```json
{
  "logging": {
    "Level": "DEBUG"
  }
}
```

---

### Get Account-Specific Settings

```
GET /v1/configuration/{number}/settings
```

**Parameters:**
- `number` - Registered phone number

**Response:** Account-specific settings

---

### Set Account-Specific Settings

```
POST /v1/configuration/{number}/settings
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "trust_mode": "UNTRUSTED"
}
```

---

## Profiles

### Update Profile

```
PUT /v1/profiles/{number}
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "name": "New Display Name",
  "about": "Status text",
  "base64_avatar": "..."
}
```

**Note:** `base64_avatar` must be base64-encoded image data.

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

## Identities

### List Identities

```
GET /v1/identities/{number}
```

**Parameters:**
- `number` - Registered phone number

**Response:**
```json
[
  {
    "added": "2024-01-01T00:00:00Z",
    "fingerprint": "...",
    "number": "+491234567890",
    "safety_number": "12345...",
    "status": "TRUSTED",
    "uuid": "..."
  }
]
```

---

### Trust Identity

```
PUT /v1/identities/{number}/trust/{numberToTrust}
```

**Parameters:**
- `number` - Registered phone number
- `numberToTrust` - Number whose identity to trust

**Request:**
```json
{
  "trust_all_known_keys": true,
  "verified_safety_number": "12345..."
}
```

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

## Contacts

### List Contacts

```
GET /v1/contacts/{number}
```

**Parameters:**
- `number` - Registered phone number
- `all_recipients` - (optional) Include all known recipients

**Response:**
```json
[
  {
    "blocked": false,
    "color": "#ffffff",
    "given_name": "John",
    "message_expiration": 0,
    "name": "John Doe",
    "nickname": null,
    "note": "",
    "number": "+491234567890",
    "profile": {
      "about": "Online",
      "given_name": "John",
      "has_avatar": true,
      "last_updated_timestamp": 1234567890,
      "lastname": "Doe"
    },
    "profile_name": "John",
    "username": "johndoe",
    "uuid": "..."
  }
]
```

---

### Update Contact

```
PUT /v1/contacts/{number}
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "expiration_in_seconds": 0,
  "name": "John Doe",
  "recipient": "+491234567890"
}
```

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

### Sync Contacts

```
POST /v1/contacts/{number}/sync
```

**Parameters:**
- `number` - Registered phone number

**Description:** Send a synchronization message with the local contacts list to all linked devices. Only use if this is the primary device.

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

### Get Contact Avatar

```
GET /v1/contacts/{number}/{uuid}/avatar
```

**Parameters:**
- `number` - Registered phone number

**Response:** Binary image data

---

## Reactions

### Send Reaction

```
POST /v1/reactions/{number}
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "reaction": "👍",
  "recipient": "+491234567890",
  "target_author": "+0987654321",
  "timestamp": 1769271479000
}
```

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

### Remove Reaction

```
DELETE /v1/reactions/{number}
```

**Parameters:**
- `number` - Registered phone number

**Request:** Same as Send Reaction

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

## Polls

### Create Poll

```
POST /v1/polls/{number}
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "allow_multiple_selections": false,
  "answers": ["Option 1", "Option 2", "Option 3"],
  "question": "What do you prefer?",
  "recipient": "+491234567890"
}
```

**Responses:**
- `201 Created` - Success
- `400 Bad Request` - Error

---

### Close Poll

```
DELETE /v1/polls/{number}
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "poll_timestamp": "1769271479",
  "recipient": "+491234567890"
}
```

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

### Vote on Poll

```
POST /v1/polls/{number}/vote
```

**Parameters:**
- `number` - Registered phone number

**Request:**
```json
{
  "poll_author": "+491234567890",
  "poll_timestamp": "1769271479",
  "recipient": "+491234567890",
  "selected_answers": [0]
}
```

**Responses:**
- `204 No Content` - Success
- `400 Bad Request` - Error

---

## Search

### Check Phone Number Registration

```
GET /v1/search/{number}
```

**Parameters:**
- `number` - Registered phone number
- `numbers` - Query parameter with comma-separated numbers to check

**Response:**
```json
[
  {
    "number": "+491234567890",
    "registered": true
  }
]
```

---

## Example curl Commands

### Update Profile Name

```bash
curl -X PUT "http://localhost:8080/v1/profiles/{PHONE_NUMBER}" \
  -H "Content-Type: application/json" \
  -d '{"name": "New Name", "about": "Online"}'
```

### List Identities

```bash
curl -X GET "http://localhost:8080/v1/identities/{PHONE_NUMBER}"
```

### Trust Identity

```bash
curl -X PUT "http://localhost:8080/v1/identities/{PHONE_NUMBER}/trust/{TARGET_NUMBER}" \
  -H "Content-Type: application/json" \
  -d '{"trust_all_known_keys": true}'
```

### Send Reaction

```bash
curl -X POST "http://localhost:8080/v1/reactions/{PHONE_NUMBER}" \
  -H "Content-Type: application/json" \
  -d '{"reaction": "👍", "recipient": "+491234567890", "target_author": "+0987654321", "timestamp": 1769271479000}'
```

### Check Number Registration

```bash
curl -X GET "http://localhost:8080/v1/search/{PHONE_NUMBER}?numbers=+491234567890"
```

### Create Poll

```bash
curl -X POST "http://localhost:8080/v1/polls/{PHONE_NUMBER}" \
  -H "Content-Type: application/json" \
  -d '{"allow_multiple_selections": false, "answers": ["A", "B"], "question": "Vote?", "recipient": "+491234567890"}'
```