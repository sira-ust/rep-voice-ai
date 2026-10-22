/**
 * Charts, tables and maps shown under a reply.
 *
 * The server sends them ready-made: the model picked the kind and the columns,
 * and the server filled the values in from the lookup's own rows (see
 * build_visual in server.py). So everything here is drawing, never deciding.
 *
 * Built with DOM and SVG calls only -- every label is set as text, so nothing
 * in a customer or item name can put markup on the page. The map library is
 * the one dependency, loaded the first time a map is shown.
 */

const SVG_NS = "http://www.w3.org/2000/svg";
const SERIES = ["#2196F3", "#3366AD", "#1F7A4D", "#A85A12", "#6B3FA0", "#0F6E78", "#A43A5A", "#4A5A6B"];
const STATUS = {
  lapsing: { colour: "#D6584F", label: "Lapsing" },
  due: { colour: "#E39A2D", label: "Due" },
  "on rhythm": { colour: "#1F7A4D", label: "On rhythm" },
  "former regular": { colour: "#6B3FA0", label: "Former regular" },
  dormant: { colour: "#6B3FA0", label: "Dormant" },
  occasional: { colour: "#4A5A6B", label: "Occasional" },
};
const OTHER_STATUS = { colour: "#2196F3", label: "Other" };

const LEAFLET = "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/";
const LEAFLET_JS_SRI = "sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH";
const LEAFLET_CSS_SRI = "sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H";

