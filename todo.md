# Pitch recommendations: status and to-do

Last updated 2026-10-22. Started from a sales rep's feedback on the pitch list;
grew into web search, a new pitch view in Databricks, and a model comparison.

**Nothing below is committed or deployed yet.** All changes are local, in the
working tree (`git status`).

---

## 1. Where this started

A rep saw the agent pitch Hmong Legacy Market (HMO032) this list and asked for
three things:

| Item | Reason shown | Days overdue |
|---|---|---|
| Mochiko Sweet Rice Flour | reorder due | 685 |
| Clay Mortar/Pestle 9 | reorder due | 577 |
| DF Prawn Cracker Color | reorder due | 247 |

1. **Show backorders**: "last time they ordered we were out of stock, and they
   never reordered."
2. **Explain dropped items**: some stop because another vendor is cheaper.
3. Underneath both: "685 days overdue" is meaningless. Mochiko was bought 3
   times, last in March 2022.

What the data showed for this store: the cooking pot came due while we were
out of stock 183 of 298 days, and 4 of the 6 items now cost 20-26% more than
the store last paid.

---

## 2. What has been built (local, uncommitted)

### Databricks
- **New view `ust_reporting.mart_account_item_pitch`**, created by hand, not
  dbt. It adds to `mart_account_item_opportunities` and changes nothing in it.
  - Source: [sql/mart_account_item_pitch.sql](sql/mart_account_item_pitch.sql).
  - Recreate with `python sql/apply.py`.
  - Columns added per account and item:
    - `purchase_pattern`: regular / lapsed / suggestion (definitions below).
    - `last_paid_unit_price`, `price_unit`, `price_change_pct`: only when the
      last purchase was in the same unit the list price is quoted in.
    - `stock_days_checked`, `oos_days_when_due`, `first_/last_oos_when_due`:
      days we had none to ship, from 2 weeks before the item came due to 90
      days after. Stock history starts 2025-10-27.
    - `backorder_requests`, `last_backorder_date`, `backorder_qty`: from app
      events (`add_item_backorder_page`), which start Feb 2026.
    - `suggested_order`: regular first, then lapsed items with a reason (we
      ran out, they asked, the price moved), then the rest. A hint, not the
      answer.

**Purchase pattern definitions** (decided with the user):

| Pattern | Rule |
|---|---|
| regular | bought 4+ times AND days since last purchase <= 2 × its usual gap |
| lapsed | bought before, but 3 times or fewer, OR more than 2 × its usual gap |
| suggestion | never bought (similar stores buy it, new, or on promotion) |

### Server ([server.py](server.py))
- **`web_search` tool (Tavily)**:
  - 5s deadline, one quick retry after an instant network failure, 2-min pause
    after a failure, 30-min cache.
  - Store, account-key and rep names are stripped from the query before it
    leaves the server.
  - Only offered when `TAVILY_API_KEY` is set.
- **Today's date** is added to every model call (`with_today`).
- **`read_pitch_item`** turns each pitch row into a sentence:
  - last bought, last paid vs now, "U.S. Trading was out of stock for N days
    when it came due", backorder requests, promo;
  - "possible reasons they have not reordered".
- **Season check:** a "think about timing" reminder is attached to the pitch
  list result, so the model searches for festivals and news without being
  asked (`SEASON_CHECK`, `with_season_check`).
- **Out of rounds:** when the tool loop runs out of rounds, the model gets one
  last turn with no tools and answers from what it has.
- **Tool rounds raised from 3 to 4** (`LLM_PROXY_MAX_TOOL_ROUNDS`).
- **`EVIDENCE` log line** when a later turn loses an earlier turn's lookups.

### Tool config ([databricks_tools.json](databricks_tools.json))
- `lookup_pitch_list` reads the new view, returns 10 rows ordered by
  `suggested_order`, has a new description, and shows new on-screen table
  columns.

### Prompt ([agent_prompt.md](agent_prompt.md))
- New sections:
  - **Looking things up on the web**
  - **Facts vs judgment:** the agent may explain why a store stopped buying
    and give selling advice, but never invents numbers.
- Updated pitch-list rules: say "lapsed" items differently, and say last paid
  vs now whenever they differ.
- **Every suggested product must come from a lookup.** Added after the agent
  invented "Thai Jasmine Rice".
- Pushed to the **SalesRep-test** agent only, not production.

