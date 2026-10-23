"""Experiment: an occasion-first pitch -- options 1 + 3 in todo.md.

Read-only: changes nothing in Databricks, the server or either agent.

    python experiments/occasion_pitch.py "Hmong Legacy" "Seeing them next week. What should I pitch?"

1. Ideas first. Gemma is told the store's name and town -- not its pitch list --
   and asked which occasions are coming for the community it serves and what
   that community buys for them. It may search the web for local dates. Asking
   before it sees the list is the point: shown the list first, it worked back
   from whatever was overdue to the festival ("Mochiko sounds festive").
2. The server checks every idea at once: has this store bought it, did it buy
   it before this occasion in past years, does it sell more before that date
   across our stores, is it in stock.
3. The store's own list, in labelled groups: staples, due now, win-backs (each
   with its own reason), suggestions.
4. Gemma writes the pitch from 1-3.
5. Gemma checks its draft against the data and fixes what it finds; a plain
   code check then flags any number in the answer that the data does not hold.
"""
import datetime
import json
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import explore_tools as ex

s = ex.s
REP, MODEL = ex.REP, ex.MODEL


def gemma(messages, tools=None, max_tokens=900):
    completion = s.call_upstream_llm(messages, MODEL, max_tokens, tools or [])
    return ((completion.get("choices") or [{}])[0]).get("message") or {}


def as_json(text):
    """The first JSON object in a reply, fences and chatter around it ignored."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        return json.loads(text[start:end + 1]) if start >= 0 else {}
    except ValueError:
        return {}


def variants_clause(col, variants):
    parts = [ex.words_clause(col, v) for v in (variants or "").split("|") if v.strip()]
    parts = [p for p in parts if p != "FALSE"]
    return "(" + " OR ".join("(%s)" % p for p in parts) + ")" if parts else "FALSE"


def say_date(value):
    try:
        return s._day(value)
    except (ValueError, TypeError):
        return str(value)


# ------------------------------------------------------------------ 1. ideas

IDEAS_PROMPT = """You plan sales visits for U.S. Trading Company, a wholesale distributor of Asian
food and restaurant supplies. Today is {today}.

Think about the store below: which community it serves, judging from its name
and where it is, and which holidays, festivals or seasons for that community
fall in the next six weeks. Use web_search to confirm local dates -- search the
topic with the city, state and year, never the store's name.

Then list up to five products that community actually buys for those
occasions: what people cook, serve or need for them. Think about the occasion,
not about any store's order history -- that is checked later.

Reply with JSON only:
{{"community": "...",
  "community_word": "one word a store serving this community might have in its name (e.g. Hmong, Filipino), or empty",
  "occasions": [{{"name": "...", "date": "YYYY-MM-DD", "source": "where you found the date"}}],
  "ideas": [{{"product": "plain name",
             "search": "2-4 name variants separated by |, in words a wholesale catalog label would use",
             "occasion": "...", "why": "how it is used for the occasion"}}]}}
