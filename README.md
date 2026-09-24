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

The page and every `/api/*` route need a session, and there are no accounts
until you create one. That is on purpose: an account shipped in the repository
would put its own password hash in everyone's hands.

```sh
python auth.py --secret          # -> APP_SECRET=...   signs session cookies
python auth.py --add-user rep    # prompts, -> APP_USERS=...
```

Put both lines in `.env`. `--add-user` prints the whole `APP_USERS` line
including any accounts already there, so adding a second person is the same
command again. Only the salted hash is stored — a forgotten password is reset by
generating a new entry, not recovered.

Without `APP_SECRET` the app still runs, but sessions die on every restart. The
startup banner warns about both.

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
| [configure_prompt.py](configure_prompt.py) | Upload [agent_prompt.md](agent_prompt.md) and [agent_greeting.txt](agent_greeting.txt) |
| [configure_audio.py](configure_audio.py) | Noise filtering and turn-taking — see *Background speakers* below |
| [configure_privacy.py](configure_privacy.py) | Retention, stored audio, zero-retention mode |
| [databricks_tool.py](databricks_tool.py) | Sync the tools defined in [databricks_tools.json](databricks_tools.json) |

### Background speakers

The browser side is already as good as it gets: the ElevenLabs SDK requests
`voiceIsolation`, `echoCancellation`, `noiseSuppression` and `autoGainControl`
on the microphone, with no way to ask for more. `voiceIsolation` is the one
that suppresses other voices in the room, and it is Chrome and Edge only —
Firefox and Safari ignore it silently, so the same room will be noticeably
more sensitive there.

That leaves two settings on the agent, both deliberate:

| Setting | Value | Why |
| --- | --- | --- |
| `background_voice_detection` | `on` | ElevenLabs defaults this off |
| `turn_eagerness` | `normal` | `eager` fired on fragments of other people's speech |
| `turn_timeout` | `20s` | at 12s the agent's own slow generation tripped it |

`eager` was set originally to shave latency, and it does — but it commits to a
turn on the slightest speech-shaped sound, which in a shared room means
answering someone who was not talking to it. `normal` waits for a natural
break, costing a few hundred milliseconds a turn.

`turn_timeout` is how long it waits through silence before speaking again. At
12s it was firing during the agent's *own* pause: a call showed a tool result
arriving at 67s and the reply not starting until 82s, so the timeout went off
first and it asked the caller why they had gone quiet — then lost the thread of
what it had been doing. 20s clears the gap. The real cause is how long the
custom LLM takes to generate after a tool result, and this only stops it
becoming a loop.

```sh
python configure_audio.py --eagerness normal   # current
python configure_audio.py --eagerness eager    # revert if latency matters more
python configure_audio.py --noisy              # goes further: decisive turn ends
```

These live on ElevenLabs, not in a file here, so run the command again after
rebuilding an agent from scratch.

**None of this separates speakers.** There is no diarization on the input side
— it is one audio stream, and the filtering decides *primary voice or not*, not
*who is talking*. For users on speakerphone in a shared room, a headset beats
every setting above.

### Adding a table

Four things describe what this agent can do, and they have to move together.
The web guide builds itself from [databricks_tools.json](databricks_tools.json)
so it stays honest on its own, but the tools live on ElevenLabs and the prompt
states the data's limits in prose — neither follows automatically.

1. **Add the tool** to [databricks_tools.json](databricks_tools.json), with
   `subject` and `sample_questions` so the guide has something to show.
2. **Describe the table** in the same file's `tables` block: what it holds,
   its grain, and what it cannot answer.
3. **Re-read [agent_prompt.md](agent_prompt.md)** under *What the data cannot
   tell you*, and [agent_greeting.txt](agent_greeting.txt). This is the step
   that gets missed: a new table can make a stated limit obsolete, and the
   agent will go on refusing questions it can now answer — a failure nobody
   reports, because it looks like the agent working normally.
4. **Re-read any custom guardrail** that repeats one of those limits, if
   guardrails are enabled.

```sh
python databricks_tool.py --sync     # pushes the tools, then runs --check
python databricks_tool.py --check    # drift only, changes nothing
python configure_prompt.py --apply   # after editing the prompt or greeting
```

`--check` compares the file, the agent and the guide, exits non-zero if they
disagree, and prints each table's stated limits so you can read them against
the prompt. Steps 3 and 4 are judgement, so it reminds rather than decides.

**Testing** — all of these work without a microphone.

| Script | What it checks | When to run it |
| --- | --- | --- |
| [chat_test.py](chat_test.py) | One or many turns end to end, with the tool calls each made | After any change — the everyday check |
| [test_phrasings.py](test_phrasings.py) | A batch of phrasings and which tool each reached | After editing a tool description or adding a tool |
| [convai.py](convai.py) | Library, not a CLI — the shared conversation driver | — |

```sh
python chat_test.py "How is RED005 doing?"                      # one question
python chat_test.py "Do you have coconut milk?" "What about the cream?"   # a conversation
```

Give `chat_test.py` several messages to test a **conversation** rather than a
question. Context failures only appear across turns — "what about the cream?"
behaves correctly alone and wrongly as a follow-up — so a single-turn test
cannot catch them. It prints the search term the model actually sent, which is
usually the thing you need: a wrong answer is far more often a bad search term
than a bad lookup.

None of these run on the web host. They talk to ElevenLabs directly, so run
them from a workstation.
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
