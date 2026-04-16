# browser-llm-coding-agent

CLI coding agent powered by browser-based LLMs. No API fees.

## What is this?

Reverse-engineered MiniMax Agent (agent.minimax.io) API client that lets you use MiniMax M2.7 from the command line, bypassing the web UI entirely.

## Quick Start

```bash
# Install dependency
pip install curl_cffi

# Set up credentials (see below)
cp config.json.example config.json
# Edit config.json with your tokens

# One-shot
python3 minimax_client.py "explain quicksort in Python"

# Interactive
python3 minimax_client.py
```

## Getting Credentials

1. Open https://agent.minimax.io in Chrome/Brave
2. Sign in with Google
3. Open DevTools → Application → Local Storage → agent.minimax.io
4. Copy `_token` value → `config.json` "token"
5. Copy `user_detail_agent` → parse JSON → `realUserID` → `config.json` "real_user_id"
6. The `device_id` is in the JWT payload (decode the token at jwt.io)

## Architecture

See `docs/` for full design documents including:
- Browser extension approach (DOM manipulation via Lemur browser)
- Direct endpoint approach (this implementation)
- API endpoint analysis and security header reverse-engineering

## How it works

- Uses `curl_cffi` to impersonate Chrome's TLS fingerprint (bypasses Cloudflare)
- Authenticates with JWT token + MD5 signatures extracted from JS bundle analysis
- Sends messages via `/matrix/api/v1/chat/send_msg`
- Polls `/matrix/api/v1/chat/get_chat_detail` for AI responses

## Status

- [x] MiniMax Agent API reverse-engineered
- [x] Authentication (JWT + x-signature + yy hash)
- [x] Cloudflare bypass (curl_cffi)
- [x] Chat send/receive PoC
- [ ] Streaming responses
- [ ] Coding agent loop (parse → execute → feedback)
- [ ] Browser extension fallback
- [ ] Multi-provider support (Claude, ChatGPT)
