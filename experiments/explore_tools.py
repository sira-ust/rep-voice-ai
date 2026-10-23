"""Experiment: Gemma with question-shaped tools instead of a fixed pitch list.

Read-only: changes nothing in Databricks, the server or either agent. Reuses
server.py's functions, adds two experimental tools (store_product_history,
seasonal_demand) and strips the prompt lines that steer toward lookup_pitch_list.
Runs as Ryan Tran against the real Gemma host. See todo.md, "Flexible tools".

    python experiments/explore_tools.py "question" ["follow-up" ...]
    NO_PITCH=1 python experiments/explore_tools.py "..."   # without lookup_pitch_list
"""
import sys, json, os, re, time
REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
import server as s
import databricks_tool as dt

REP = "Ryan Tran"
MODEL = os.environ.get("CUSTOM_LLM_MODEL", "gemma4-26b")
MAX_ROUNDS = 8


def sql_rows(sql):
    for attempt in range(3):
        try:
            r = dt.run_adhoc(sql); break
        except Exception:
            if attempt == 2: raise
            time.sleep(2)
    if (r.get("status") or {}).get("state") != "SUCCEEDED":
        return {"error": str((r.get("status") or {}).get("error"))[:300]}
    cols = [c["name"] for c in r["manifest"]["schema"]["columns"]]
    rows = [dict(zip(cols, row)) for row in (r.get("result", {}).get("data_array") or [])]
    return {"row_count": len(rows), "rows": rows}


def words_clause(col, words):
    ws = [w for w in re.findall(r"[a-z0-9]+", words.lower()) if len(w) > 1][:4]
    if not ws:
        return "FALSE"
    return " AND ".join("lower(%s) LIKE '%%%s%%'" % (col, w) for w in ws)


def owns(key):
    key = re.sub(r"[^A-Za-z0-9]", "", key).upper()
    got = sql_rows("SELECT 1 FROM ust_databricks.ust_reporting.mart_rep_account_status "
                   "WHERE customer_key = '%s' AND owner_rep_name = '%s' LIMIT 1" % (key, REP))
    return key if got.get("rows") else None


def store_product_history(args):
    key = owns(args.get("customer_key", ""))
    if not key:
        return {"error": "Not one of this rep's accounts, or not an account key."}
    sql = """
    SELECT d.item_no, d.item_label, d.item_category_name,
           count(DISTINCT p.posting_date) AS times_bought, min(p.posting_date) AS first_bought,
           max(p.posting_date) AS last_bought, round(sum(p.sold_quantity), 1) AS total_qty,
           d.unit_price AS price_now, d.is_in_stock, d.shippable_qty
    FROM ust_databricks.ust_dims.dim_items d
    LEFT JOIN ust_databricks.ust_intermediate.int_customer_item_purchases p
      ON p.item_no = d.item_no AND p.customer_key = '%s' AND p.sold_quantity > 0
    WHERE (%s) AND d.is_sellable AND NOT d.is_blocked
    GROUP BY ALL ORDER BY times_bought DESC, d.is_in_stock DESC LIMIT 8""" % (
        key, words_clause("d.item_label", args.get("product_words", "")))
    out = sql_rows(sql)
    out["note"] = "times_bought 0 means this store has never bought that item from us."
    return out