export function formatValue(value, format) {
  if (value === null || value === undefined || value === "") return "–";
  if (typeof value !== "number") return String(value);
  switch (format) {
    case "money":
      return (value < 0 ? "-$" : "$") + Math.round(Math.abs(value)).toLocaleString("en-US");
    case "percent": {
      const pct = value * 100;
      return (Math.abs(pct) < 10 ? pct.toFixed(1) : Math.round(pct).toString()) + "%";
    }
    case "times":
      return value.toFixed(1) + "×";
    default:
      return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
}

function node(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

function svg(tag, attrs = {}) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

function humanise(column) {
  const text = String(column).replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function legend(items) {
  const list = node("ul", "visual-legend");
  for (const { colour, label, value } of items) {
    const li = node("li");
    const swatch = node("span", "visual-swatch");
    swatch.style.background = colour;
    li.append(swatch, node("span", "visual-legend-label", label));
    if (value) li.append(node("span", "visual-legend-value", value));
    list.append(li);
  }
  return list;
}

// ---------------------------------------------------------------- bar

function bar(v) {
  const box = node("div", "vbar");
  const numbers = v.series.flatMap((s) => s.values).filter((x) => typeof x === "number");
  // Against a target, 100% is the line that matters: always in view.
  const againstTarget = v.series.some((s) => s.target);
  let max = Math.max(0, ...numbers);
  if (againstTarget) max = Math.max(max, 1);
  if (!max) return null;
  if (v.series.length > 1) {
    box.append(legend(v.series.map((s, k) => ({ colour: SERIES[k % SERIES.length], label: s.name }))));
  }
  v.labels.forEach((label, i) => {
    const row = node("div", "vbar-row");
    row.append(node("div", "vbar-label", label));
    v.series.forEach((s, k) => {
      const value = s.values[i];
      const line = node("div", "vbar-line");
      const track = node("div", "vbar-track");
      const fill = node("div", "vbar-fill");
      fill.style.width = `${((Math.max(0, value || 0) / max) * 100).toFixed(1)}%`;
      fill.style.background = SERIES[k % SERIES.length];
      track.append(fill);
      if (s.target) {
        const guide = node("div", "vbar-guide");
        guide.style.left = `${((1 / max) * 100).toFixed(1)}%`;
        guide.title = "100%";
        track.append(guide);
      }
      line.append(track, node("div", "vbar-value", formatValue(value, s.format)));
      row.append(line);
    });
    box.append(row);
  });
  return box;
}

// ---------------------------------------------------------------- pie

function pie(v) {
  const s = v.series[0];
  const labels = [...v.labels];
  const values = s.values.map((x) => Math.max(0, x || 0));
  let total = values.reduce((a, b) => a + b, 0);
  // Shares that do not fill the whole leave the rest visible as the rest,
  // rather than being stretched into a whole they are not.
  if (s.format === "percent" && total > 0 && total < 0.999) {
    labels.push("Everything else");
    values.push(1 - total);
    total = 1;
  }
  if (!total) return null;
  // The leftover slice is context, not a contender: grey, so the named ones stand out.
  const colourOf = (i) => (/^(all other|everything else)/i.test(labels[i]) ? "#C7D3E3" : SERIES[i % SERIES.length]);
  const shareOf = (i) => `${Math.round((values[i] / total) * 100)}%`;

  const box = node("div", "vpie");
  const chart = svg("svg", { viewBox: "-1.05 -1.05 2.1 2.1", class: "vpie-chart", role: "img" });
  let angle = -Math.PI / 2;
  values.forEach((value, i) => {
    if (!value) return;
    const colour = colourOf(i);
    const share = value / total;
    if (share > 0.9999) {
      chart.append(svg("circle", { cx: 0, cy: 0, r: 1, fill: colour }));
      return;
    }
    const end = angle + share * 2 * Math.PI;
    const d = `M 0 0 L ${Math.cos(angle)} ${Math.sin(angle)} ` +
      `A 1 1 0 ${share > 0.5 ? 1 : 0} 1 ${Math.cos(end)} ${Math.sin(end)} Z`;
    chart.append(svg("path", { d, fill: colour, stroke: "#fff", "stroke-width": 0.02 }));
    angle = end;
  });
  chart.append(svg("circle", { cx: 0, cy: 0, r: 0.56, fill: "#fff" }));
  box.append(chart, legend(labels.map((label, i) => {
    const amount = i < s.values.length ? formatValue(s.values[i], s.format) : formatValue(values[i], s.format);
    // A percentage's own figure already is its share; anything else gets both.
    return { colour: colourOf(i), label, value: s.format === "percent" ? amount : `${amount} · ${shareOf(i)}` };
  })));
  return box;
}

// ---------------------------------------------------------------- line

function line(v) {
  const W = 320, H = 170, L = 52, R = 10, T = 12, B = 26;
  const numbers = v.series.flatMap((s) => s.values).filter((x) => typeof x === "number");
  if (!numbers.length) return null;
  const lo = Math.min(0, ...numbers), hi = Math.max(...numbers) || 1;
  const n = v.labels.length;
  const x = (i) => L + (n === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (n - 1));
  const y = (val) => T + (H - T - B) * (1 - (val - lo) / (hi - lo || 1));
  const fmt = v.series[0].format;

  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "vline-chart", role: "img" });
  for (const tick of [lo, (lo + hi) / 2, hi]) {
    chart.append(svg("line", { x1: L, x2: W - R, y1: y(tick), y2: y(tick), class: "vline-grid" }));
    const t = svg("text", { x: L - 6, y: y(tick) + 3, class: "vline-axis", "text-anchor": "end" });
    t.textContent = formatValue(tick, fmt);
    chart.append(t);
  }
  const ticks = n <= 3 ? [...Array(n).keys()] : [0, Math.floor((n - 1) / 2), n - 1];
  for (const i of ticks) {
    const t = svg("text", { x: x(i), y: H - 8, class: "vline-axis", "text-anchor": "middle" });
    t.textContent = v.labels[i];
    chart.append(t);
  }
  v.series.forEach((s, k) => {
    const colour = SERIES[k % SERIES.length];
    const pts = s.values.map((val, i) => (typeof val === "number" ? `${x(i)},${y(val)}` : null)).filter(Boolean);
    chart.append(svg("polyline", { points: pts.join(" "), fill: "none", stroke: colour, "stroke-width": 2 }));
    s.values.forEach((val, i) => {
      if (typeof val !== "number") return;
      const dot = svg("circle", { cx: x(i), cy: y(val), r: 3, fill: colour });
      const tip = svg("title");
      tip.textContent = `${v.labels[i]}: ${formatValue(val, s.format)}`;
      dot.append(tip);
      chart.append(dot);
    });
  });
  const box = node("div", "vline");
  if (v.series.length > 1) {
    box.append(legend(v.series.map((s, k) => ({ colour: SERIES[k % SERIES.length], label: s.name }))));
  }
  box.append(chart);
  return box;
}

// ---------------------------------------------------------------- table

function table(v) {
  const wrap = node("div", "vtable-wrap");
  const t = node("table", "vtable");
  const head = node("tr");
  v.columns.forEach((col, i) => {
    const numeric = v.rows.some((r) => typeof r[i] === "number");
    head.append(node("th", numeric ? "num" : null, humanise(col.name)));
  });
  t.append(node("thead"));
  t.tHead.append(head);
  const body = node("tbody");
  for (const row of v.rows) {
    const tr = node("tr");
    row.forEach((cell, i) => {
      const td = node("td", typeof cell === "number" ? "num" : null, formatValue(cell, v.columns[i].format));
      tr.append(td);
    });
    body.append(tr);
  }
  t.append(body);
  wrap.append(t);
  return wrap;
}

// ---------------------------------------------------------------- map

let leafletLoading = null;

function loadLeaflet() {
  if (window.L) return Promise.resolve(window.L);
  if (!leafletLoading) {
    leafletLoading = new Promise((resolve, reject) => {
      const css = document.createElement("link");
      css.rel = "stylesheet";
      css.href = LEAFLET + "leaflet.css";
      css.integrity = LEAFLET_CSS_SRI;
      css.crossOrigin = "anonymous";
      document.head.append(css);
      const script = document.createElement("script");
      script.src = LEAFLET + "leaflet.js";
      script.integrity = LEAFLET_JS_SRI;
      script.crossOrigin = "anonymous";
      script.onload = () => resolve(window.L);
      script.onerror = () => { leafletLoading = null; reject(new Error("map library did not load")); };
      document.head.append(script);
    });
  }
  return leafletLoading;
}

function map(v) {
  const wrap = node("div", "vmap-wrap");
  const box = node("div", "vmap");
  const seen = new Map();
  for (const p of v.points) {
    const s = STATUS[p.status] || OTHER_STATUS;
    seen.set(s.label, s.colour);
  }
  wrap.append(box);
  if (v.route) {
    const r = v.route;
    let note = `${r.from.label} to ${r.to.label}, as the crow flies`;
    if (r.path && r.miles) {
      const h = Math.floor(r.minutes / 60), m = r.minutes % 60;
      note = `${r.from.label} to ${r.to.label}: about ${r.miles} miles by road, ` +
        `${h ? `${h} h ` : ""}${m} min without traffic`;
    }
    wrap.append(node("p", "visual-note", note));
  }
  wrap.append(legend([...seen].map(([label, colour]) => ({ colour, label }))));

  loadLeaflet().then((L) => {
    const m = L.map(box, { scrollWheelZoom: false });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: "&copy; OpenStreetMap contributors",
      // The page sends no referrer anywhere (Referrer-Policy in server.py),
      // and OpenStreetMap refuses tiles that arrive without one -- "Access
      // blocked" drawn over the map. Tiles alone send our site's origin:
      // never the path, and nothing about the rep or the stores.
      referrerPolicy: "strict-origin",
    }).addTo(m);
    const bounds = [];
    if (v.route) {
      const a = [v.route.from.lat, v.route.from.lon];
      const b = [v.route.to.lat, v.route.to.lon];
      if (v.route.path && v.route.path.length > 1) {
        // The road itself, from OpenRouteService via the server.
        L.polyline(v.route.path, { color: "#3366AD", weight: 4, opacity: 0.85 }).addTo(m);
        bounds.push(...v.route.path);
      } else {
        L.polyline([a, b], { color: "#3366AD", weight: 3, dashArray: "6 6", opacity: 0.8 }).addTo(m);
      }
      for (const [end, at] of [[v.route.from, a], [v.route.to, b]]) {
        L.circleMarker(at, { radius: 6, color: "#3366AD", weight: 3, fillColor: "#fff", fillOpacity: 1 })
          .bindTooltip(node("span", null, end.label), { permanent: true, direction: "top", className: "vmap-end" })
          .addTo(m);
      }
      bounds.push(a, b);
    }
    for (const p of v.points) {
      const s = STATUS[p.status] || OTHER_STATUS;
      const popup = node("div", "vmap-popup");
      popup.append(node("strong", null, p.label));
      if (p.detail) popup.append(node("div", null, p.detail));
      L.circleMarker([p.lat, p.lon], { radius: 8, color: "#fff", weight: 2, fillColor: s.colour, fillOpacity: 1 })
        .bindTooltip(node("span", null, p.label))
        .bindPopup(popup, { maxWidth: 260 })
        .addTo(m);
      bounds.push([p.lat, p.lon]);
    }
    const fit = () => { m.invalidateSize(); m.fitBounds(bounds, { padding: [28, 28], maxZoom: 13 }); };
    fit();
    // Laid out only once the card is on screen; fit again when it is.
    requestAnimationFrame(fit);
  }).catch(() => {
    box.replaceChildren(node("p", "visual-note", "The map could not load."));
  });
  return wrap;
}

