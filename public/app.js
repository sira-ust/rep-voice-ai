// ElevenLabs Agent web UI.
//
// The API key lives only on the Python server (.env). This file asks that server
// for a short-lived conversation token / signed URL and hands it to the SDK.

import { Conversation } from "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm";

const $ = (id) => document.getElementById(id);

const el = {
  brandName: $("brandName"),
  statusDot: $("statusDot"),
  statusText: $("statusText"),
  settingsBtn: $("settingsBtn"),
  settingsPanel: $("settingsPanel"),
  errorBanner: $("errorBanner"),
  errorText: $("errorText"),
  errorClose: $("errorClose"),
  agentSelect: $("agentSelect"),
  agentIdInput: $("agentIdInput"),
  connSelect: $("connSelect"),
  keyNote: $("keyNote"),
  llmNote: $("llmNote"),
  orb: $("orb"),
  modeLabel: $("modeLabel"),
  callBtn: $("callBtn"),
  callBtnLabel: $("callBtnLabel"),
  muteBtn: $("muteBtn"),
  volumeSlider: $("volumeSlider"),
  meterFill: $("meterFill"),
  transcript: $("transcript"),
  guide: $("guide"),
  guideGroups: $("guideGroups"),
  guideFoot: $("guideFoot"),
  composer: $("composer"),
  composerInput: $("composerInput"),
  composerSend: $("composerSend"),
  debugToggle: $("debugToggle"),
  debugLog: $("debugLog"),
  debugCount: $("debugCount"),
  debugStat: $("debugStat"),
  debugDetail: $("debugDetail"),
  debugCopy: $("debugCopy"),
  debugClear: $("debugClear"),
};

const state = {
  conversation: null,
  status: "disconnected", // disconnected | connecting | connected | disconnecting
  mode: "listening",
  muted: false,
  meterTimer: null,
  config: { hasApiKey: false, agentId: "" },
  // Live agent reply being streamed in, so an aborted turn still shows text.
  streaming: { id: null, node: null, text: "" },
  // Counters surfaced in the debug header -- the fastest way to tell whether
  // the server's JSON events are reaching the browser at all.
  counts: { incoming: 0, transcripts: 0, agentReplies: 0, audioSent: 0 },
  startedAt: 0,
  // False until a key is loaded and an agent is selected, so the idle pill can
  // distinguish "Ready" from "Not configured".
  ready: false,
};

// ---------------------------------------------------------------- debug log

const LOG_CAP = 600;
const logLines = [];

// Message text and tool payloads -- the rows coming back from Databricks among
// them -- are logged only when someone asks for them. Off by default because
// the log has a Copy button and a 600-line buffer, so the quiet default is for
// customer data not to ride along into a ticket or a screen-share.
//
// Event types, counts, timings, connection states and errors are never hidden:
// that is what actually diagnoses a stall or a dead microphone, and none of it
// carries content.
const DETAIL_KEY = "convai.logDetail";
let logDetail = false;
try {
  logDetail = localStorage.getItem(DETAIL_KEY) === "1";
} catch {
  // Private windows and locked-down profiles throw on access; default stands.
}

function log(channel, label, payload, redacted) {
  const elapsed = state.startedAt ? ((Date.now() - state.startedAt) / 1000).toFixed(2) : "0.00";
  let detail = "";
  if (redacted) {
    detail = "(hidden)";
  } else if (payload !== undefined) {
    try {
      detail = typeof payload === "string" ? payload : JSON.stringify(payload);
    } catch {
      detail = String(payload);
    }
    if (detail.length > 400) detail = detail.slice(0, 400) + "…";
  }

  const plain = `[${elapsed}s] ${channel.toUpperCase().padEnd(3)} ${label}${detail ? " " + detail : ""}`;
  logLines.push(plain);
  if (logLines.length > LOG_CAP) logLines.shift();

  const line = document.createElement("div");
  const time = document.createElement("span");
  time.className = "t";
  time.textContent = `[${elapsed}s] `;
  const body = document.createElement("span");
  body.className = channel;
  body.textContent = `${label}${detail ? " " + detail : ""}`;
  line.append(time, body);

  el.debugLog.appendChild(line);
  while (el.debugLog.childElementCount > LOG_CAP) el.debugLog.firstElementChild.remove();
  el.debugLog.scrollTop = el.debugLog.scrollHeight;

  el.debugCount.textContent = String(logLines.length);
  console.log("[convai]", plain);
}