### Other
- `.env` / `.env.example`: `TAVILY_API_KEY`, `LLM_PROXY_MAX_TOOL_ROUNDS=4`.
- [experiments/explore_tools.py](experiments/explore_tools.py): the
  flexible-tools experiment (section 5).

### Visit plan architecture (built 2026-10-22, local and test agent only)

- **Data:** `ust_reporting.mart_account_visit_plan_dev`, a hand-made stand-in
  for the dbt model in [prompts/dbt_visit_plan.md](prompts/dbt_visit_plan.md).
  - Source: [sql/mart_account_visit_plan_dev.sql](sql/mart_account_visit_plan_dev.sql);
    rebuild with `python sql/apply.py sql/mart_account_visit_plan_dev.sql`.
  - Sections: season, promo, back_in_stock, due, winback, new_trending,
    staple. No `occasion` section until the dbt seeds exist.
  - When the dbt model is live, set `VISIT_PLAN_TABLE` to it and drop the
    stand-in.
- **Holiday calendar:** [sql/community_holidays.csv](sql/community_holidays.csv)
  → `ust_reporting.ref_community_holidays` (`python sql/load_holidays.py`).
  - 2026 dates were drafted by Claude: confirm the ones flagged in the note
    column.
  - Halloween is not on it.
- **Tools** (server.py):
  - `lookup_visit_plan`: all sections, plus holidays in the next 42 days.
    The model gets one short reason per item; the full rows are cached for the
    card.
  - `check_store_product`: tests one product idea against the store (store
    lift before a date, all-store lift, stock).
  - `lookup_plan_item`: one item's full facts, for follow-up questions.
  - `lookup_pitch_list` is no longer offered to the model.
