# ElevenLabs Agent Web UI

A local voice web UI for an ElevenLabs Conversational AI agent. Push-to-talk orb,
live transcript, text input, mute and volume — served by a single Python file with
**no dependencies to install** (standard library only, no pip, no npm).

## Where the API key goes

Open **[.env](.env)** in this folder and fill in the first line:

```ini
ELEVENLABS_API_KEY=sk_your_key_here
ELEVENLABS_AGENT_ID=agent_your_agent_id_here
```

- Get the key at <https://elevenlabs.io/app/settings/api-keys>
- Get the agent ID at <https://elevenlabs.io/app/agents> (or leave it blank and pick
  an agent from the dropdown in the UI)
- **Restart the server** after editing `.env`.

The key is read by `server.py` only. It is never written into the HTML, never sent
to the browser, and `.env` is git-ignored. The browser only ever receives a
short-lived conversation token minted per call.

> Deploying this on a server? See **[DEPLOY.md](DEPLOY.md)**. Two things there
> are not optional: the microphone needs HTTPS, and `/api/conversation-token`
> spends ElevenLabs credits and has no authentication of its own.

## Sign-in is required

The page and every `/api/*` route need a session. A built-in account works out
of the box, so `python server.py` is all you need to get going:

| | |
| --- | --- |
| User | `testuser` |
| Password | `ustuser!` |

> **This is a demo credential, not a secret.** Its hash sits in
> [auth.py](auth.py) in this repository, and the password is short enough that
> recovering it from the hash is trivial. The real posture is *anyone who can
> read this repo can sign in.* Replace it before the app is reachable by anyone
> you would not hand the password to:
>
> ```sh
> python auth.py --add-user rep    # -> APP_USERS=... in .env
> ```
>
> Setting `APP_USERS` replaces the built-in account entirely. The startup
> banner says loudly which one is active.

Also worth setting, though neither is required:

```sh
python auth.py --secret          # -> APP_SECRET=...  or sessions die on restart
```

> **If an edit to `.env` seems to have no effect,** a real environment variable
> is shadowing it — that precedence is deliberate, so a systemd unit or CI can
> override without editing the file. `python common.py --check` prints which
> source is winning for every key.

Cookies are `Secure`, which means a browser withholds them over plain HTTP.
The server relaxes that automatically when bound to loopback — so
`http://127.0.0.1:8080` just works — and keeps it on everywhere else.

What this buys you:

| | |
| --- | --- |
| Passwords | scrypt hashes, ~65 ms to verify. Plaintext is never accepted |
| Sessions | HMAC-signed cookie, `HttpOnly` + `Secure` + `SameSite=Strict` |
| Conversation cap | `APP_TOKENS_PER_HOUR` per user — each conversation costs credits |
| Sign-in cap | `APP_LOGIN_ATTEMPTS` failures per source address per 15 min |
| Audit log | `login_ok`, `login_failed`, `mint`, `token_throttled`, `logout` to stderr |
| Headers | CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`, `Permissions-Policy` |

`/api/config` deliberately returns no fragment of the API key. Rotating
`APP_SECRET` invalidates every session immediately.

## Run

```sh
python server.py
```

Then open <http://127.0.0.1:8080> (it opens automatically). On Windows you can
also just double-click `start.bat`.

Options:

```sh
python server.py --port 9000     # different port
python server.py --no-browser    # don't auto-open a browser
```

## How it works

```
Browser                     server.py                  ElevenLabs
   |  GET /api/conversation-token  |                        |
   |------------------------------>|  xi-api-key header     |
   |                               |----------------------->|
   |                               |<--- { token } ---------|
   |<--- { token } ----------------|                        |
   |                                                        |
   |=========== WebRTC audio session (SDK) ================>|