/** log() for conversation content and tool payloads. Gated by the toggle. */
function logDetailed(channel, label, payload) {
  log(channel, label, logDetail ? payload : undefined, !logDetail);
}

function renderDebugStats() {
  const c = state.counts;
  el.debugStat.textContent =
    `mic-out ${c.audioSent} · in ${c.incoming} · transcripts ${c.transcripts} · replies ${c.agentReplies}`;
}

// ---------------------------------------------------------------- utilities

async function getJSON(url) {
  const res = await fetch(url, {
    headers: { Accept: "application/json" },
    credentials: "same-origin",
  });
  let body = {};
  try {
    body = await res.json();
  } catch {
    /* non-JSON error page */
  }
  // A session can expire mid-call. Send the user back to sign in rather than
  // showing them a bare "Not signed in" they cannot act on.
  if (res.status === 401 && body.login) {
    window.location.href = body.login;
    throw new Error("Session expired — signing in again.");
  }
  if (!res.ok) throw new Error(body.error || `${res.status} ${res.statusText}`);
  return body;
}

function showError(message) {
  // The SDK's onError can fire with an empty string, which previously unhid an
  // empty red banner. Show the banner only when there is something to say.
  let text = "";
  if (typeof message === "string") {
    text = message;
  } else if (message !== undefined && message !== null) {
    try {
      text = JSON.stringify(message);
    } catch {
      text = String(message);
    }
  }
  text = (text || "").trim();

  if (!text) {
    log("err", "empty error suppressed", message === undefined ? "undefined" : typeof message);
    return;
  }
  el.errorText.textContent = text;
  el.errorBanner.hidden = false;
}

function clearError() {
  el.errorBanner.hidden = true;
}

/** Append a transcript bubble and return the node so it can be updated later. */
function addMessage(text, kind) {
  // A typed message is echoed locally; if the server also relays it back as a
  // user transcript, drop the duplicate rather than showing it twice.
  const last = el.transcript.lastElementChild;
  if (kind === "user" && last && last.classList.contains("msg-user") && last.textContent === text) {
    return last;
  }

  if (el.guide && el.guide.isConnected) el.guide.remove();

  const node = document.createElement("div");
  node.className = `msg msg-${kind}`;
  node.textContent = text;
  el.transcript.appendChild(node);
  el.transcript.scrollTop = el.transcript.scrollHeight;
  return node;
}

// ---------------------------------------------------------------- rendering

const STATUS_LABEL = {
  disconnected: "Ready",
  connecting: "Connecting…",
  connected: "Connected",
  disconnecting: "Ending…",
};

function render() {
  const { status, mode } = state;

  el.statusDot.className = `dot ${status === "disconnected" ? "" : status}`;
  el.statusText.textContent =
    status === "disconnected" && !state.ready
      ? "Not configured"
      : STATUS_LABEL[status] || status;

  const live = status === "connected";
  const busy = status === "connecting" || status === "disconnecting";
  el.callBtn.classList.toggle("is-live", live);
  el.callBtn.disabled = busy;
  el.callBtnLabel.textContent = live
    ? "End conversation"
    : busy
      ? STATUS_LABEL[status]
      : "Start conversation";

  el.orb.className = "orb";
  if (busy) {
    el.orb.classList.add("connecting");
    el.modeLabel.textContent = STATUS_LABEL[status];
  } else if (live) {
    el.orb.classList.add(mode === "speaking" ? "speaking" : "listening");
    el.modeLabel.textContent =
      mode === "speaking" ? "Agent is speaking" : state.muted ? "Microphone muted" : "Listening…";
  } else {
    el.modeLabel.textContent = "Tap to start talking";
  }

  el.muteBtn.disabled = !live;
  el.muteBtn.classList.toggle("active", state.muted);
  el.muteBtn.title = state.muted ? "Unmute microphone" : "Mute microphone";
  el.composerInput.disabled = !live;
  el.composerSend.disabled = !live;
  el.agentSelect.disabled = live;
  el.agentIdInput.disabled = live;
  el.connSelect.disabled = live;
}