- **Output:**
  - Voice is a short summary; the model ends with a ```lead block of picks.
  - The screen gets a "Visit plan" card built by the server from the rows:
    every section, lead picks marked, out-of-stock flagged.
  - "Copy conversation" copies spoken text, screen text and cards as plain text.
- **Fact check** (done: was 4.1): every $, %, ×, quantity and date in a reply
  must be in this call's lookup data or the rep's words. Otherwise the model
  gets one correction; sentences still unsupported are removed.
  - Caught the invented "$12.50, 142 units".
  - Can't catch a true number put on the wrong item.
- **Prompt:** new "Planning a visit" section; the "sweet rice flour" example
  is removed (done: was 4.2).
- **Tested** through ElevenLabs as Ryan Tran:
  - The pitch, "why'd they stop buying the cooking pot" and "tell me more
    about the steamer" are all correct (stockout 56 days and price +26%;
    steamer 2.8×, $48, down 17%).
  - Sales, call list, catalog and holiday questions are unaffected.
- **Occasion section (added 2026-10-22):** built in the stand-in table from
  four reference tables loaded by `python sql/load_holidays.py`:
  - `ref_community_holidays`, from [sql/community_holidays.csv](sql/community_holidays.csv).
  - `ref_holiday_products`, from [sql/holiday_products.csv](sql/holiday_products.csv):
    191 rows drafted by Gemma (`python sql/draft_holiday_inputs.py products`),
    each match term checked against the catalog. Khmer New Year got no reply.
  - `ref_account_communities`, from [sql/account_communities.csv](sql/account_communities.csv):
    drafted by Gemma (`... communities`), then `python sql/name_rules.py`
    sets 297 stores by name. The other 1,599 are Gemma's first pick, and too
    many are "thai" (our DF/Dragonfly house brand is Thai), so **reps should
    review**. A row with source=rep is never overwritten.
  - `ref_holiday_local_dates`, from [sql/holiday_local_dates.csv](sql/holiday_local_dates.csv):
    Madison and Saint Paul Hmong New Year.
  - Rule: an occasion item needs a lift before the holiday, either this
    store's (≥ 1.3×) or that of stores celebrating it (≥ 1.15×). At most 3
    items per dish.
  - Past-year windows use this year's month and day; lunar holidays are
    approximate until the calendar has 2019–2025 dates.
- **No fixed template (2026-10-22):** the plan is a menu of idea types; the
  model picks 2–4 angles for this store and writes them in a ```plan block.
  The card shows "For this visit" (its angles, facts filled from the rows),
  then a collapsed "More ideas".
  - New idea types: promo_ending, price_drop, low_stock (our stock at ≤ 25% of
    its 90-day level), peer_gap (≥ 30% of same-community stores buy it; this
    store never has), own_brand, alternative (when we're out), buying_less
    (last 6 months ≤ 40% of the 6 before). The store's overall ordering
    rhythm is passed too.
  - US-wide holidays (Thanksgiving) need the store's own lift; otherwise the
    all-store 6.4× condensed milk went to every store.
  - To check:
    - own_brand / alternative match on two shared name words, so a jam can
      stand in for a powder (DF Purple Yam Jam for Ube Powder). Needs a
      tighter match (same unit/size, or a reviewed mapping).
    - Teans crispy chili shows "$128 → $68 (47% less)" at several stores: may
      be a pack-size change rather than a price drop. Compare case_qty before
      calling it a drop.
  - Result: Hmong Legacy → Hmong New Year (Oct 31, Madison); Shwe and Pyone
    Cho → Thadingyut; Siam → Loy Krathong (Red Boat fish sauce 6.5× at that
    store); all stores → Thanksgiving where condensed milk sells 6.4× before
    it. The agent now raises the holiday consistently.

---

## 3. To put it live (production)

- [ ] **Test Tavily from the office server itself** (ustrading-ai-server has
      unreliable outbound internet):
      `curl -s -m 10 https://api.tavily.com -o /dev/null -w "%{http_code} %{time_total}s\n"`
- [ ] If it times out, ask whoever manages the firewall to allow
      `api.tavily.com`, and `api.openrouteservice.org` at the same time (road
      routes have been failing there since Oct 16).
- [ ] Commit on a branch and open a PR.
- [ ] Deploy `server.py`, `databricks_tools.json` and `agent_prompt.md` to the
      office server.
- [ ] In the office server's `.env`: add `TAVILY_API_KEY` and set
      `LLM_PROXY_MAX_TOOL_ROUNDS=4`. Restart server.py.
- [ ] Push the prompt to the production agent: `python configure_prompt.py --apply`.
- [ ] Rerun the test conversations (section 8) against production.

---

## 4. Open work, roughly in priority order

### 4.1 Fact check before an answer is spoken (do before reps rely on this)
In 1 of 4 voice-path test runs the agent invented numbers: "465 units" (the
data said 21,600) and "haven't ordered since Sep 16" (it was Jun 6, 2025).
Both came in turns answered from memory. A prompt can't fully stop a mid-size
model from doing this.
- [ ] Before the answer goes out, check every $ amount, quantity and date
      against this call's lookup results. If one isn't found, ask the model
      again. About 40 lines in server.py.

### 4.2 Remove the prompt example that teaches "festival → lapsed item"
- [ ] [agent_prompt.md](agent_prompt.md), "Looking things up on the web"
      section, still says: *"Hmong New Year three weeks out is a reason to
      pitch the sweet rice flour a Hmong store has stopped buying."* Gemma
      copies examples closely; this is likely why it keeps pairing festivals
      with lapsed items. Replace or delete it.

### 4.3 Festivals: which products actually sell for them
Rep feedback: the agent correctly found Hmong New Year (Oct–Nov) but kept
recommending lapsed items that aren't used for it.
- [ ] Decide the direction first (see 4.4). Options discussed:
  - **Holiday → product category list** (categories, not SKUs, so it
    survives catalog changes), with rep review. Then flag festival items in
    the pitch data and rank: festival items they buy regularly → other due
    items → festival items they've never bought → lapsed items with a reason.
  - **Check with sales data:** invoices go back to 2016, so measure which
    categories actually sell more in the 3–4 weeks before each holiday at
    that community's stores. First look (`seasonal_demand` tool): most sweet
    rice does **not** sell more before Hmong New Year. The assumption needs
    checking.
- [ ] Which community each store serves: `customer_group` is blank for most
      stores. Work it out from name and purchases, have reps tag accounts, or
      both.
- [ ] Holiday dates per year (lunar holidays move; Hmong New Year differs by
      city: Madison Oct 31–Nov 1, St. Paul late Nov/early Dec, Eau Claire mid-Oct).
- Open questions: which communities matter most? Who reviews the list (the
  rep, or a sales manager)?

### 4.4 Rules vs the model's own intelligence (the bigger design question)
The user wants the model to decide what to look up rather than follow fixed
logic. Today every Databricks tool is a pre-written query with one input, so
the model chooses from a fixed menu; "what to pitch" always goes through the
10-item pitch list.
- Experiment results (section 5): with free choice, Gemma takes the shortcut.
  Forced to explore, it chains tools but chooses poorly and slowly.
- [ ] Decide between:
  - keep the structured approach (A) and give it a better menu (4.5);
  - test a stronger model on the flexible tools (4.6), then decide;
  - both.

### 4.5 Better pitch-list "menu"
- [ ] Include **store staples** even when not overdue. The experiment found
      Hmong Legacy buys "Apple (USA) Sweet Rice" constantly (56 times, last
      Aug 12, 2026), and the pitch list never shows it.
- [ ] Revisit the lapsed definition: an item bought 3 times in the last year
      counts as lapsed today. Consider "bought within the last year = not
      lapsed".
- [ ] `lookup_pitch_list` is sometimes called with the store name instead of
      the key, which returns 0 rows and wastes a round. Tighten its value hint,
      or resolve names server-side.

### 4.6 Add Claude as a switchable model
Goal: compare Gemma with a stronger model on the same tools and data.

**Option 1 (recommended): Claude behind server.py**
- A settings dropdown, "AI model: Gemma | Claude", sent per call in
  `customLlmExtraBody` (the same path as `scope_token`;
  [public/app.js](public/app.js) line ~449, read in server.py as
  `elevenlabs_extra_body`).
- server.py routes each call to Gemma or Claude, translating OpenAI-format
  messages and tools to Anthropic's Messages API and back. Keep Claude's
  thinking blocks attached within a turn's tool loop and pass them back
  unchanged.
- Everything else stays the same: rep scoping, tools, readers, web search,
  visuals.
- **Needs an Anthropic API key:**
  - Get one at console.anthropic.com with a company account.
  - Set a spend limit (e.g. $20 for the trial).
  - Put it in `.env` as `ANTHROPIC_API_KEY`.
  - No free or offline option: Claude only runs on Anthropic's servers.
- **Data:** each Claude turn sends that conversation's store names, items and
  prices to Anthropic, even when the server runs locally.
- **Office server internet** is unreliable, so test reaching
  `api.anthropic.com` from there.
- Can be built and tested **without a key** first: the dropdown shows Claude
  as unavailable, and the message translation is tested offline on recorded
  conversations.

**Option 2: ElevenLabs' built-in models** (no extra key, billed by ElevenLabs)
- The model runs inside ElevenLabs and **bypasses server.py**, so you lose:
  per-rep scoping, everything in section 2, and the fix for silent replies.
  The Databricks token would be stored with ElevenLabs again.
- Per agent, not per call: a second agent picked from the existing Agent
  dropdown.
- Only useful as a quick, unfair look.

**Decisions still open:**
- [ ] Add the official `anthropic` Python package (recommended; adds one
      `pip install anthropic` and changes the "no dependencies" line in
      README/DEPLOY), or keep stdlib-only and call the REST API with urllib.
- [ ] Which model: Opus 5.5 (`claude-opus-5-5`, strongest, run at low effort
      for voice latency), Sonnet 5.5 (`claude-sonnet-5-5`, faster, half the
      price), Haiku 5.5 (`claude-haiku-5-5`, fastest, weaker reasoning), or
      all of them in the dropdown.
- [ ] Exposure: test agent only at first (recommended), or everywhere.

### 4.7 Move the view into dbt
- [ ] Port [sql/mart_account_item_pitch.sql](sql/mart_account_item_pitch.sql)
      into the dbt project as a model (or fold the columns into
      `mart_account_item_opportunities`), then drop the hand-made view. The
      dbt project path is still needed.

### 4.8 Smaller items
- [ ] Older backorders: the 2022 Mochiko case ("we were out when they last
      ordered") can't be checked. Stock history starts Oct 2025 and app
      backorder events start Feb 2026. It would need ERP order lines (ordered
      vs shipped) as a modeled `int_` table. Ask the data team; never query
      the raw replicas.
- [ ] Competitor prices: no data exists. Optionally let reps say "they buy it
      from X for $60" and log it.
- [ ] `shippable_qty` units: DF Water Chestnut shows 21,600 with case_qty 24
      and base unit CASE. Check whether that's cases or eaches before the
      agent says "21,600 cases".
- [ ] The agent keeps offering to "check stock" when stock already came with
      the pitch list. Existing habit.
- [ ] Pitch questions now take 13–18s (web search added), up from 8–10s. The
      agent fills the wait with "Let me check… Still checking."

---

## 5. Flexible-tools experiment (2026-10-22)

Harness: [experiments/explore_tools.py](experiments/explore_tools.py). It
adds two read-only tools:
- `store_product_history(customer_key, product_words)`: has this store bought
  X, how often, when, and our price and stock now.
- `seasonal_demand(product_words, event_date, store_name_word?)`: sales in the
  4 weeks before a date vs a usual 4 weeks, 2023–2025.

| Setup | Hmong Legacy | Pyone Cho | Time |
|---|---|---|---|
| **A. Today** (pitch list + timing reminder) | Madison Hmong New Year → prawn crackers, water chestnut (17% cheaper), cooking pot | Found Thadingyut (Oct 26–27) → mung bean, coconut cream, coffee with prices | 13–18s |
| **B. Flexible tools, pitch list available** | Pitch list only, no festival | Pitch list only | 8–19s |
| **C. Flexible tools, no pitch list** | Explored, but pitched fish sauce for Hmong New Year | Led with an out-of-stock item, then shower cream | 12–25s, up to 9 steps |

Conclusion: Gemma 26B takes the easy path when there is one, and chains tools
poorly when forced. The structured setup (A) works best with this model. Rerun
setup C with a stronger model (4.6) to decide the long-term design.

### 5b. Occasion-first prototype (options 1 + 3, 2026-10-22)

Script: [experiments/occasion_pitch.py](experiments/occasion_pitch.py). Steps:
1. Gemma gets the store's name, town and most-bought items, but **not** the
   pitch list. It names the community, the occasions in the next 6 weeks
   (dates searched on the web) and what the community buys for them.
2. The server checks each idea at once:
   - does this store buy it, and does it buy **more than its usual rate** in
     the 5 weeks before the date (2019–2025);
   - do stores with the community word in their name buy more before it;
   - is it in stock.
3. The store's own list, in groups: staples, due now, win-backs (each with
   its own reason), suggestions.
4. Gemma writes the pitch; win-backs may not be presented as holiday items.
5. Gemma checks its draft against the data; code flags numbers not in the data.

Results:
- **Hmong Legacy:** "Hmong New Year is coming up at the end of the month:
  pitch the Nam Sausage or the Banh Pho noodles, as they tend to buy more of
  those before the holiday… the Chaokoh Coconut Milk, which is a staple for
  them." All true: the store buys 2.1× its usual Nam Sausage and 1.5× its
  usual Banh Pho before New Year. No lapsed items as holiday picks, no invented
  numbers.
- **Pyone Cho:** with no occasion in the next 6 weeks, it falls back to due
  items and doesn't force a festival in. But it never recognizes the store as
  **Burmese**; it guessed Thai, Vietnamese and South Asian on different runs.

Lessons:
- **"Bought in the window" is not holiday evidence** for a store that buys an
  item every few weeks. Compare against the store's own usual rate.
- **Date arithmetic belongs in code.** Asked for "the next six weeks", Gemma
  returned a festival a month past and one four months out.
- **Ideas need a clue to the community.** Name and town alone gave Diwali for
  a Burmese store; most-bought items helped, but not enough. Fix: a stored
  community per account (rep-tagged, or set once by a model and reviewed).

**Eight-store check** (latest run of each; every claim checked against the data):

| Store | Community | Occasion | Result |
|---|---|---|---|
| Hmong Legacy (Madison) | Hmong | Hmong New Year Oct 31 | ✅ Nam Sausage 2.1×, Banh Pho 1.5× before it |
| Xiangkhouang (Oshkosh) | Hmong, inferred without "Hmong" in the name | Hmong New Year (Madison's date) | ✅ fish sauce 4.2×, sweet rice 1.4× |
| Shwe (Columbia, MO) | Burmese | Thadingyut | ✅ coconut milk 1.6–1.9×, shrimp fish sauce 1.8× |
| Isabela (Billings) | Filipino | All Saints' Day / Undas Nov 1 | ✅ duck eggs 1.9×, beef bouillon 2.4× |
| Viet Hoa (Madison) | Vietnamese | none in 6 weeks | ✅ fell back to due items on promotion |
| Mei Hua (West Allis) | guessed Vietnamese (name suggests Chinese) | none | ✅ fell back to due items |
| Siam (Rockford) | Thai | Loy Krathong Nov 25 | ⚠️ ran before the last fixes; called weak links "strongly tied" |
| Pyone Cho (Indianapolis) | never read as Burmese | none | ⚠️ correct fallback, but missed Thadingyut |

Fixes made during the check:
- An occasion must be one the community celebrates (yes/no check). Before
  it, a Vietnamese store got Diwali. Ask with the plain word ("Burmese"); a
  long description made it reject Thadingyut.
- Occasion items are sorted strongest first, with weak ones left out. Before,
  it ignored 4.2× evidence and pitched due items.
- The store's own town goes in the data. Before, Shwe Market in Columbia
  became "Shwe Market in Madison".
- A draft written as tool-call text is retried; a check that replies with
  commentary is ignored.

Still seen:
- Festival dates differ between sources (Thadingyut Oct 25 vs Oct 27 on
  different runs).
- Oshkosh got Madison's Hmong New Year date.
- A fix in one place can break another (the yes/no check), so keep these 8
  stores as a regression set and rerun them after every change.

Before it can go in the app:
- [ ] Minimum volume for "similar stores" lift: a few purchases at a few
      stores can show 5×.
- [ ] **Speed:** 15–31s end to end. Steps 1–2 depend only on the store and
      the week, so run them **overnight** for every account and store the
      results. The live call is then steps 3–5, about 5–8s.
- [ ] Store the community per account (see Lessons) instead of guessing it
      on every call.
- [ ] Catalog matching relies on Gemma's name variants ("sticky rice | sweet
      rice | glutinous"). Indian items found nothing for Pyone Cho; check
      coverage for each community.
- [ ] The self-check misses spelled-out numbers ("six of the last seven
      years"). Add word-number checking to the code check.

---

## 6. Things learned about the environment

- **This PC's network** drops DNS lookups intermittently. Searches and
  Databricks calls sometimes fail on the first try; the first TLS connection
  to Tavily once took 17s.
- **Databricks warehouse cold start** adds 10–15s. Before a demo, ask
  something simple first ("How am I doing this month?").
- **`chat_test.py` waits only 10s of silence** per turn, so it can cut off a
  slow first turn. `convai.converse(quiet=25)` is closer to a real caller.
- **`proxy_test.py`** runs the local prompt and server with no ElevenLabs:
  fastest for iterating.
- **Gemma acts on reminders next to the data**, not on rules deep in the
  system prompt. A prompt rule to "think about the calendar" did nothing; the
  same reminder on the pitch-list result worked.

---

## 7. Local voice testing setup (reproduce)

1. `APP_ENV=test python server.py --port 8080 --no-browser`
2. `"C:/Program Files (x86)/cloudflared/cloudflared" tunnel --url http://localhost:8080`
   and copy the `https://….trycloudflare.com` URL.
3. `APP_ENV=test python configure_proxy.py --on https://<tunnel-url>`
   (changes only **SalesRep-test**).
4. `APP_ENV=test python configure_prompt.py --apply`
5. Open http://localhost:8080, pick **Ryan Tran** (rep 032), start a call.

Quick tunnels expire. When the session ends, SalesRep-test points at a dead
address until step 3 is rerun. (The "secret cannot be deleted" error from
`configure_proxy.py --on` is harmless: other agents still use that secret.)

---

## 8. Test conversations (as Ryan Tran)

**Natural pitch (should search for the festival on its own):**
1. "Seeing Hmong Legacy next week. What should I pitch them?"
2. "Why'd they stop buying the cooking pot?"
3. "How do I handle the price going up?"

**Related follow-ups across a call:**
"Who should I call today?" → "Tell me more about the first one." → "I'll see
them next week. What should I pitch?" → "Is the water chestnut in stock?" →
"Why'd they stop buying the cooking pot?" → "How do I handle the price going
up?" → "OK. What about the second store on your list?" → "Anything going on in
their community I could use?"

**Guardrails (should not use the web):** "How much is our jasmine rice?",
"How am I doing this month?"

**Expected facts for Hmong Legacy (as of 2026-10-21):**
- Alum Cooking Pot: last paid $85.59, now $108 (+26%); out of stock 56 days
  around when it was due (Mar 16 – May 19, 2026).
- DF Water Chestnut (Whole): last paid $36.28, now $30 (−17%); out 21 days
  when due; 21,600 shippable.
- Mochiko Sweet Rice Flour: lapsed, 3 purchases, last Mar 28, 2022; last paid
  $62.87, now $76 (+21%).
- Madison Hmong New Year: Oct 31 – Nov 1, 2026, Alliant Energy Center.
