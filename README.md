# MTProto Proxy Hub

Community-driven MTProto proxy aggregator with real-time ping monitoring, voting system, and automatic cleanup.

## Features

- **Proxy Aggregation**: Add and manage MTProto proxies from multiple sources
- **Real-time Ping Monitoring**: Uses MTProto Proxy-get protocol for accurate availability checks
- **Voting System**: Like/dislike proxies to help community find the best ones
- **Automatic Cleanup**: 
  - Removes most disliked proxies (5+ dislikes) every 30 minutes
  - Removes proxies that have been down for 5+ days
- **QR Code Support**: Generate QR codes for quick Telegram proxy connection
- **Bulk Import**: Parse and add multiple proxies from text
- **Responsive Design**: Material Design 3 UI with dark/light theme support

## How It Works

### Ping Checking

The system uses **MTProto Proxy-get** protocol (not simple TCP connect) to accurately check proxy availability:

1. Sends a valid MTProto handshake request with magic bytes `0x00010001`
2. Includes secret as padding (or handles domain fronting with `ee` prefix)
3. Waits for proper MTProto response
4. Measures round-trip time for ping value

This ensures proxies actually work with Telegram, not just accept TCP connections.

### Background Workers

#### Ping Worker
- Runs every 5 minutes
- Checks all proxies using MTProto Proxy-get
- Updates ping status (OK/WARNING/FAILED)
- Skips recently failed proxies (2 hour cooldown) to reduce load

#### Cleanup Worker
- Runs every 30 minutes
- Deletes the most disliked proxy if it has 5+ dislikes
- Deletes all proxies that have been in FAILED status for 5+ days
- Updates last_cleanup timestamp in stats

## Installation

### Requirements

- Python 3.11+
- SQLite (included via aiosqlite)

### Setup

```bash
uv sync
```

### Running

```bash
python main.py
```

The application will be available at `http://localhost:8000`

## API Endpoints

### GET `/`
Main HTML page with proxy list

### GET `/api/proxies`
Get list of proxies
- Query params: `sort` (likes/ping/newest), `limit`, `offset`

### POST `/api/proxies`
Add a single proxy
- Body: `{ "server": "...", "port": ..., "secret": "..." }`

### POST `/api/proxies/parse`
Parse proxy links from text
- Body: `{ "text": "tg://proxy?..." }`

### POST `/api/proxies/bulk`
Parse and add multiple proxies
- Body: `{ "text": "multiple links..." }`

### POST `/api/vote`
Vote on a proxy
- Body: `{ "proxy_id": ..., "vote_type": "like"|"dislike" }`

### GET `/api/vote/{proxy_id}`
Get current user's vote for a proxy

### GET `/api/stats`
Get aggregate statistics

### POST `/api/ping/{proxy_id}`
Manually trigger ping check for a proxy

### POST `/api/add-proxy`
Add proxy via API (supports both single and bulk)
- Body: `{ "server": "...", "port": ..., "secret": "..." }` or `{ "links": "..." }`

## Database Schema

### proxies
- `id`: Primary key
- `server`: Hostname or IP
- `port`: Port number
- `secret`: Hex secret (32-512 chars)
- `likes`: Vote count
- `dislikes`: Vote count
- `ping_ms`: Last ping in milliseconds
- `ping_status`: OK/WARNING/FAILED/PENDING
- `tcp_ok`: TCP connection success
- `dns_ok`: DNS resolution success
- `created_at`: Creation timestamp
- `last_checked`: Last ping check timestamp

### votes
- `id`: Primary key
- `proxy_id`: Foreign key
- `voter_id`: Cookie-based voter ID
- `vote_type`: like/dislike
- `created_at`: Vote timestamp

### stats
- Single row tracking last cleanup time

## Proxy Link Formats

Supported formats for parsing:
- `tg://proxy?server=...&port=...&secret=...`
- `https://t.me/proxy?server=...&port=...&secret=...`

## Configuration

Configuration is loaded from `config.toml`.

Example `config.toml`:

```toml
[app]
debug = false

[logging]
level = "INFO"
file = "logs/proxyhub.log"
rotation = "10 MB"
retention = "7 days"

[telegram]
enabled = false
api_id = 123456
api_hash = "your_api_hash"
session_name = "proxyhub"
channels = ["telemtrs", "@your_channel_id"]
```

### Telegram proxy ingestion

When `telegram.enabled` is `true`, the app will start a Telethon client at startup and listen for new messages in configured Telegram channels.

- Only authorized channels listed in `config.toml` are processed.
- New message text is parsed for `tg://proxy` and `https://t.me/proxy` links.
- Valid proxies are added automatically to the database.

### Notes

- `api_id` and `api_hash` are required when Telegram ingestion is enabled.
- Session files are stored under `.session/`.
- If Telegram is disabled, the app still works with manual and bulk proxy import.