function setStatus(status) {
  if (state.status === status) return;
  state.status = status;
  render();
}

// ---------------------------------------------------------------- input meter

function startMeter() {
  stopMeter();
  let ticks = 0;
  let peak = 0;
  state.meterTimer = setInterval(() => {
    if (!state.conversation) return;
    try {
      const input = Number(state.conversation.getInputVolume()) || 0;
      const level =
        state.mode === "speaking"
          ? Number(state.conversation.getOutputVolume()) || 0
          : input;
      el.meterFill.style.width = `${Math.min(100, Math.max(0, level * 100))}%`;
      // Feeds the ring that expands around the orb, so a working mic is visible.
      el.orb.style.setProperty("--level", Math.min(1, input * 3).toFixed(3));

      // Report the loudest mic level per second. If this stays at 0.00 while
      // you are talking, the SDK is not capturing audio at all -- which is a
      // different failure from capturing but not transmitting.
      peak = Math.max(peak, input);
      if (++ticks >= 10) {
        log("cb", "mic level peak", peak.toFixed(3));
        ticks = 0;
        peak = 0;
      }
    } catch (err) {
      if (++ticks >= 10) {
        log("err", "level read failed", err.message);
        ticks = 0;
      }
    }
  }, 100);
}

function stopMeter() {
  if (state.meterTimer) clearInterval(state.meterTimer);
  state.meterTimer = null;
  el.meterFill.style.width = "0%";
  el.orb.style.setProperty("--level", "0");
}

// ---------------------------------------------------------------- session

function currentAgentId() {
  return el.agentIdInput.value.trim();
}

/** Ask our own server for credentials. The API key never leaves the server. */
async function getAuth(agentId, connectionType) {
  const query = agentId ? `?agent_id=${encodeURIComponent(agentId)}` : "";
  if (connectionType === "websocket") {
    const { signedUrl } = await getJSON(`/api/signed-url${query}`);
    return { signedUrl };
  }
  const { token } = await getJSON(`/api/conversation-token${query}`);
  return { conversationToken: token };
}

/** DisconnectionDetails -> a short human phrase. */
function describeDisconnect(details) {
  if (!details) return "closed";
  if (details.reason === "user") return "you hung up";
  if (details.reason === "agent") return "agent hung up";
  if (details.reason === "error") return details.message || "error";
  return details.reason;
}

/** Render streamed agent text so a turn that gets cut off still shows something. */
function handleResponsePart(part) {
  const { text, type, response_id: id } = part || {};
  if (type === "start") {
    state.streaming = { id, node: null, text: "" };
    return;
  }
  if (type === "delta") {
    state.streaming.text += text || "";
    if (!state.streaming.node) {
      state.streaming.node = addMessage(state.streaming.text, "ai");
    } else {
      state.streaming.node.textContent = state.streaming.text;
    }
    el.transcript.scrollTop = el.transcript.scrollHeight;
  }
}