// ---------------------------------------------------------------- visit plan
//
// Every section of a store's visit plan, built by the server from the night's
// rows. The voice gives a summary; this is the whole of it, so nothing the
// agent had no time to say is lost. The agent's lead picks are marked.

function planItems(items) {
  const list = node("ul", "vplan-items");
  for (const it of items || []) {
    const li = node("li", it.lead ? "lead" : it.warn ? "warn" : null);
    const head = node("div", "vplan-item");
    head.append(node("strong", null, it.label || ""));
    if (it.lead) head.append(node("span", "vplan-tag", "Lead with"));
    if (it.warn) head.append(node("span", "vplan-tag vplan-tag-warn", "Out of stock"));
    li.append(head);
    // The detail opens with the item's name, already shown in bold.
    const detail = (it.detail || "").replace(/^[^:]*:\s*/, "");
    if (detail) li.append(node("div", "vplan-detail", detail));
    list.append(li);
  }
  return list;
}

function planSection(sec) {
  const part = node("section", "vplan-section");
  part.append(node("h4", "vplan-head", sec.title || ""));
  if (Array.isArray(sec.groups)) {
    for (const group of sec.groups) {
      if (group.title) part.append(node("p", "vplan-dish", group.title));
      part.append(planItems(group.items));
    }
  } else {
    part.append(planItems(sec.items));
  }
  return part;
}

