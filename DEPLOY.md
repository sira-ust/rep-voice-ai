# Deploying on Linux

The app is one Python file serving static assets and proxying a couple of
ElevenLabs calls. There is nothing to build and nothing to install — the work is
all in TLS, credentials and access control.

Read the two constraints below before anything else; they decide the whole shape
of the deployment.

## Two things that will bite you

**1. The microphone needs HTTPS.** Browsers only grant `getUserMedia` in a
secure context. `localhost` is exempt, so it works on your laptop — but the
moment the page is served from a hostname or LAN IP over plain HTTP, the mic is
silently unavailable and the agent hears nothing. **A TLS reverse proxy is not
optional.**

**2. `/api/conversation-token` spends money.** Every call starts a billable
ElevenLabs conversation. `server.py` now requires a signed-in session for the
page and every `/api/*` route, and caps conversations per user per hour — but it
refuses to serve anything at all until you configure a user, so do that before
you expect the service to come up.

## Requirements

- **Python 3.8 or newer.** Standard library only — no `pip install`, no
  virtualenv needed, no `requirements.txt`.
- **A TLS-terminating reverse proxy.** Caddy is the least work; nginx is fine.
- **Outbound HTTPS to `api.elevenlabs.io`** from the server.
- **Outbound HTTPS from each browser** to `cdn.jsdelivr.net` (the ElevenLabs
  SDK), `api.elevenlabs.io`, and `livekit.rtc.elevenlabs.io` if you use the
  WebRTC transport. On a locked-down client network, see *Air-gapped clients*.

Check the interpreter:

```sh
python3 --version
```

## Credentials: the server needs less than you think

ElevenLabs calls your LLM and Databricks **directly**, authenticated by secrets
stored in your ElevenLabs workspace. Those tokens never pass through this
server, so the deployed `.env` holds one upstream credential — the ElevenLabs
API key — plus this app's own sign-in settings:

```ini
# /opt/rep-voice-ai/.env  -- runtime
ELEVENLABS_API_KEY=sk_...
ELEVENLABS_AGENT_ID=agent_...
HOST=127.0.0.1
PORT=8080

APP_SECRET=<from auth.py --secret>
APP_USERS=rep:scrypt$...
APP_SESSION_HOURS=12
APP_TOKENS_PER_HOUR=30

# Only because a reverse proxy sets X-Forwarded-For. Without a proxy, leave
# this unset -- otherwise a caller can forge their source address and walk past
# the sign-in rate limit.
APP_TRUST_PROXY=1
```

`DATABRICKS_TOKEN`, `CUSTOM_LLM_API_KEY` and the rest are only used by the
`configure_*.py` and `databricks_tool.py` admin scripts. **Run those from a
workstation, not the server.** Keeping them off the box means a compromise of
the web host does not hand over your warehouse token.

## Install

```sh
sudo useradd --system --home /opt/rep-voice-ai --shell /usr/sbin/nologin voiceai
sudo mkdir -p /opt/rep-voice-ai
sudo git clone https://github.com/sira-ust/rep-voice-ai.git /opt/rep-voice-ai
sudo chown -R voiceai:voiceai /opt/rep-voice-ai
```

### Credentials for signing in

The server will not serve the UI with no users configured — it returns 503 with
setup instructions rather than falling back to open access. Generate both values
on the server (the hash is salted, so generate it wherever you like):

```sh
cd /opt/rep-voice-ai
sudo -u voiceai python3 auth.py --secret          # -> APP_SECRET=...
sudo -u voiceai python3 auth.py --add-user rep    # prompts, -> APP_USERS=...
```

`--add-user` prints the whole `APP_USERS` line including any existing entries,
so adding a second person is the same command again. Passwords are stored as
scrypt hashes; plaintext is never accepted.

### Runtime .env

Create it and lock it down — it holds a live API key:

```sh
sudo -u voiceai cp /opt/rep-voice-ai/.env.example /opt/rep-voice-ai/.env
sudo -u voiceai nano /opt/rep-voice-ai/.env      # fill in the two ELEVENLABS_ values
sudo chmod 600 /opt/rep-voice-ai/.env
sudo chown voiceai:voiceai /opt/rep-voice-ai/.env
```

## systemd unit

`/etc/systemd/system/rep-voice-ai.service`:

```ini
[Unit]
Description=ElevenLabs agent web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=voiceai
Group=voiceai
WorkingDirectory=/opt/rep-voice-ai
# --no-browser matters: without it the process tries to open a browser, which
# is pointless on a server. --host keeps it on loopback, behind the proxy.
ExecStart=/usr/bin/python3 /opt/rep-voice-ai/server.py --host 127.0.0.1 --port 8080 --no-browser
Restart=on-failure
RestartSec=5

# The app never writes to disk, so give it almost nothing.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/opt/rep-voice-ai
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
MemoryMax=256M

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now rep-voice-ai
systemctl status rep-voice-ai
curl -s localhost:8080/api/config      # {"agentId": "...", "hasApiKey": true, ...}
```

The startup banner reports the security posture — configured users, the
per-user conversation cap, and whether CSP is on. Check it:

```sh
journalctl -u rep-voice-ai -n 20
```

`hasApiKey: false` means the `.env` was not read — check the path and that the
`voiceai` user can read it. A 503 with "Authentication is not configured" means
`APP_USERS` is empty.