async function startConversation() {
  clearError();
  const agentId = currentAgentId();
  const connectionType = el.connSelect.value;

  if (!agentId) {
    showError("No agent ID. Open settings and pick an agent, or set ELEVENLABS_AGENT_ID in .env.");
    el.settingsPanel.hidden = false;
    return;
  }

  state.startedAt = Date.now();
  state.counts = { incoming: 0, transcripts: 0, agentReplies: 0, audioSent: 0 };
  renderDebugStats();
  log("out", "startConversation", { agentId, connectionType });

  setStatus("connecting");

  // Do NOT open a capture stream here. The SDK opens its own, and acquiring
  // then releasing the device first can hand back a silent stream on some
  // Windows audio drivers. Just read the permission state, which is passive.
  try {
    if (navigator.permissions && navigator.permissions.query) {
      const perm = await navigator.permissions.query({ name: "microphone" });
      log("out", "mic permission", perm.state);
      if (perm.state === "denied") {
        setStatus("disconnected");
        showError("Microphone access is blocked for this site. Allow it in the address bar, then retry.");
        return;
      }
    }
  } catch {
    /* Firefox and some builds do not expose the microphone permission name */
  }

  try {
    const auth = await getAuth(agentId, connectionType);
    log("out", "auth ok", connectionType === "websocket" ? "signedUrl" : "conversationToken");

    state.conversation = await Conversation.startSession({
      ...auth,
      connectionType,
      textOnly: false,

      onConnect: ({ conversationId }) => {
        log("cb", "onConnect", conversationId);
        setStatus("connected");
        addMessage(`Connected · ${conversationId}`, "system");
        startMeter();
        applyVolume();
      },

      onDisconnect: (details) => {
        log("cb", "onDisconnect", details);
        addMessage(`Conversation ended (${describeDisconnect(details)})`, "system");
        if (details && details.reason === "error" && details.message) showError(details.message);
        teardown();
      },

      onMessage: ({ message, role, source }) => {
        logDetailed("cb", `onMessage ${role || source || "?"}`, message);
        if (!message) return;
        const speaker = role || source;
        if (speaker === "user") {
          state.counts.transcripts += 1;
          addMessage(message, "user");
        } else {
          state.counts.agentReplies += 1;
          // Replace the streamed preview rather than adding a second bubble.
          if (state.streaming.node) {
            state.streaming.node.textContent = message;
            state.streaming = { id: null, node: null, text: "" };
          } else {
            addMessage(message, "ai");
          }
        }
        renderDebugStats();
      },

      onModeChange: ({ mode }) => {
        log("cb", "onModeChange", mode);
        state.mode = mode;
        render();
      },

      onStatusChange: ({ status }) => {
        log("cb", "onStatusChange", status);
        setStatus(status);
      },

      onError: (message, context) => {
        log("err", "onError", { message, context });
        console.error("ElevenLabs error:", message, context);
        showError(typeof message === "string" ? message : JSON.stringify(message));
      },

      // ---- instrumentation only ----

      onOutgoingEvent: (event) => {
        const type = event && event.type;
        // The mic frame arrives as {user_audio_chunk: "<base64>"} with no type
        // field, so matching on event.type never fired: every frame fell
        // through to the generic branch and was logged whole, ~30 a second.
        // That buried the 600-line buffer in under 20 seconds -- which is why
        // a real error was never still on screen by the time anyone looked.
        if (type === "user_audio_chunk" || (event && event.user_audio_chunk !== undefined)) {
          state.counts.audioSent += 1;
          if (state.counts.audioSent === 1) log("out", "FIRST mic audio chunk sent");
          if (state.counts.audioSent % 50 === 0) {
            log("out", `mic audio chunks sent: ${state.counts.audioSent}`);
          }
          renderDebugStats();
          return;
        }
        logDetailed("out", type || "event", event);
      },

      onIncomingEvent: (event) => {
        state.counts.incoming += 1;
        renderDebugStats();
        // Audio frames are large and constant; log only their presence.
        const type = event && event.type;
        if (type === "audio") {
          if (state.counts.incoming % 25 === 0) log("in", "audio x25");
          return;
        }
        logDetailed("in", type || "event", event);
      },

      onAgentChatResponsePart: (part) => {
        logDetailed("in", "responsePart", part);
        handleResponsePart(part);
      },

      onVadScore: ({ vadScore }) => {
        if (vadScore > 0.5) log("in", "vad speech", vadScore.toFixed(2));
      },

      onInterruption: (info) => log("in", "interruption", info),
      onAgentToolRequest: (info) => logDetailed("in", "toolRequest", info),
      onAgentToolResponse: (info) => logDetailed("in", "toolResponse", info),
      onContextUsage: (info) => log("in", "contextUsage", info),
      onPing: (info) => log("in", "ping", info && info.ping_ms),
    });

    // onConnect can fire before startSession resolves, so set volume here too.
    applyVolume();
    log("out", "session started", state.conversation.getId?.());
  } catch (err) {
    setStatus("disconnected");
    log("err", "startSession failed", err.message || String(err));
    showError(err.message || String(err));
  }
}