// The angles the agent chose for this store come first; every other idea is
// one tap away under "More ideas" -- there, not in the way.
function plan(v) {
  const featured = Array.isArray(v.featured) ? v.featured : [];
  const more = Array.isArray(v.sections) ? v.sections : [];
  if (!featured.length && !more.length) return null;
  const box = node("div", "vplan");
  if (v.note) box.append(node("p", "vplan-note", v.note));
  if (featured.length) {
    const top = node("div", "vplan-featured");
    top.append(node("p", "vplan-kicker", "For this visit"));
    featured.forEach((sec) => top.append(planSection(sec)));
    box.append(top);
  }
  if (more.length) {
    const fold = node("details", "vplan-more");
    if (!featured.length) fold.open = true;
    fold.append(node("summary", null, `More ideas (${more.length})`));
    more.forEach((sec) => fold.append(planSection(sec)));
    box.append(fold);
  }
  return box;
}

const DRAW = { bar, pie, line, table, map, plan };

/** A finished card for `visual`, or null if there is nothing to draw. */
export function renderVisual(visual) {
  const draw = visual && DRAW[visual.type];
  if (!draw) return null;
  const body = draw(visual);
  if (!body) return null;
  const card = node("figure", "msg msg-visual");
  if (visual.title) card.append(node("figcaption", "visual-title", visual.title));
  card.append(body);
  card.visual = visual;     // kept for copying the conversation as text
  return card;
}

/** A visual as plain text, for pasting into an email or a chat. */
export function visualToText(v) {
  if (!v) return "";
  const lines = v.title ? [v.title] : [];
  const items = (list, indent) => (list || []).forEach((it) => {
    const tags = [it.lead ? "LEAD WITH" : "", it.warn ? "OUT OF STOCK" : ""].filter(Boolean).join(", ");
    const detail = (it.detail || "").replace(/^[^:]*:\s*/, "");
    lines.push(`${indent}- ${it.label}${tags ? ` [${tags}]` : ""}${detail ? `: ${detail}` : ""}`);
  });
  if (v.type === "plan") {
    if (v.note) lines.push(v.note);
    const section = (sec) => {
      lines.push("", `  ${sec.title}`);
      if (Array.isArray(sec.groups)) {
        for (const g of sec.groups) {
          if (g.title) lines.push(`    ${g.title}`);
          items(g.items, "      ");
        }
      } else {
        items(sec.items, "    ");
      }
    };
    if ((v.featured || []).length) {
      lines.push("", "FOR THIS VISIT");
      v.featured.forEach(section);
    }
    if ((v.sections || []).length) {
      lines.push("", "MORE IDEAS");
      v.sections.forEach(section);
    }
  } else if (v.type === "table") {
    lines.push("  " + v.columns.map((c) => humanise(c.name)).join(" | "));
    for (const row of v.rows) lines.push("  " + row.map((cell, i) => formatValue(cell, v.columns[i].format)).join(" | "));
  } else if (v.type === "map") {
    for (const p of v.points || []) lines.push(`  - ${p.label}${p.detail ? `: ${p.detail}` : ""}`);
  } else if (Array.isArray(v.labels) && Array.isArray(v.series)) {
    v.labels.forEach((label, i) => {
      const values = v.series.map((s) => `${v.series.length > 1 ? `${s.name}: ` : ""}${formatValue(s.values[i], s.format)}`);
      lines.push(`  - ${label}: ${values.join(", ")}`);
    });
  }
  return lines.join("\n");
}