If nothing falls in the next six weeks, return empty lists."""


def occasion_ideas(store):
    # What the store buys tells Gemma whom it serves. From the name and town
    # alone, a Burmese store in Indianapolis came out as "South Asian" and the
    # occasion as Diwali. Its most-bought items over the year -- not the pitch
    # list, which would anchor the ideas on whatever is overdue -- are a fair
    # clue to the community without being a menu.
    bought = ex.sql_rows("""
        SELECT d.item_label FROM ust_databricks.ust_intermediate.int_customer_item_purchases p
        JOIN ust_databricks.ust_dims.dim_items d USING (item_no)
        WHERE p.customer_key = '%s' AND p.sold_quantity > 0 AND p.posting_date >= date_sub(current_date(), 365)
        GROUP BY d.item_label ORDER BY count(DISTINCT p.posting_date) DESC LIMIT 15""" % store["customer_key"])
    names = ", ".join(s._title_case(r["item_label"]) for r in bought.get("rows") or [])
    messages = [
        {"role": "system", "content": IDEAS_PROMPT.format(today=datetime.date.today().isoformat())},
        {"role": "user", "content": (
            "Store: %s, in %s (post code %s).\nWhat it buys most, as a clue to the community it "
            "serves (not a list to choose from): %s." % (
                store["customer_name"], store.get("city") or "?", store.get("post_code") or "?",
                names or "unknown"))},
    ]
    searches = []
    for _ in range(3):
        msg = gemma(messages, [s.WEB_SEARCH_TOOL])
        calls = msg.get("tool_calls") or []
        if not calls:
            break
        messages.append(msg)
        for c in calls:
            query = (json.loads(c["function"].get("arguments") or "{}")).get("value", "")
            searches.append(query)
            messages.append({"role": "tool", "tool_call_id": c.get("id"),
                             "content": s.web_search(query, [], REP)})
    else:
        msg = gemma(messages, [])
    ideas = as_json(msg.get("content"))
    if ideas.get("occasions") and not searches:
        # A date from memory is a guess; lunar and local dates move every year.
        messages += [msg, {"role": "user", "content": "Confirm each occasion's date for this city and "
                                                      "year with web_search, then reply with the JSON again."}]
        for _ in range(2):
            msg = gemma(messages, [s.WEB_SEARCH_TOOL])
            calls = msg.get("tool_calls") or []
            if not calls:
                break
            messages.append(msg)
            for c in calls:
                query = (json.loads(c["function"].get("arguments") or "{}")).get("value", "")
                searches.append(query)
                messages.append({"role": "tool", "tool_call_id": c.get("id"),
                                 "content": s.web_search(query, [], REP)})
        else:
            msg = gemma(messages, [])
        ideas = as_json(msg.get("content")) or ideas
    return ideas, searches


# ------------------------------------------------------------------ 2. evidence

def community_word_ok(word):
    """A store-name word worth filtering on: at least three of our stores carry it."""
    if not word:
        return ""
    got = ex.sql_rows("SELECT count(*) AS n FROM ust_databricks.ust_dims.dim_customers "
                      "WHERE lower(customer_name) LIKE '%%%s%%'" % word)
    rows = got.get("rows") or [{}]
    return word if int(rows[0].get("n") or 0) >= 3 else ""


def idea_evidence(key, idea, occasion_date, community):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", occasion_date or "")
    mo, dy = (int(m.group(2)), int(m.group(3))) if m else (1, 1)
    word = re.sub(r"[^a-z]", "", (community or "").lower())
    sql = """
    WITH items AS (
        SELECT item_no, item_label, unit_price, is_in_stock, shippable_qty
        FROM ust_databricks.ust_dims.dim_items
        WHERE is_sellable AND NOT is_blocked AND {match}),
    store AS (
        SELECT p.item_no, count(DISTINCT p.posting_date) AS times_bought, max(p.posting_date) AS last_bought,
               -- 2019-2025: the five weeks before the date, against a usual five weeks.
               sum(CASE WHEN year(p.posting_date) BETWEEN 2019 AND 2025
                         AND datediff(make_date(year(p.posting_date), {mo}, {dy}), p.posting_date) BETWEEN 0 AND 34
                        THEN p.sold_quantity ELSE 0 END) / 7 AS store_before_qty,
               sum(CASE WHEN year(p.posting_date) BETWEEN 2019 AND 2025 THEN p.sold_quantity ELSE 0 END)
                   / (7 * 365 / 35.0) AS store_usual_qty
        FROM ust_databricks.ust_intermediate.int_customer_item_purchases p JOIN items i USING (item_no)
        WHERE p.customer_key = '{key}' AND p.sold_quantity > 0 GROUP BY p.item_no),
    lift AS (
        SELECT p.item_no,
               sum(CASE WHEN datediff(make_date(year(p.posting_date), {mo}, {dy}), p.posting_date) BETWEEN 0 AND 27
                        THEN p.sold_quantity ELSE 0 END) / 3 AS before_qty,
               sum(p.sold_quantity) / 39.0 AS usual_qty,
               count(DISTINCT p.customer_key) AS stores
        FROM ust_databricks.ust_intermediate.int_customer_item_purchases p JOIN items i USING (item_no)
        {community_join}
        WHERE p.posting_date >= '2023-01-01' AND p.posting_date < '2026-01-01' AND p.sold_quantity > 0
        GROUP BY p.item_no)
    SELECT i.item_label, i.unit_price, i.is_in_stock, i.shippable_qty,
           coalesce(st.times_bought, 0) AS times_bought, st.last_bought,
           round(st.store_before_qty / nullif(st.store_usual_qty, 0), 2) AS store_lift,
           round(l.before_qty / nullif(l.usual_qty, 0), 2) AS lift, l.stores
    FROM items i LEFT JOIN store st USING (item_no) LEFT JOIN lift l USING (item_no)
    ORDER BY times_bought DESC, i.is_in_stock DESC, lift DESC NULLS LAST
    LIMIT 3""".format(
        match=variants_clause("item_label", idea.get("search") or idea.get("product")),
        mo=mo, dy=dy, key=key,
        community_join=("JOIN (SELECT DISTINCT customer_key FROM ust_databricks.ust_dims.dim_customers "
                        "WHERE lower(customer_name) LIKE '%%%s%%') c USING (customer_key)" % word) if word else "")
    out = ex.sql_rows(sql)
    lines = []
    for r in out.get("rows", []):
        bits = []
        times = int(float(r["times_bought"]))
        store_lift = float(r["store_lift"]) if r.get("store_lift") not in (None, "") else None
        if times:
            bits.append("this store bought it %d times, last on %s" % (times, say_date(r["last_bought"])))
            if store_lift is not None and store_lift >= 1.3:
                bits.append("in the five weeks before this date it buys %.1f times its usual amount"
                            " (2019-2025)" % store_lift)
            elif store_lift is not None:
                bits.append("it buys this all year, no more than usual before this date")
        else:
            bits.append("this store has never bought it from us")
        if r.get("lift") not in (None, ""):
            lift = float(r["lift"])
            who = "stores with '%s' in their name" % word.title() if word else "our stores"
            if lift >= 1.15:
                bits.append("%s buy %.1f times their usual amount in the four weeks before" % (who, lift))
            else:
                bits.append("%s do not buy more of it before this date" % who)
        stock = str(r["is_in_stock"]).lower() == "true"
        bits.append(("in stock, %s" % s._price(r["unit_price"])) if stock else "OUT OF STOCK now")
        strength = ("strong" if stock and ((store_lift or 0) >= 1.3 or
                                          (times >= 4 and r.get("lift") and float(r["lift"]) >= 1.15))
                    else "some" if stock and (times or (r.get("lift") and float(r["lift"]) >= 1.15))
                    else "weak")
        lines.append("%s [%s evidence]: %s." % (s._title_case(r["item_label"]), strength, "; ".join(bits)))
    return lines


# ------------------------------------------------------------------ 3. the store's own list

def store_groups(key):
    pitch = json.loads(s.run_tool_call("lookup_pitch_list", key, REP)).get("rows") or []
    groups = {"Due now (regular items)": [], "Win-backs (lapsed, each with its own reason)": [],
              "Suggestions (never bought)": []}
    for r in pitch:
        words = r.get("in_words") or r.get("item_label")
        if r.get("purchase_pattern") == "regular":
            groups["Due now (regular items)"].append(words)
        elif r.get("purchase_pattern") == "lapsed":
            if "possible reasons" in words:      # a lapsed item with no reason is not worth the breath
                groups["Win-backs (lapsed, each with its own reason)"].append(words)
        else:
            groups["Suggestions (never bought)"].append(words)
    staples = ex.sql_rows("""
        SELECT d.item_label, count(DISTINCT p.posting_date) AS days_bought, max(p.posting_date) AS last_bought,
               d.unit_price, d.is_in_stock
        FROM ust_databricks.ust_intermediate.int_customer_item_purchases p
        JOIN ust_databricks.ust_dims.dim_items d USING (item_no)
        WHERE p.customer_key = '%s' AND p.sold_quantity > 0 AND p.posting_date >= date_sub(current_date(), 365)
        GROUP BY ALL ORDER BY days_bought DESC LIMIT 5""" % key).get("rows") or []
    groups = {"Staples (what they buy most in the last year)": [
        "%s: bought on %s days in the last year, last on %s; %s." % (
            s._title_case(r["item_label"]), r["days_bought"], say_date(r["last_bought"]),
            ("in stock, %s" % s._price(r["unit_price"])) if str(r["is_in_stock"]).lower() == "true"
            else "OUT OF STOCK now") for r in staples], **groups}
    return groups


# ------------------------------------------------------------------ 4 + 5. write and check

WRITE_GUIDE = """