async function endConversation() {
  log("out", "endConversation (user)");
  const conversation = state.conversation;
  state.conversation = null;
  try {
    if (conversation) await conversation.endSession();
  } catch {
    /* already closed */
  }
  teardown();
}

function teardown() {
  state.conversation = null;
  state.mode = "listening";
  state.muted = false;
  state.streaming = { id: null, node: null, text: "" };
  stopMeter();
  setStatus("disconnected");
}

function applyVolume() {
  if (!state.conversation) return;
  const volume = Number(el.volumeSlider.value) / 100;
  try {
    state.conversation.setVolume({ volume });
  } catch {
    /* not supported on this transport */
  }
}

// ---------------------------------------------------------------- wiring

el.callBtn.addEventListener("click", () => {
  if (state.status === "connected") endConversation();
  else if (state.status === "disconnected") startConversation();
});

el.muteBtn.addEventListener("click", () => {
  if (!state.conversation) return;
  state.muted = !state.muted;
  try {
    state.conversation.setMicMuted(state.muted);
    log("out", "setMicMuted", state.muted);
  } catch {
    showError("This SDK build does not support muting; end the call to stop the microphone.");
    state.muted = false;
  }
  render();
});

el.volumeSlider.addEventListener("input", applyVolume);

el.settingsBtn.addEventListener("click", () => {
  el.settingsPanel.hidden = !el.settingsPanel.hidden;
});

el.errorClose.addEventListener("click", clearError);

el.agentSelect.addEventListener("change", () => {
  if (el.agentSelect.value) {
    el.agentIdInput.value = el.agentSelect.value;
    refreshAgentInfo();
  }
});

el.agentIdInput.addEventListener("change", refreshAgentInfo);

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = el.composerInput.value.trim();
  if (!text || !state.conversation) return;
  logDetailed("out", "sendUserMessage", text);
  state.conversation.sendUserMessage(text);
  addMessage(text, "user");
  el.composerInput.value = "";
});

el.debugToggle.addEventListener("click", () => {
  const open = el.debugLog.hidden;
  el.debugLog.hidden = !open;
  el.debugToggle.setAttribute("aria-expanded", String(open));
});

function renderDetailToggle() {
  el.debugDetail.textContent = logDetail ? "Details on" : "Details off";
  el.debugDetail.setAttribute("aria-pressed", String(logDetail));
  el.debugDetail.title = logDetail
    ? "Message text and tool payloads are being logged. Clear before sharing."
    : "Include message text and tool payloads in the log";
}

el.debugDetail.addEventListener("click", () => {
  logDetail = !logDetail;
  try {
    localStorage.setItem(DETAIL_KEY, logDetail ? "1" : "0");
  } catch {
    // Not persisting is fine; the toggle still holds for this page.
  }
  renderDetailToggle();
  // Only affects what is logged from here on -- lines already in the buffer
  // were redacted when they were written and stay that way.
  log("out", logDetail
    ? "details ON: message text and tool payloads will be logged"
    : "details off: message text and tool payloads hidden");
});

renderDetailToggle();

