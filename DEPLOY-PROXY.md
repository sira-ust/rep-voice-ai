# Deploying proxy mode

In proxy mode, ElevenLabs calls `server.py` as its LLM. The server runs the
Databricks lookups and web searches itself and returns only the sentence to
speak. Do the base install in [DEPLOY.md](DEPLOY.md) first. This page covers
only what proxy mode adds.

**The server is now inside every call.** It needs an always-on host with a
permanent public HTTPS address, and a restart ends any call in progress.

## Known limitations (test phase)

- **Any signed-in user can view any rep's accounts.** They pick the rep from
  the page's list. That is fine while only testers can sign in, but the rep
  must come from the sign-in before reps get access (the `TODO` in
  `/api/scope`).
- **One rollback file for all agents.** `configure_proxy.py` saves a single
  `.proxy-previous.json`. Before switching a second agent from the same
  machine, run `--off` on the first, or delete the file.
- **The visit plan table is a stand-in snapshot.** Until the dbt model in
  [prompts/dbt_visit_plan.md](prompts/dbt_visit_plan.md) exists, it has to be
  rebuilt every day (section 3), or plans go stale.

## 1. Add to the server's `.env`

```ini
# Gemma on the same machine. The public address (testai.qskyabc.com) goes out
# through Cloudflare and back in, and from the office server those requests
# hang -- every answer went silent on Oct 16 until this was local.
LLM_UPSTREAM_URL=http://127.0.0.1:<vLLM port>/v1
LLM_UPSTREAM_API_KEY=<vLLM key>
LLM_PROXY_SECRET=<python -c "import secrets; print(secrets.token_urlsafe(32))">
# Find the store, read its plan, check an idea, answer: four rounds.
LLM_PROXY_MAX_TOOL_ROUNDS=4

DATABRICKS_HOST=https://adb-xxxx.N.azuredatabricks.net
DATABRICKS_TOKEN=<service principal -- see below>
DATABRICKS_WAREHOUSE_ID=<warehouse id>
DATABRICKS_WARM_TOKEN=<service principal: CAN_USE on the warehouse only>

# Web search for news and what the data cannot know. Leave blank to turn it off.
TAVILY_API_KEY=<tavily.com key>
# Road routes for trip maps. Leave blank for straight lines.
ORS_API_KEY=<openrouteservice key>

# Optional: only to point at other tables.
# VISIT_PLAN_TABLE=ust_databricks.ust_reporting.mart_account_visit_plan_dev
# HOLIDAYS_TABLE=ust_databricks.ust_reporting.ref_community_holidays
# FEEDBACK_TABLE=ust_databricks.ust_reporting.voice_agent_feedback
```

Only if Gemma is reached through Cloudflare rather than locally, also set
`LLM_UPSTREAM_USER_AGENT` to a browser user agent; Cloudflare refuses the
default one.

**The Databricks service principal needs:**
- read on the `mart_`/`fct_`/`int_`/`dim_` tables, the visit plan table and the
  `ref_` holiday tables;
- create and insert on `ust_reporting.voice_agent_feedback`. The server
  creates that table on the first rating; if it cannot write there, ratings go
  to `feedback.jsonl` next to `server.py`, which holds customer data and is
  kept out of git.

On startup, the banner should say `LLM proxy: ready -> ...` with the tool count.

## 2. Outbound access from the server

The office server's outbound internet has been unreliable. From the server
itself (not from a PC on the same network), check each address answers:

```sh
curl -s -m 10 -o /dev/null -w "%{http_code} %{time_total}s\n" https://api.tavily.com
curl -s -m 10 -o /dev/null -w "%{http_code} %{time_total}s\n" https://api.openrouteservice.org
curl -s -m 10 -o /dev/null -w "%{http_code} %{time_total}s\n" https://<databricks host>
```

A timeout means the firewall needs to allow that host. Neither web search nor
road routes can hang a call -- each gives up after a few seconds and the
answer goes on without it -- but they do not work until the host is allowed.

## 3. The visit plan's data

Load the holiday reference lists, then build the visit plan table:

```sh
python sql/load_holidays.py                                # calendar, holiday products, store communities, local dates
python sql/apply.py sql/mart_account_item_pitch.sql        # the pitch view the plan builds on
python sql/apply.py sql/mart_account_visit_plan_dev.sql    # about 1-2 minutes
```

**Rebuild the visit plan table every night** until the dbt model replaces it:
schedule the last command after the nightly data refresh. Reload the holiday
lists whenever their CSVs in `sql/` are edited.

## 4. Reverse proxy

- **Don't buffer `/llm/v1/` or `/api/live`.** Both stream. In nginx, add a
  `location` for each with `proxy_buffering off;` and
  `proxy_http_version 1.1;`, plus `proxy_read_timeout 1h;` for `/api/live`.
  Caddy streams them without changes.
- **Let ElevenLabs reach `/llm/v1/`.** It is called by ElevenLabs' servers,
  not a browser, so exempt it from bot protection, WAF rules and
  user-agent filters.

## 5. Run one instance

Calls are tracked in memory. Don't load-balance across several processes.

## 6. Switch the agent

From a workstation with the full `.env`:

```sh
APP_ENV=test python configure_proxy.py --on https://<host>          # test agent
APP_ENV=test python configure_prompt.py --apply                     # its prompt
python configure_proxy.py --on https://<host> --force               # production, after testing
python configure_prompt.py --apply --force                          # production prompt
```

On each agent, also set the voice's **text normalisation** to **ElevenLabs**
(`tts.text_normalisation_type = "elevenlabs"`), so it reads `$4,210`
correctly.

## 7. Check on the test agent

- Ask for tomorrow's visits. You should hear "Let me check.", then only
  your own accounts.
- Ask "Seeing <store> next week, what should I pitch them?" You should hear a
  short summary by product family, and see a visit plan card: "For this
  visit" first, "More ideas" folded below.
- Ask "Why haven't they ordered in so long?" The reasons should come from
  the plan (stockouts when due, price rises), with no new card.
- Ask "Anything in the news that affects what they buy?" It should name a
  source. Silence about the web means api.tavily.com is not reachable.
- Rate an answer with the thumbs, and say "Feedback: that was useful". Both
  should appear in `ust_reporting.voice_agent_feedback`.
- Name another rep's account. It should be refused.
- Play the agent through a speaker. It should not answer its own voice.
- Keep one call going past 5 minutes.

## Roll back

```sh
python configure_proxy.py --off --force
python databricks_tool.py --sync --force
```
