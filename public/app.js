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
  composer: $("composer"),
  composerInput: $("composerInput"),
  composerSend: $("composerSend"),
  debugToggle: $("debugToggle"),
  debugLog: $("debugLog"),
  debugCount: $("debugCount"),
  debugStat: $("debugStat"),
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

function log(channel, label, payload) {
  const elapsed = state.startedAt ? ((Date.now() - state.startedAt) / 1000).toFixed(2) : "0.00";
  let detail = "";
  if (payload !== undefined) {
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

function renderDebugStats() {
  const c = state.counts;
  el.debugStat.textContent =
    `mic-out ${c.audioSent} · in ${c.incoming} · transcripts ${c.transcripts} · replies ${c.agentReplies}`;
}

// ---------------------------------------------------------------- utilities

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  let body = {};
  try {
    body = await res.json();
  } catch {
    /* non-JSON error page */
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

  const empty = el.transcript.querySelector(".empty-state");
  if (empty) empty.remove();

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
        log("cb", "onMessage", { role: role || source, message });
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
        if (type === "user_audio_chunk") {
          state.counts.audioSent += 1;
          if (state.counts.audioSent === 1) log("out", "FIRST mic audio chunk sent");
          if (state.counts.audioSent % 50 === 0) {
            log("out", `mic audio chunks sent: ${state.counts.audioSent}`);
          }
          renderDebugStats();
          return;
        }
        log("out", type || "event", event);
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
        log("in", type || "event", event);
      },

      onAgentChatResponsePart: (part) => {
        log("in", "responsePart", part);
        handleResponsePart(part);
      },

      onVadScore: ({ vadScore }) => {
        if (vadScore > 0.5) log("in", "vad speech", vadScore.toFixed(2));
      },

      onInterruption: (info) => log("in", "interruption", info),
      onAgentToolRequest: (info) => log("in", "toolRequest", info),
      onAgentToolResponse: (info) => log("in", "toolResponse", info),
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
  log("out", "sendUserMessage", text);
  state.conversation.sendUserMessage(text);
  addMessage(text, "user");
  el.composerInput.value = "";
});

el.debugToggle.addEventListener("click", () => {
  const open = el.debugLog.hidden;
  el.debugLog.hidden = !open;
  el.debugToggle.setAttribute("aria-expanded", String(open));
});

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

  el.keyNote.textContent = `API key loaded from .env (${state.config.apiKeyHint}). It stays on the server.`;

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
}

init();