el.debugCopy.addEventListener("click", async () => {
  const text = logLines.join("\n");
  try {
    await navigator.clipboard.writeText(text);
    el.debugCopy.textContent = "Copied";
  } catch {
    el.debugCopy.textContent = "Press Ctrl+C";
    const range = document.createRange();
    range.selectNodeContents(el.debugLog);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  setTimeout(() => (el.debugCopy.textContent = "Copy"), 1500);
});

el.debugClear.addEventListener("click", () => {
  logLines.length = 0;
  el.debugLog.textContent = "";
  el.debugCount.textContent = "0";
});

window.addEventListener("beforeunload", () => {
  if (state.conversation) state.conversation.endSession();
});

/** Render the "what can I ask" guide from the tools actually wired up. */
async function loadGuide() {
  let data;
  try {
    data = await getJSON("/api/capabilities");
  } catch {
    return; // the guide is a convenience; never block the app on it
  }
  const groups = data.groups || [];
  if (!groups.length || !el.guideGroups) return;

  el.guideGroups.innerHTML = "";
  const tables = [];
  for (const group of groups) {
    for (const table of group.tables || []) {
      if (!tables.includes(table.name)) tables.push(table.name);
    }
  }

  // Two lists: what it knows about, then what asking looks like. Earlier this
  // was a block per topic carrying its own tables, description and caveats,
  // which was six of everything on the one screen meant to get someone talking.
  const topics = document.createElement("div");
  topics.className = "guide-group";
  const topicsLabel = document.createElement("span");
  topicsLabel.className = "guide-subject";
  topicsLabel.textContent = "Topics";
  topics.appendChild(topicsLabel);

  const topicList = document.createElement("ul");
  topicList.className = "guide-about";
  for (const group of groups) {
    const li = document.createElement("li");
    li.textContent = group.subject;
    // The tables behind a topic, what they hold and what they cannot answer:
    // there for anyone who wants it, costing nothing to anyone who does not.
    li.title = (group.tables || [])
      .map((t) => [t.label || t.name, t.about,
                   t.notCovered && "Not in this data: " + t.notCovered]
        .filter(Boolean).join("\n"))
      .join("\n\n");
    topicList.appendChild(li);
  }
  topics.appendChild(topicList);
  el.guideGroups.appendChild(topics);

  const asking = document.createElement("div");
  asking.className = "guide-group";
  const askingLabel = document.createElement("span");
  askingLabel.className = "guide-subject";
  askingLabel.textContent = "Types of questions";
  asking.appendChild(askingLabel);

  const asks = document.createElement("div");
  asks.className = "guide-asks";
  // Two per topic. One left three questions standing for three broad sections,
  // which reads as a thin tool rather than a shorthand.
  for (const group of groups) {
    for (const question of (group.questions || []).slice(0, 2)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "guide-ask";
      btn.textContent = question;
      // Clicking loads it into the composer rather than sending, so the user
      // can edit it and can see where their words go.
      btn.addEventListener("click", () => {
        el.composerInput.value = question;
        if (!el.composerInput.disabled) el.composerInput.focus();
        else el.modeLabel.textContent = "Start the conversation, then send it";
      });
      asks.appendChild(btn);
    }
  }
  asking.appendChild(asks);

  // What an answer sounds like, once. Spoken answers are a sentence or two,
  // and someone expecting a table reads that as the lookup having failed.
  const example = groups.map((g) => (g.answers || [])[0]).find(Boolean);
  if (example) {
    const reply = document.createElement("p");
    reply.className = "guide-limit";
    reply.textContent = "Answers like: \u201c" + example + "\u201d";
    asking.appendChild(reply);
  }
  el.guideGroups.appendChild(asking);

  // Questions the PDF asks for that nothing can answer yet. Better read here
  // than discovered mid-call: each one is a reasonable thing for a rep to want
  // and will stay unanswerable until the table behind it lands.
  const tbd = data.tbd || [];
  if (tbd.length) {
    const block = document.createElement("div");
    block.className = "guide-group";
    const label = document.createElement("span");
    label.className = "guide-subject";
    label.textContent = "TBD — not answerable yet";
    block.appendChild(label);

    const list = document.createElement("ul");
    list.className = "guide-about";
    for (const item of tbd) {
      const li = document.createElement("li");
      li.textContent = item.question;
      if (item.waitingOn) li.title = "Waiting on " + item.waitingOn;
      list.appendChild(li);
    }
    block.appendChild(list);

    const waiting = [...new Set(tbd.map((t) => t.waitingOn).filter(Boolean))];
    if (waiting.length) {
      const note = document.createElement("p");
      note.className = "guide-limit";
      note.textContent = "Waiting on " + waiting.join(", ") + ".";
      block.appendChild(note);
    }
    el.guideGroups.appendChild(block);
  }

  if (el.guideFoot) {
    // The gaps worth knowing before you ask, in one line rather than one per
    // topic. Everything else about a topic is on its tooltip.
    el.guideFoot.textContent =
      `Read live from ${tables.length} table${tables.length === 1 ? "" : "s"} ` +
      "each time you ask, never from memory.";
  }
}

/** Show which LLM the selected agent is wired to. */
async function refreshAgentInfo() {
  const agentId = currentAgentId();
  if (!agentId) {
    el.llmNote.textContent = "";
    return;
  }
  state.ready = Boolean(state.config.hasApiKey && agentId);
  render();
  el.llmNote.textContent = "Checking agent…";
  try {
    const info = await getJSON(`/api/agent-info?agent_id=${encodeURIComponent(agentId)}`);
    document.title = `${info.name} · ElevenLabs Agent`;
    el.brandName.textContent = info.name || "ElevenLabs Agent";
    el.llmNote.textContent =
      info.llm === "custom-llm"
        ? `Custom LLM · ${info.customLlmModel} · ${info.customLlmUrl}`
        : `LLM · ${info.llm}`;
  } catch (err) {
    el.llmNote.textContent = `Could not read agent config: ${err.message}`;
  }
}

// ---------------------------------------------------------------- boot

async function init() {
  render();
  renderDebugStats();
  log("out", "ui ready", navigator.userAgent.slice(0, 60));

  try {
    state.config = await getJSON("/api/config");
  } catch (err) {
    showError(`Could not read server config: ${err.message}`);
    return;
  }

  el.agentIdInput.value = state.config.agentId || "";

  if (!state.config.hasApiKey) {
    el.keyNote.textContent =
      "No API key loaded. Add ELEVENLABS_API_KEY to the .env file next to server.py and restart the server.";
    el.keyNote.classList.add("warn");
    el.settingsPanel.hidden = false;
    showError("ELEVENLABS_API_KEY is missing from .env — the agent cannot be reached.");
    return;
  }

  el.keyNote.textContent = state.config.user
    ? `Signed in as ${state.config.user}. The API key stays on the server.`
    : "The API key stays on the server and is never sent to this page.";

  // Populate the agent picker.
  try {
    const { agents } = await getJSON("/api/agents");
    el.agentSelect.innerHTML = "";
    if (!agents.length) {
      el.agentSelect.appendChild(new Option("No agents found on this account", ""));
    } else {
      el.agentSelect.appendChild(new Option("— select an agent —", ""));
      for (const agent of agents) {
        el.agentSelect.appendChild(new Option(agent.name, agent.agentId));
      }
      if (state.config.agentId) el.agentSelect.value = state.config.agentId;
      if (!el.agentIdInput.value && agents.length === 1) {
        el.agentIdInput.value = agents[0].agentId;
        el.agentSelect.value = agents[0].agentId;
      }
    }
  } catch (err) {
    el.agentSelect.innerHTML = "";
    el.agentSelect.appendChild(new Option("Could not list agents", ""));
    showError(`Could not list agents: ${err.message}`);
  }

  if (!el.agentIdInput.value) el.settingsPanel.hidden = false;
  state.ready = Boolean(state.config.hasApiKey && el.agentIdInput.value);
  render();
  refreshAgentInfo();
  loadGuide();
}

init();