Unauthenticated requests should be refused before you go any further:

```sh
curl -si localhost:8080/ | head -2                    # 303, Location: /login
curl -s  localhost:8080/api/conversation-token         # 401 Not signed in
```

## TLS reverse proxy

### Caddy (recommended — certificates are automatic)

`/etc/caddy/Caddyfile`:

```caddyfile
voice.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

```sh
sudo systemctl reload caddy
```

The app authenticates its own users now, so proxy-level basic auth is optional.
Add it only if you want a second, independent gate — be aware it means two
password prompts.

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name voice.example.com;

    ssl_certificate     /etc/letsencrypt/live/voice.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/voice.example.com/privkey.pem;

    location / {
        proxy_pass       http://127.0.0.1:8080;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name voice.example.com;
    return 301 https://$host$request_uri;
}
```

```sh
sudo nginx -t && sudo systemctl reload nginx
```

The browser talks to ElevenLabs **directly** over its own WebSocket or WebRTC
connection using the signed URL this server hands it. Nothing agent-related is
proxied, so no `Upgrade`/`Connection` header juggling is needed.

## Firewall

Only the proxy is public. `server.py` stays on loopback.

```sh
sudo ufw allow 443/tcp
sudo ufw allow 80/tcp
sudo ufw enable
```

RHEL/Fedora with SELinux also needs the proxy allowed to make local
connections, or nginx will log permission denied on the upstream:

```sh
sudo setsebool -P httpd_can_network_connect 1
```

## Verify

From a browser at `https://voice.example.com`:

1. The header shows the agent name and a **Ready** pill.
2. Open the gear — the LLM line names your model.
3. Expand **Event log**, click **Start conversation**, allow the mic.
4. Speak. `mic-out` should climb and a `user_transcript` event should appear.

If the mic button does nothing and the console shows a `getUserMedia` error, the
page is not on HTTPS — go back and fix the proxy.

## Air-gapped clients

`public/app.js` imports the SDK from jsDelivr. If browsers cannot reach the
internet, vendor it:

```sh
curl -o /opt/rep-voice-ai/public/elevenlabs-client.js \
  https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm
```

then change the first import in `public/app.js` to `"/elevenlabs-client.js"`.
Note the bundle itself pulls `livekit-client` from a jsDelivr-relative path, so
WebRTC will still need internet — use the **WebSocket** transport in that case.

## Administration

Run these from a workstation that has the full `.env`, not from the server:

```sh
python configure_prompt.py --apply       # after editing agent_prompt.md
python databricks_tool.py --sync         # after editing databricks_tools.json
python configure_privacy.py              # check retention settings
python test_phrasings.py                 # regression across all tools
```

They change the **agent**, which lives in ElevenLabs, so they take effect
immediately for every deployment pointed at that agent. No restart needed.

## Updating

```sh
cd /opt/rep-voice-ai
sudo -u voiceai git pull
sudo systemctl restart rep-voice-ai
```

`.env` is git-ignored, so it survives. Tell users to hard-reload
(**Ctrl+Shift+R**) — `app.js` and `styles.css` are served with `Cache-Control:
no-store`, but a stale `index.html` in a browser cache has caused confusion
before.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| Mic button does nothing, `getUserMedia` error | Page is not HTTPS |
| `hasApiKey: false` at `/api/config` | `.env` unreadable by the service user |
| 502 from the proxy | `server.py` not running, or SELinux blocking the upstream |
| Agent connects, speaks its opener, then dies | The LLM leg — see the "custom_llm generation failed" section in [README.md](README.md) |
| Connects but never hears you | `mic-out` at 0 in the event log means audio is not being captured or sent |
| Tool calls time out | Cold Databricks warehouse; raise `DATABRICKS_TOOL_TIMEOUT` and re-sync |
| 503 "Authentication is not configured" | `APP_USERS` is empty — run `auth.py --add-user` |
| Sign-in succeeds then bounces back to /login | Cookies are `Secure`; you are on plain HTTP. Fix TLS, or set `APP_INSECURE_COOKIE=1` for local testing only |
| Everyone signed out after a restart | `APP_SECRET` unset, so a new one is generated each boot |
| 429 on starting a conversation | Per-user hourly cap; raise `APP_TOKENS_PER_HOUR` |

Logs, including the audit trail of sign-ins and every conversation started:

```sh
journalctl -u rep-voice-ai -f
journalctl -u rep-voice-ai | grep AUDIT
```

```
AUDIT user=rep ip=10.0.0.14 login_ok
AUDIT user=rep ip=10.0.0.14 mint /api/conversation-token agent=agent_... remaining=28
AUDIT user=rep ip=10.0.0.14 token_throttled /api/conversation-token
```

## What this deployment does not do

- **No horizontal scaling story.** `ThreadingHTTPServer` is fine for a handful of
  concurrent users minting tokens; it is not a production web server. The heavy
  lifting is all in ElevenLabs' cloud, so this rarely matters — but do not put
  it in front of hundreds of users without measuring.
- **Sessions are stateless.** There is no way to revoke one user without
  rotating `APP_SECRET`, which signs everyone out.
- **Rate limits are per process.** Fine for one instance; run two and each keeps
  its own counters, so the effective cap doubles.
- **Users share one agent and one ElevenLabs account.** Sign-ins are attributed
  in the audit log, but ElevenLabs conversations are not tagged per user.