```

`public/app.js` loads `@elevenlabs/client` from jsDelivr and calls
`Conversation.startSession({ conversationToken, ... })`. Because the token is
minted server-side, this works with **private** agents — you do not need to make
your agent public.

## Custom LLM (vLLM / any OpenAI-compatible server)

The agent can run on your own model instead of an ElevenLabs-hosted one. Settings
live in the same [.env](.env):

```ini
CUSTOM_LLM_URL=https://your-llm-host.example.com/v1
CUSTOM_LLM_MODEL=gemma4-26b
CUSTOM_LLM_API_KEY=sk-vllm-...
CUSTOM_LLM_SECRET_NAME=CUSTOM_LLM_API_KEY
```

Then use [configure_llm.py](configure_llm.py):

```sh
python configure_llm.py                        # show what the agent uses now
python configure_llm.py --test                 # probe the LLM, change nothing
python configure_llm.py --apply                # switch the agent over
python configure_llm.py --revert gpt-4o-mini   # back to a built-in model
python configure_llm.py --apply --update-secret  # also rotate the stored key
```

`--apply` stores `CUSTOM_LLM_API_KEY` as an ElevenLabs **workspace secret**
(named by `CUSTOM_LLM_SECRET_NAME`) and points the agent's `custom_llm` at your
URL. It re-reads the agent afterwards and fails loudly if the change didn't stick.

### Cold warehouse pre-warm

A cold serverless SQL warehouse takes 10-15s to answer its first query — long
enough to blow the tool timeout and have the agent tell the caller *"I
encountered an error"*. Set `DATABRICKS_WARM_TOKEN` and the server submits
`SELECT 1` when a user signs in and when a conversation starts, so the
warehouse boots while the agent is still greeting them.

It is fire-and-forget (`wait_timeout=0s`, returns `PENDING` in ~1s on a
background thread, so nothing delays the caller) and debounced to at most one
query per `DATABRICKS_WARM_MINUTES`.

`SELECT 1` reads no table, so **this credential needs only `CAN_USE` on the
warehouse and no catalog grants at all.** Use a service principal with nothing
else granted; the worst it can do is start a warehouse. It falls back to
`DATABRICKS_TOKEN`, which is convenient but puts a data-capable token on the
web host — see [DEPLOY.md](DEPLOY.md).

This shortens the cold path; it does not remove it. Raising the warehouse
auto-stop window, or serving the table from a Lakebase synced table, are the
real fixes.

### Requirements for the LLM endpoint

- **Publicly reachable.** ElevenLabs' servers call it, not your browser. A
  localhost or VPN-only URL will not work.
- **`POST {URL}/chat/completions`** — so `CUSTOM_LLM_URL` includes the `/v1`.
- **Streams Server-Sent Events** with `Content-Type: text/event-stream`.

`--test` verifies all three against the live endpoint.

> **CDN / WAF note:** if the LLM host sits behind Cloudflare, it may return
> **403** to requests with a `Python-urllib` user agent. `configure_llm.py` sends a
> browser-like UA to get through. If calls fail once ElevenLabs starts using the
> agent, Cloudflare bot protection is the first thing to check — allowlist
> ElevenLabs' egress or drop the security level for `/v1/*`.

### Testing without a microphone

```sh
python chat_test.py "What do you sell?"
```

[chat_test.py](chat_test.py) drives the real ElevenLabs conversation WebSocket in
text mode, so it exercises the same LLM path a voice call does. It exits non-zero
if the LLM fails, and prints the conversation's `termination_reason`.

The agent's opening line is static and does **not** prove the LLM ran — only a
reply that comes *after* your message does. `chat_test.py` counts it that way.

### Resolved issue: two stacked failures behind "custom_llm generation failed"

Symptom: the call connects, the agent speaks its opener, then dies with
`custom_llm generation failed` / `custom_llm_error` (code 1002). The browser shows
"Server error: Unknown error". Both causes below had to be fixed before a real
conversation would work.

**1. vLLM rejected ElevenLabs' `tools` field.** Measured directly against the
vLLM server:

| Request shape | Result |
| --- | --- |
| No `tools` field | works |
| `tools: []` (empty array) | **400** — "`tools` must not be an empty array" |
| `tools: [...]`, `tool_choice` absent or `auto` | **400** — needs `--enable-auto-tool-choice` |
| `tools: [...]`, `tool_choice: "none"` | works |

Fixed by the vLLM operator restarting with tool calling enabled:
`vllm serve <model> --enable-auto-tool-choice --tool-call-parser <parser>`.

**2. Cloudflare silently blocked ElevenLabs' own server-to-server request.**
After fix #1, direct requests from this machine (with a browser-like
User-Agent) succeeded, but real calls placed by ElevenLabs still failed
identically. Cloudflare in front of the LLM host had earlier returned a 403 to
a plain `Python-urllib` User-Agent while passing everything with a browser-like
one — ElevenLabs' own outbound client apparently hit the same block, and it
surfaced only as the generic "failed to generate response" error, not a
visible 403.

Fixed with `CUSTOM_LLM_USER_AGENT` in `.env`, which `configure_llm.py --apply`
sets as a `request_headers.User-Agent` override on the agent's `custom_llm`
config — every request ElevenLabs sends now carries a browser-like UA.

Confirmed fixed with `python chat_test.py` — the agent now generates real
replies (`status: done`) instead of failing on the first user turn.

If you ever hit this again on a different LLM host: reproduce with `--test`
first; if that passes but real calls still fail, suspect a WAF/CDN treating
ElevenLabs' request differently than yours, since you cannot see ElevenLabs'
outbound request directly.

## Files

Copy [.env.example](.env.example) to `.env` and fill it in. `.env` is
git-ignored and holds live credentials; never commit it.

**The web UI**

| Path | What it is |
| --- | --- |
| [server.py](server.py) | Static file server + ElevenLabs API proxy. Stdlib only. |
| [public/index.html](public/index.html) | Page markup |
| [public/app.js](public/app.js) | Session logic, transcript, controls, event log |
| [public/styles.css](public/styles.css) | Styling |

**Configuring the agent** — each reads `.env`, writes through the ElevenLabs API,
and re-reads afterwards to confirm the change stuck.

| Script | What it sets |
| --- | --- |
| [configure_llm.py](configure_llm.py) | Point the agent at a custom OpenAI-compatible LLM |
| [configure_prompt.py](configure_prompt.py) | Upload [agent_prompt.md](agent_prompt.md) as the system prompt |
| [configure_audio.py](configure_audio.py) | Noise filtering and turn-taking |
| [configure_privacy.py](configure_privacy.py) | Retention, stored audio, zero-retention mode |
| [databricks_tool.py](databricks_tool.py) | Sync the tools defined in [databricks_tools.json](databricks_tools.json) |

**Testing** — all of these work without a microphone.

| Script | What it checks |
| --- | --- |
| [chat_test.py](chat_test.py) | One text turn end to end; exits non-zero if the LLM fails |
| [test_conversation.py](test_conversation.py) | A scripted multi-turn conversation, plus the tool calls it made |
| [test_phrasings.py](test_phrasings.py) | A batch of phrasings: which tool each routed to, and whether rows came back |
| [analyze_call.py](analyze_call.py) | Per-turn latency breakdown for a past call |
| [auth.py](auth.py) | Password hashing, sessions, rate limits — plus the CLI above |
| [common.py](common.py) | Shared `.env` loading. `python common.py --check` shows which values are actually in force |

**Docs**

| Path | What it is |
| --- | --- |
| [DEPLOY.md](DEPLOY.md) | Running this on Linux behind TLS |
| [agent_prompt.md](agent_prompt.md) | The agent's system prompt, uploaded by configure_prompt.py |

## Server endpoints

| Endpoint | Purpose |
| --- | --- |
| `GET /api/config` | Whether a key is loaded, and the default agent ID |
| `GET /api/agents` | Lists the agents on your account (fills the dropdown) |
| `GET /api/conversation-token?agent_id=…` | Mints a WebRTC token |
| `GET /api/signed-url?agent_id=…` | Mints a WebSocket signed URL |

## Notes

- The server binds to `127.0.0.1` only. Microphone access requires a secure
  context; `localhost` counts as one, so no HTTPS setup is needed locally.
- If you expose this beyond localhost, put it behind HTTPS and add authentication —
  anyone who can reach `/api/conversation-token` can spend your ElevenLabs credits.
- Transport can be switched between WebRTC and WebSocket in the settings panel
  (gear icon). WebRTC is the better default.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "ELEVENLABS_API_KEY is missing" | Fill in `.env` and restart `python server.py` |
| 401 from ElevenLabs | Key is wrong or revoked; regenerate it in the dashboard |
| "Could not list agents" | Key lacks Agents permission, or the account has none |
| No microphone prompt | Use `http://127.0.0.1:8080`, not a LAN IP |
| Port already in use | `python server.py --port 8081` |