def seasonal_demand(args):
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", args.get("event_date", ""))
    if not m:
        return {"error": "event_date must be YYYY-MM-DD"}
    mo, dy = int(m.group(2)), int(m.group(3))
    stores = ""
    sw = (args.get("store_name_word") or "").strip()
    if sw:
        stores = ("JOIN (SELECT DISTINCT customer_key FROM ust_databricks.ust_reporting.mart_rep_account_status "
                  "WHERE %s) c ON c.customer_key = p.customer_key" % words_clause("customer_name", sw))
    sql = """
    SELECT d.item_label, d.item_category_name,
      round(sum(CASE WHEN datediff(make_date(year(p.posting_date), %d, %d), p.posting_date) BETWEEN 0 AND 27
                THEN p.sold_quantity ELSE 0 END) / 3, 1) AS qty_4wk_before_per_year,
      round(sum(p.sold_quantity) / (3 * 13.0), 1) AS qty_avg_4wk,
      count(DISTINCT p.customer_key) AS stores_buying,
      d.is_in_stock, d.unit_price
    FROM ust_databricks.ust_intermediate.int_customer_item_purchases p
    JOIN ust_databricks.ust_dims.dim_items d ON d.item_no = p.item_no
    %s
    WHERE p.posting_date >= '2023-01-01' AND p.posting_date < '2026-01-01' AND p.sold_quantity > 0
      AND (%s)
    GROUP BY ALL HAVING qty_avg_4wk > 0
    ORDER BY qty_4wk_before_per_year DESC LIMIT 8""" % (
        mo, dy, stores, words_clause("d.item_label", args.get("product_words", "")))
    out = sql_rows(sql)
    for r in out.get("rows", []):
        try:
            r["lift_vs_normal"] = round(float(r["qty_4wk_before_per_year"]) / float(r["qty_avg_4wk"]), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    out["note"] = ("2023-2025 sales. lift_vs_normal above 1 means it sells more in the four weeks "
                   "before that date than in a usual four weeks.")
    return out


NEW_TOOLS = [
    {"type": "function", "function": {
        "name": "store_product_history",
        "description": ("Whether ONE store has bought products matching some words, and how often and "
                        "when -- including products it has never bought (times_bought 0), with our price "
                        "and stock now. Use to check a specific idea: 'has this store ever bought sticky "
                        "rice?' Needs the account key."),
        "parameters": {"type": "object", "required": ["customer_key", "product_words"], "properties": {
            "customer_key": {"type": "string", "description": "Account key, e.g. HMO032"},
            "product_words": {"type": "string", "description": "1-3 words in the product name, e.g. sweet rice"}}}}},
    {"type": "function", "function": {
        "name": "seasonal_demand",
        "description": ("How much of a product our stores buy in the four weeks before a date (a holiday "
                        "or festival), compared with a usual four weeks, over 2023-2025. Optionally only "
                        "stores whose name has a word in it (e.g. Hmong). Use to check with real sales "
                        "whether a product actually sells for an occasion before you recommend it."),
        "parameters": {"type": "object", "required": ["product_words", "event_date"], "properties": {
            "product_words": {"type": "string", "description": "1-3 words in the product name"},
            "event_date": {"type": "string", "description": "The occasion's date, YYYY-MM-DD"},
            "store_name_word": {"type": "string", "description": "Optional: only stores whose name has this word"}}}}},
]


def system_prompt():
    text = open(os.path.join(REPO, "agent_prompt.md"), encoding="utf-8").read().replace("{{rep_name}}", REP)
    # Take out what steers toward the pitch list and the lapsed-item example.
    text = re.sub(r"When a rep asks what to pitch, sell or bring up with a store, call\nlookup_pitch_list.*?trust\.\n",
                  "", text, flags=re.S)
    text = text.replace("Hmong New Year three weeks out is a reason to pitch the sweet rice\n"
                        "flour a Hmong store has stopped buying.", "a festival coming up is a reason to pitch.")
    text += ("\n\n## Working out what to pitch\n\nYou have several ways to look into a store, and how you "
             "combine them is your call. lookup_pitch_list is a quick shortlist the system ranked from the "
             "store's history; it knows nothing about festivals. To build a pitch around an occasion, work "
             "out from your own knowledge what that community actually buys for it, then check: does our "
             "catalog carry it (search_items), has this store bought it (store_product_history), and does it "
             "really sell before that date (seasonal_demand). Recommend only products a lookup returned, and "
             "say what each recommendation rests on.\n")
    return text


def run(turns):
    tools = [s.openai_tool_schema(sp) for sp in s.tool_specs() if not (os.environ.get("NO_PITCH") and sp["name"] == "lookup_pitch_list")] + [s.WEB_SEARCH_TOOL] + NEW_TOOLS
    history = [{"role": "system", "content": system_prompt()}]
    for turn in turns:
        print("\n  you   : %s" % turn, flush=True)
        history.append({"role": "user", "content": turn})
        t0 = time.time()
        answer = None
        for rnd in range(MAX_ROUNDS + 1):
            offer = tools if rnd < MAX_ROUNDS else []
            msg = ((s.call_upstream_llm(history, MODEL, 1024, offer).get("choices") or [{}])[0]).get("message") or {}
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = (msg.get("content") or "").strip()
                break
            history.append(msg)
            for c in calls:
                fn = c.get("function") or {}
                try:
                    a = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    a = {}
                name = fn.get("name")
                if name not in {t["function"]["name"] for t in tools}:
                    res = json.dumps({"error": "There is no tool called %s. Use the tools you have." % name})
                elif name == "store_product_history":
                    res = json.dumps(store_product_history(a))
                elif name == "seasonal_demand":
                    res = json.dumps(seasonal_demand(a))
                elif name == "web_search":
                    res = s.web_search(a.get("value", ""), history, REP)
                else:
                    res = s.run_tool_call(name, a.get("value", ""), REP)
                try:
                    p = json.loads(res); outcome = p.get("error") or "%d row(s)" % len(p.get("rows") or p.get("results") or [])
                except ValueError:
                    outcome = "?"
                print("    TOOL %s(%s) -> %s" % (name, json.dumps(a), str(outcome)[:80]), flush=True)
                history.append({"role": "tool", "tool_call_id": c.get("id"), "content": res})
        history.append({"role": "assistant", "content": answer or ""})
        print("  agent (%.0fs, %d rounds): %s" % (time.time() - t0, rnd + 1, answer), flush=True)


if __name__ == "__main__":
    run(sys.argv[1:])