## What you have for this visit

{data}

## How to build this pitch

Choose two or three things to say, for this visit, from the groups above.

- Occasion picks come only from "For the occasion": products the occasion
  calls for, each with the evidence beside it. Lead with strong evidence and
  things in stock; say what it rests on. "They stock up before New Year" only
  when the entry says this store buys more than usual before the date; an item
  it "buys all year" is a staple to keep them stocked on, not a holiday habit.
- A win-back is a separate conversation. Raise one only for its own reason --
  we ran out, the price moved, they asked for it -- and never present it as a
  holiday item unless it also appears under "For the occasion".
- Staples and due items are the easy part of the order; one is often worth a
  mention alongside the occasion pick.
- Never lead with anything out of stock."""

CHECK_PROMPT = """You check a sales assistant's answer before it is spoken to a sales rep.

The data the assistant had:
{data}

The answer:
{draft}

Check:
1. Every product named appears in the data.
2. Every number, price and date appears in the data.
3. Each holiday or festival link rests on a "For the occasion" entry, not on a
   win-back or a due item dressed up as festive.
4. It does not lead with something out of stock.

If all four hold, reply with exactly: OK
Otherwise reply with the corrected answer only, same length and voice."""


def numbers_not_in(answer, data):
    found = set(re.findall(r"\$?\d[\d,]*(?:\.\d+)?", answer or ""))
    flat = data.replace(",", "")
    return sorted(n for n in found if n.replace(",", "").lstrip("$") not in flat
                  and len(n.replace(",", "").lstrip("$")) > 1)


def run(store_name, question):
    t0 = time.time()
    store = json.loads(s.run_tool_call("find_customer_by_name", store_name, REP)).get("rows") or []
    if not store:
        print("  no such store for %s" % REP)
        return
    store = store[0]
    key = store["customer_key"]
    print("  store : %s (%s), %s" % (store["customer_name"], key, store.get("city")))

    t = time.time()
    ideas, searches = occasion_ideas(store)
    print("\n  1. IDEAS (%.0fs) searched: %s" % (time.time() - t, searches))
    print("     community: %s" % ideas.get("community"))
    for o in ideas.get("occasions") or []:
        print("     occasion : %s %s (%s)" % (o.get("name"), o.get("date"), o.get("source")))
    for i in ideas.get("ideas") or []:
        print("     idea     : %s  [%s]  -- %s" % (i.get("product"), i.get("search"), i.get("why")))

    t = time.time()
    # Only what is still ahead and within six weeks. Asked for "the next six
    # weeks", Gemma returned a festival a month past and one four months out.
    today = datetime.date.today()
    occasions = {}
    for o in ideas.get("occasions") or []:
        try:
            when = datetime.date.fromisoformat(str(o.get("date"))[:10])
        except ValueError:
            continue
        if not 0 <= (when - today).days <= 42:
            print("     dropped  : %s on %s (not in the next six weeks)" % (o.get("name"), o.get("date")))
            continue
        # Asked for this community's occasions, Gemma gave a Vietnamese store
        # Diwali. A plain yes/no question on its own is one it answers well --
        # asked about the plain community word: put as "the Burmese/Southeast
        # Asian community in Columbia, Missouri" it said no to Thadingyut.
        people = (ideas.get("community_word") or ideas.get("community") or "").strip()
        answer = (gemma([{"role": "user", "content": (
            "Do %s people traditionally celebrate %s? Answer yes or no only."
            % (people or "the store's", o.get("name")))}], max_tokens=5).get("content") or "")
        if answer.strip().lower().startswith("yes"):
            occasions[o.get("name")] = when.isoformat()
        else:
            print("     dropped  : %s (not this community's occasion)" % o.get("name"))
    if not occasions:
        ideas["ideas"] = []      # no occasion, so no occasion picks: the store's own list carries it
    first_date = next(iter(occasions.values()), "")
    word = community_word_ok(re.sub(r"[^a-z]", "", (ideas.get("community_word") or "").lower()))
    print("     store-name word for 'similar stores': %s" % (word or "(none -- all stores)"))
    results, threads = {}, []
    for idea in ideas.get("ideas") or []:
        def work(idea=idea):
            date = occasions.get(idea.get("occasion")) or first_date
            results[idea.get("product")] = (idea, idea_evidence(key, idea, date, word))
        threads.append(threading.Thread(target=work))
    groups_box = {}
    threads.append(threading.Thread(target=lambda: groups_box.update(store_groups(key))))
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    print("\n  2+3. EVIDENCE AND GROUPS (%.0fs, all queries at once)" % (time.time() - t))

    rank = {"[strong evidence]": 0, "[some evidence]": 1}
    picks = sorted(((rank[m.group(0)], l, idea) for idea, lines in results.values() for l in lines
                    for m in [re.search(r"\[(strong|some) evidence\]", l)] if m),
                   key=lambda x: x[0])
    occasion_lines = ["  - %s (for %s: %s)" % (l, idea.get("occasion"), idea.get("why")) for _, l, idea in picks]
    # The store's own town, or the model supplies one: Shwe Market in Columbia
    # came out as "Shwe Market in Madison", the town the prompt's examples use.
    data = "Store: %s, in %s.\n\nOccasions: %s\n\nFor the occasion (strongest first):\n%s" % (
        s._title_case(store["customer_name"]), (store.get("city") or "").title(),
        "; ".join("%s on %s" % (n, d) for n, d in occasions.items()) or "none in the next six weeks",
        "\n".join(occasion_lines) or "  (nothing)")
    for name, rows in groups_box.items():
        data += "\n\n%s:\n%s" % (name, "\n".join("  - " + r for r in rows) or "  (none)")
    print("\n".join("     " + l for l in data.splitlines()))

    t = time.time()
    system = open(Path(ex.REPO) / "agent_prompt.md", encoding="utf-8").read().replace("{{rep_name}}", REP)
    system += WRITE_GUIDE.format(data=data)
    convo = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    draft = (gemma(convo).get("content") or "").strip()
    if not draft or "tool_call" in draft or re.match(r"^\W*call:", draft):
        # With no tools on offer, Gemma once wrote its tool call out as text.
        convo += [{"role": "assistant", "content": draft},
                  {"role": "user", "content": "You already have everything above. Answer in plain spoken words."}]
        draft = (gemma(convo).get("content") or "").strip()
    print("\n  4. DRAFT (%.0fs):\n     %s" % (time.time() - t, draft))

    t = time.time()
    verdict = (gemma([{"role": "user", "content": CHECK_PROMPT.format(data=data, draft=draft)}])
               .get("content") or "").strip()
    commentary = re.search(r"(?i)(cannot check|please provide|the assistant|the answer (is|was))", verdict)
    final = draft if verdict.upper().startswith("OK") or commentary or not verdict else verdict
    print("\n  5. CHECK (%.0fs): %s" % (time.time() - t, "OK, unchanged" if final == draft else "revised"))
    print("\n  FINAL (%.0fs total):\n     %s" % (time.time() - t0, final))
    stray = numbers_not_in(final, data)
    print("\n  numbers not found in the data: %s" % (stray or "none"))
    city = (store.get("city") or "").lower()
    towns = ("madison", "milwaukee", "chicago", "indianapolis", "kansas city", "oshkosh",
             "columbia", "rockford", "billings", "st. paul", "saint paul", "west allis")
    wrong = [c for c in towns if c in final.lower() and c != city]
    print("  other towns named: %s" % (wrong or "none"))


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "What should I pitch them?")
