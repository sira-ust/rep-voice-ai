// ElevenLabs Agent web UI.
//
// The API key lives only on the Python server (.env). This file asks that server
// for a short-lived conversation token / signed URL and hands it to the SDK.

import { Conversation } from "https://cdn.jsdelivr.net/npm/@elevenlabs/client@1.25.0/+esm";
import { renderVisual, visualToText } from "/visuals.js";

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
  modelSelect: $("modelSelect"),
  settingsClose: $("settingsClose"),
  agentGroup: $("agentGroup"),
  debugDownload: $("debugDownload"),
  keyNote: $("keyNote"),
  llmNote: $("llmNote"),
  orb: $("orb"),
  modeLabel: $("modeLabel"),
  repField: $("repField"),
  repSelect: $("repSelect"),
  repNote: $("repNote"),
  callBtn: $("callBtn"),
  callBtnLabel: $("callBtnLabel"),
  muteBtn: $("muteBtn"),
  volumeSlider: $("volumeSlider"),
  meterFill: $("meterFill"),
  transcript: $("transcript"),
  guide: $("guide"),
  guideGroups: $("guideGroups"),
  topicBar: $("topicBar"),
  topicBarTopics: $("topicBarTopics"),
  topicBarPanel: $("topicBarPanel"),
  guideFoot: $("guideFoot"),
  debugToggle: $("debugToggle"),
  debugLog: $("debugLog"),
  debugCount: $("debugCount"),
  debugStat: $("debugStat"),
  debugDetail: $("debugDetail"),
  debugCopy: $("debugCopy"),
  debugClear: $("debugClear"),
  copyTranscript: $("copyTranscript"),
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
  // The rep's words as our own proxy saw them, ahead of ElevenLabs' copy.
  live: { source: null, node: null, reply: null, visuals: [] },
  // Counters surfaced in the debug header -- the fastest way to tell whether
  // the server's JSON events are reaching the browser at all.
  counts: { incoming: 0, transcripts: 0, agentReplies: 0, audioSent: 0 },
  startedAt: 0,
  // False until a key is loaded and an agent is selected, so the idle pill can
  // distinguish "Ready" from "Not configured".
  ready: false,
  // True until the agent, the rep list and the data warehouse are all ready.
  // The rep list comes from Databricks, so on a cold warehouse it used to
  // arrive many seconds after the page, which sat showing only "All reps" --
  // and a call started then would have run as the manager view.
  loading: true,
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
// CJK Unified Ideographs, the Extension A block below it, and the compatibility
// block above. Kana are deliberately absent: this asks "is this Han text", and
// the answer decides glyph shapes, not which language the agent replied in.
const HAN = /[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF]/;

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
  // Han characters are unified across Chinese and Japanese in Unicode: the same
  // codepoint is drawn with different strokes depending on the language, and the
  // browser decides which using `lang`. The document is lang="en", so a Chinese
  // reply inherits it and can be rendered in Japanese letterforms wherever the
  // fallback font supports both. Tagging the bubble by what is actually in it
  // keeps the agent's own detection as the only thing choosing a language --
  // there is no picker to get out of step with.
  if (HAN.test(text)) node.lang = "zh";
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
  // For the layout: on a PDA the controls shrink to one row once a call is
  // under way, so the answers get the screen (styles.css, "PDA view").
  document.body.dataset.call = live ? "live" : busy ? "busy" : "idle";
  el.callBtn.classList.toggle("is-live", live);
  el.callBtn.disabled = busy;
  // Locked for the duration of a call. The agent is told who it is speaking to
  // once, at the start, so changing it mid-conversation would leave the picker
  // and every answer disagreeing about whose accounts these are.
  if (el.repSelect) el.repSelect.disabled = live || busy;
  // The live controls are one row at CT70 width, which "End conversation" overfills.
  el.callBtnLabel.textContent = live
    ? "End call"
    : busy
      ? STATUS_LABEL[status]
      : "Start conversation";

  if (state.loading) {
    el.callBtn.disabled = true;
    if (el.repSelect) el.repSelect.disabled = true;
    el.callBtnLabel.textContent = "Getting ready…";
    el.statusText.textContent = "Getting ready";
  }

  el.orb.className = "orb";
  if (state.loading) {
    el.orb.classList.add("connecting");
    el.modeLabel.textContent = "Getting ready — loading your reps and waking the data…";
  } else if (busy) {
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
  el.agentSelect.disabled = live;
  el.agentIdInput.disabled = live;
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
  // WebSocket, always: it is the connection every part of this app has been
  // tested on. The WebRTC option was a choice no rep needed to make.
  const connectionType = "websocket";

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

    // Who the agent takes the caller to be. The prompt reads {{rep_name}},
    // so this must always be sent -- a referenced variable with nothing behind
    // it fails the whole session, and "All" is a real answer here rather than
    // a missing one.
    const rep = el.repSelect ? el.repSelect.value : "";
    log("out", "signed in as", rep || "All reps");

    // A signed statement of whose accounts this conversation may read. The
    // page cannot mint one and the agent cannot alter one, so the rep survives
    // the round trip through ElevenLabs without being something anyone on the
    // call can change. Sent as an extra body field, which ElevenLabs passes
    // through to our own LLM endpoint untouched.
    let scopeToken = "";
    let callId = "";
    let account = "";
    try {
      ({ scopeToken, callId, user: account } = await getJSON(`/api/scope?rep=${encodeURIComponent(rep)}`));
    } catch (err) {
      log("err", "scope token failed", err.message || String(err));
    }
    openLiveTranscript(callId);
    state.callId = callId;

    state.conversation = await Conversation.startSession({
      ...auth,
      connectionType,
      textOnly: false,
      dynamicVariables: { rep_name: rep || "All" },
      customLlmExtraBody: { scope_token: scopeToken },
      // The sign-in account, shown as the user on the conversation in
      // ElevenLabs' history, so each call says who was speaking.
      ...(account ? { userId: account } : {}),

      onConnect: ({ conversationId }) => {
        log("cb", "onConnect", conversationId);
        state.conversationId = conversationId;
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
          // Already shown from the live stream: ElevenLabs' copy is the
          // authoritative wording, so take it, but in the same bubble.
          // The bubble stays this turn's until the reply is confirmed below:
          // ElevenLabs sometimes asks again just after confirming the words --
          // the rep kept talking, or the mic caught something while the agent
          // spoke -- and closing the turn here sent that re-transcription to a
          // new bubble, so the question showed twice with the answer after it.
          if (state.live.node && state.live.node.isConnected) {
            state.live.node.textContent = message;
          } else {
            addMessage(message, "user");
          }
        } else {
          state.counts.agentReplies += 1;
          // Replace the streamed preview rather than adding a second bubble.
          // The live bubble is kept as it is: ElevenLabs' copy is the same
          // words with the formatting stripped for the voice.
          // The reply is confirmed, so the turn is over: the next words from
          // the rep start a new bubble.
          state.live.node = null;
          state.live.visuals = [];        // confirmed: they stay with this turn
          // ElevenLabs' copy is what was actually spoken. Kept on the bubble
          // beside the screen text, for "Copy conversation" -- a visit plan's
          // summary is spoken, its details only shown.
          let answered;
          if (state.live.reply && state.live.reply.isConnected) {
            answered = state.live.reply;
            state.live.reply.dataset.spoken = message;
            state.live.reply = null;
          } else if (state.streaming.node) {
            answered = state.streaming.node;
            state.streaming.node.textContent = message;
            state.streaming.node.dataset.spoken = message;
            state.streaming = { id: null, node: null, text: "" };
          } else {
            answered = addMessage(message, "ai");
            answered.dataset.spoken = message;
          }
          addFeedbackRow(answered);
        }
        renderDebugStats();
      },

      onModeChange: ({ mode }) => {
        log("cb", "onModeChange", mode);
        state.mode = mode;
        holdMicWhileSpeaking(mode);
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
    closeLiveTranscript();
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

/**
 * Show the rep's words the moment our proxy receives them.
 *
 * ElevenLabs sends the page a user transcript only together with the first
 * sentence of the reply, so a slow lookup kept the rep's own words off the
 * screen for as long as the lookup took. The proxy has them within a second of
 * the rep stopping, and runs in the same server as this page, so it passes them
 * straight here. Proxy mode only: in direct mode nothing is published and this
 * simply stays quiet.
 */
function openLiveTranscript(callId) {
  closeLiveTranscript();
  if (!callId || typeof EventSource === "undefined") return;
  const source = new EventSource(`/api/live?call=${encodeURIComponent(callId)}`);
  source.onmessage = (msg) => {
    let item;
    try {
      item = JSON.parse(msg.data);
    } catch {
      return;
    }
    if (item && item.type === "visual") {
      showVisual(item.visual);
      return;
    }
    if (!item || !item.text) return;
    if (item.type === "agent") {
      showLiveReply(item);
      return;
    }
    if (item.type !== "user") return;
    logDetailed("in", "live user words", item.text);
    // One bubble per turn, however many times ElevenLabs asks. It sends a new
    // request at each pause and often re-transcribes the whole utterance, so
    // a later version is not always the earlier one with words added -- and
    // treating it as a new turn left the first version, and the reply to it,
    // on screen as a turn that never happened. Until ElevenLabs confirms the
    // turn (onMessage below), each new version replaces the last.
    //
    // A reply not yet confirmed answered a version the rep has since added
    // to; ElevenLabs dropped it, so it comes off the screen.
    if (state.live.reply && state.live.reply.isConnected) state.live.reply.remove();
    state.live.reply = null;
    for (const card of state.live.visuals) card.remove();
    state.live.visuals = [];
    const node = state.live.node;
    if (node && node.isConnected) {
      node.textContent = item.text;
    } else {
      state.live.node = addMessage(item.text, "user");
    }
  };
  source.onerror = () => log("err", "live transcript", "stream interrupted; retrying");
  state.live.source = source;
}

/**
 * The proxy's own reply, piece by piece: "Let me check." the moment a lookup
 * starts, then the answer. ElevenLabs streams reply text as well but holds it
 * back until the whole answer exists, so the rep heard "Let me check." with
 * nothing on screen until it had finished speaking the result.
 */
function showLiveReply(item) {
  logDetailed("in", "live reply", item.text);
  let node = state.live.reply;
  let raw = item.text;
  if (node && node.isConnected) {
    // Until ElevenLabs confirms it, this turn's reply: a later piece adds to
    // it, and a second opening -- ElevenLabs asked again -- replaces it.
    // On its own line, so an answer that opens with a list still renders as one.
    if (!item.start) raw = `${(node.dataset.raw || "").trimEnd()}\n${item.text}`;
  } else {
    node = state.live.reply = addMessage("", "ai");
  }
  renderReply(node, raw);
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

/**
 * Show a reply's light markdown: **bold**, *italics*, ==highlight== and "- "
 * lists. The proxy asks the model for it so figures and names stand out on
 * screen; the voice gets the same words with the markup stripped.
 *
 * Everything is HTML-escaped first and only these few patterns are turned back
 * into tags, so nothing the model writes can put markup of its own on the page.
 */
function renderReply(node, raw) {
  node.dataset.raw = raw;
  if (HAN.test(raw)) node.lang = "zh";
  const escape = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) => escape(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/==(.+?)==/g, "<mark>$1</mark>")
    .replace(/(^|[^\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])/g, "$1<em>$2</em>");
  const html = [];
  let list = null;
  for (const line of raw.split("\n")) {
    const bullet = line.match(/^\s*(?:[-*\u2022]|\d+[.\u0029])\s+(.*)$/);
    if (bullet) {
      if (!list) html.push((list = []));
      list.push(`<li>${inline(bullet[1])}</li>`);
    } else if (line.trim()) {
      list = null;
      html.push(`<p>${inline(line.trim())}</p>`);
    }
  }
  node.innerHTML = html.map((p) => (Array.isArray(p) ? `<ul>${p.join("")}</ul>` : p)).join("");
}

/**
 * A chart, table or map under this turn's reply. It arrives after the reply's
 * text, built by the server from the lookup rows, and goes if the reply does --
 * ElevenLabs dropping the turn takes its picture with it.
 */
function showVisual(visual) {
  let card = null;
  try {
    card = renderVisual(visual);
  } catch (err) {
    log("err", "visual", err.message || String(err));
  }
  if (!card) return;
  logDetailed("in", "live visual", `${visual.type}: ${visual.title || ""}`);
  el.transcript.appendChild(card);
  // The rating row belongs below the whole answer, card included.
  if (state.feedbackRow && card.previousElementSibling === state.feedbackRow) {
    el.transcript.appendChild(state.feedbackRow);
  }
  // Only an unconfirmed turn's picture can still be withdrawn.
  if (state.live.reply) state.live.visuals.push(card);
  el.transcript.scrollTop = el.transcript.scrollHeight;
}

function closeLiveTranscript() {
  if (state.live.source) state.live.source.close();
  state.live = { source: null, node: null, reply: null, visuals: [] };
}

function teardown() {
  closeLiveTranscript();
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

// The agent heard itself: on a handheld or laptop speaker its reply came back
// in through the microphone, was transcribed as the rep speaking, and answered.
// Browser echo cancellation does not catch all of it. So the microphone is off
// while the agent speaks and comes back a moment after it stops -- the tail of
// the audio is still in the room. The rep's own mute button wins: a muted mic
// stays muted. The cost is that the rep cannot talk over the agent; a headset
// would allow that, and this is where to relax it if one is standard.
const MIC_BACK_AFTER_MS = 450;
let micBackTimer = null;

function holdMicWhileSpeaking(mode) {
  if (!state.conversation || typeof state.conversation.setMicMuted !== "function") return;
  clearTimeout(micBackTimer);
  try {
    if (mode === "speaking") {
      state.conversation.setMicMuted(true);
    } else {
      micBackTimer = setTimeout(() => {
        try {
          if (state.conversation && state.mode !== "speaking") state.conversation.setMicMuted(state.muted);
        } catch { /* call ended in the meantime */ }
      }, MIC_BACK_AFTER_MS);
    }
  } catch (err) {
    log("err", "mic hold", err.message || String(err));
  }
}

el.muteBtn.addEventListener("click", () => {
  if (!state.conversation) return;
  state.muted = !state.muted;
  try {
    // While the agent speaks the mic is held off regardless; the rep's choice
    // takes effect when it stops.
    if (state.mode !== "speaking") state.conversation.setMicMuted(state.muted);
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
el.settingsClose.addEventListener("click", () => {
  el.settingsPanel.hidden = true;
});

// A placeholder until a second model exists: the choice is remembered, and
// the server still answers with the on-site one.
try {
  const savedModel = localStorage.getItem("convai.model");
  if (savedModel && [...el.modelSelect.options].some((o) => o.value === savedModel && !o.disabled)) {
    el.modelSelect.value = savedModel;
  }
} catch { /* storage blocked: the default is fine */ }
el.modelSelect.addEventListener("change", () => {
  try { localStorage.setItem("convai.model", el.modelSelect.value); } catch { /* not worth telling */ }
});

el.errorClose.addEventListener("click", clearError);

el.agentSelect.addEventListener("change", () => {
  if (el.agentSelect.value) {
    el.agentIdInput.value = el.agentSelect.value;
    refreshAgentInfo();
  }
});

el.agentIdInput.addEventListener("change", refreshAgentInfo);

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

// ---------------------------------------------------------------- rep feedback
//
// A thumbs up or down under each answer; a thumbs down asks what was wrong.
// What is sent is what the rep saw -- the question, the answer as spoken and as
// shown, the card -- so a bad answer can be traced back. One rating per answer,
// changeable: a new rating keeps the same id and the latest counts.

const FEEDBACK_REASONS = {
  up: ["Useful idea", "Accurate", "Good timing"],
  down: ["Wrong info", "Not useful", "Missed something", "Too long", "Wrong store or item"],
};

// Dictation uses the device's own speech-to-text where the browser has it
// (Chrome on Android and desktop, Edge). It cannot share the microphone with a
// live call, so during one the button says to tell the agent instead.
const SpeechRecognizer = window.SpeechRecognition || window.webkitSpeechRecognition || null;

function dictateInto(box, button, onUsed) {
  if (!SpeechRecognizer) return null;
  let rec = null;
  button.addEventListener("click", () => {
    if (state.status === "connected") {
      button.title = "The call is using the microphone - just tell the agent your feedback";
      note(button, "On a call: just tell the agent, e.g. \"Feedback: ...\"");
      return;
    }
    if (rec) { rec.stop(); return; }
    rec = new SpeechRecognizer();
    rec.lang = "en-US";
    rec.interimResults = false;
    rec.maxAlternatives = 1;
    button.setAttribute("aria-pressed", "true");
    rec.onresult = (e) => {
      const said = Array.from(e.results).map((r) => r[0].transcript).join(" ").trim();
      if (said) {
        box.value = (box.value ? `${box.value.trim()} ` : "") + said;
        onUsed();
      }
    };
    rec.onerror = (e) => log("err", "dictation", e.error || "failed");
    rec.onend = () => { rec = null; button.setAttribute("aria-pressed", "false"); };
    rec.start();
  });
  return button;
}

function note(near, text) {
  const row = near.closest(".feedback");
  const slot = row && row.querySelector(".feedback-note");
  if (slot) slot.textContent = text;
}

function answerContext(reply) {
  let question = "";
  for (let n = reply.previousElementSibling; n; n = n.previousElementSibling) {
    if (n.classList.contains("msg-user")) { question = n.textContent.trim(); break; }
  }
  const card = [];
  for (let n = reply.nextElementSibling; n && !n.classList.contains("msg-user"); n = n.nextElementSibling) {
    if (n.classList.contains("msg-visual") && n.visual) card.push(n.visual);
  }
  return {
    question,
    spoken: reply.dataset.spoken || "",
    screen: reply.dataset.raw || reply.textContent || "",
    card,
  };
}

async function sendFeedback(reply, body) {
  const rep = el.repSelect && el.repSelect.selectedOptions[0] ? el.repSelect.selectedOptions[0].textContent : "";
  const payload = {
    id: reply.dataset.feedbackId, call: state.callId || "", conversation: state.conversationId || "",
    rep, at: new Date().toISOString(), ...answerContext(reply), ...body,
  };
  const res = await fetch("/api/feedback", {
    method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`feedback ${res.status}`);
}

function addFeedbackRow(reply) {
  // Not under the greeting, which answers nothing.
  let asked = false;
  for (let n = reply.previousElementSibling; n; n = n.previousElementSibling) {
    if (n.classList.contains("msg-user")) { asked = true; break; }
  }
  if (!asked || !reply.isConnected) return;
  reply.dataset.feedbackId = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now() + Math.random());

  const make = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text) n.textContent = text;
    return n;
  };
  const row = make("div", "feedback");
  const up = make("button", "feedback-btn", "👍");
  const down = make("button", "feedback-btn", "👎");
  up.setAttribute("aria-label", "Helpful");
  down.setAttribute("aria-label", "Not helpful");
  const status = make("span", "feedback-note");
  row.append(make("span", "feedback-ask", "Was this helpful?"), up, down, status);

  // Both thumbs open the same box: what was good is as worth knowing as what was not.
  const form = make("div", "feedback-form");
  form.hidden = true;
  const chipBox = make("div", "feedback-chips");
  const comment = make("textarea", "feedback-comment");
  comment.maxLength = 1000;
  comment.rows = 2;
  const tools = make("div", "feedback-tools");
  const mic = make("button", "feedback-btn feedback-mic", "🎤");
  mic.type = "button";
  mic.setAttribute("aria-label", "Dictate");
  mic.title = "Dictate your feedback";
  const send = make("button", "btn-mini feedback-send", "Send");
  form.append(chipBox, comment, tools);
  row.append(form);

  let rating = "";
  let mode = "none";
  if (dictateInto(comment, mic, () => { mode = "dictated"; })) tools.append(mic);
  tools.append(send);
  comment.addEventListener("input", () => { if (mode === "none") mode = "text"; });

  const submit = async (body) => {
    try {
      await sendFeedback(reply, { rating, ...body });
      status.textContent = "Thanks, noted";
    } catch (err) {
      status.textContent = "Couldn't send - try again";
      log("err", "feedback", err.message || String(err));
    }
  };
  const choose = (value) => {
    rating = value;
    up.setAttribute("aria-pressed", String(value === "up"));
    down.setAttribute("aria-pressed", String(value === "down"));
    chipBox.replaceChildren(...FEEDBACK_REASONS[value].map((label) => {
      const chip = make("button", "feedback-chip", label);
      chip.type = "button";
      chip.setAttribute("aria-pressed", "false");
      chip.addEventListener("click", () => {
        chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true");
      });
      return chip;
    }));
    comment.placeholder = value === "up" ? "What made it useful? (optional)" : "What would have helped? (optional)";
    form.hidden = false;
    // The thumb alone is saved straight away; a note sent after updates it.
    submit({ mode: "none" });
    el.transcript.scrollTop = el.transcript.scrollHeight;
  };
  up.addEventListener("click", () => choose("up"));
  down.addEventListener("click", () => choose("down"));
  send.addEventListener("click", () => {
    const reasons = Array.from(chipBox.children)
      .filter((c) => c.getAttribute("aria-pressed") === "true").map((c) => c.textContent);
    form.hidden = true;
    submit({ reasons, comment: comment.value.trim(), mode: comment.value.trim() ? mode : "none" });
  });

  // Below the whole answer: its card often arrives before ElevenLabs confirms
  // the reply, so the row goes after any card already there (and showVisual
  // moves it down again for one that comes later).
  let last = reply;
  while (last.nextElementSibling && last.nextElementSibling.classList.contains("msg-visual")) {
    last = last.nextElementSibling;
  }
  last.after(row);
  state.feedbackRow = row;
}

/**
 * The conversation as plain text: each turn as spoken, and as it appeared on
 * screen when that differs -- a visit plan speaks a summary and shows the
 * whole plan, and both belong in what a rep pastes into an email.
 */
function transcriptText() {
  const rep = el.repSelect && el.repSelect.selectedOptions[0] ? el.repSelect.selectedOptions[0].textContent : "";
  const when = new Date().toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
  const out = [`U.S. Trading sales assistant${rep ? ` - ${rep}` : ""} - ${when}`];
  for (const item of el.transcript.children) {
    if (item.classList.contains("guide")) continue;
    if (item.classList.contains("msg-user")) {
      out.push("", `You: ${item.textContent.trim()}`);
    } else if (item.classList.contains("msg-ai")) {
      const screen = (item.dataset.raw || item.textContent || "").trim();
      const spoken = (item.dataset.spoken || "").trim();
      const flat = (s) => s.replace(/[*=]/g, "").replace(/\s+/g, " ").trim();
      if (spoken && flat(spoken) !== flat(screen)) {
        out.push("", `Agent (spoken): ${spoken}`, "", `Agent (on screen):\n${screen}`);
      } else {
        out.push("", `Agent: ${screen || spoken}`);
      }
    } else if (item.classList.contains("msg-visual") && item.visual) {
      out.push("", "On screen:", visualToText(item.visual));
    } else if (item.classList.contains("msg-system")) {
      out.push("", `(${item.textContent.trim()})`);
    }
  }
  return out.join("\n");
}

el.copyTranscript.addEventListener("click", async () => {
  const text = transcriptText();
  try {
    await navigator.clipboard.writeText(text);
    el.copyTranscript.textContent = "Copied";
  } catch {
    // No clipboard permission (an older handheld browser): a hidden text box
    // and the old copy command still work there.
    const box = document.createElement("textarea");
    box.value = text;
    box.style.position = "fixed";
    box.style.opacity = "0";
    document.body.appendChild(box);
    box.select();
    const ok = document.execCommand && document.execCommand("copy");
    box.remove();
    el.copyTranscript.textContent = ok ? "Copied" : "Copy failed";
  }
  setTimeout(() => (el.copyTranscript.textContent = "Copy conversation"), 1500);
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

// The log as a file to attach to a support message, with what support asks
// first -- which agent, which rep, which browser -- at the top.
el.debugDownload.addEventListener("click", () => {
  const now = new Date();
  const rep = el.repSelect && el.repSelect.selectedOptions[0] ? el.repSelect.selectedOptions[0].textContent : "";
  const agent = el.agentSelect.selectedOptions[0] ? el.agentSelect.selectedOptions[0].textContent : "";
  const header = [
    `U.S. Trading sales assistant - event log`,
    `Saved:   ${now.toLocaleString("en-US")}`,
    `Agent:   ${agent} (${currentAgentId() || "none"})`,
    `Rep:     ${rep}`,
    `Model:   ${el.modelSelect.selectedOptions[0] ? el.modelSelect.selectedOptions[0].textContent : ""}`,
    `Call:    ${state.conversationId || "-"}`,
    `Details: ${logDetail ? "on" : "off"}`,
    `Browser: ${navigator.userAgent}`,
    "",
  ];
  const blob = new Blob([header.concat(logLines).join("\n") + "\n"], { type: "text/plain" });
  const stamp = now.toISOString().slice(0, 16).replace(/[-:T]/g, "");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `voice-log-${stamp}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(link.href), 2000);
  el.debugDownload.textContent = "Saved";
  setTimeout(() => (el.debugDownload.textContent = "Download"), 1500);
});

window.addEventListener("beforeunload", () => {
  if (state.conversation) state.conversation.endSession();
});

/** Render the "what can I ask" guide from the tools actually wired up. */
function showRepNote(text) {
  if (!el.repNote) return;
  el.repNote.textContent = text;
  el.repNote.hidden = false;
}

/** Fill the rep picker. Failure leaves the single "All reps" option, which
 *  still works -- the agent simply has to ask who it is speaking to. */
async function loadReps() {
  if (!el.repSelect) return;
  let data;
  try {
    data = await getJSON("/api/reps");
  } catch (err) {
    showRepNote("Could not load the rep list: " + (err.message || err));
    return;
  }
  // Opened by the PDA for the rep signed in on it: their name, no list, and no
  // "All reps". The server holds the same lock, so this is how it looks, not
  // what enforces it.
  if (data.locked) {
    const rep = data.locked;
    const option = document.createElement("option");
    option.value = rep.name || "";
    option.textContent = rep.name ? `${rep.name} (${rep.code})` : `Sales ID ${rep.code}`;
    el.repSelect.replaceChildren(option);
    el.repSelect.value = option.value;
    el.repSelect.hidden = true;
    // Nor does a rep pick the agent: that is set up for them. On the PDA, no
    // sign out either: the app signed in for them, and they have no password
    // to come back with. A rep's own account on a computer keeps it.
    if (el.agentGroup) el.agentGroup.hidden = true;
    const onPda = data.via !== "account";
    const signOut = document.getElementById("signOut");
    if (signOut) signOut.hidden = onPda;
    el.keyNote.textContent = onPda ? `Signed in on the PDA as ${option.textContent}.`
                                   : `Signed in as ${option.textContent}.`;
    const shown = document.createElement("div");
    shown.className = "rep-locked";
    shown.textContent = option.textContent;
    el.repSelect.after(shown);
    return;
  }
  // An empty picker and a failed lookup look the same from the outside, and
  // the person who can fix it is the one staring at the page.
  if (!(data.reps || []).length) {
    showRepNote("Rep list unavailable" + (data.error ? " — " + data.error : "") +
                ". Ask as a manager, or name the rep in your question.");
  }
  for (const rep of data.reps || []) {
    const option = document.createElement("option");
    option.value = rep.name;
    option.textContent = rep.code ? `${rep.name} (${rep.code})` : rep.name;
    el.repSelect.appendChild(option);
  }
  // Survives a reload, so a rep testing a few questions is not re-picking
  // themselves every time.
  try {
    const saved = localStorage.getItem("convai.rep");
    if (saved && [...el.repSelect.options].some((o) => o.value === saved)) {
      el.repSelect.value = saved;
    }
  } catch {
    // Storage blocked; the default is fine.
  }
  el.repSelect.addEventListener("change", () => {
    try {
      localStorage.setItem("convai.rep", el.repSelect.value);
    } catch {
      // Not persisting is not worth telling anyone about.
    }
  });
}

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

  const TOPIC_COLOURS = 7;      // .topic-0 to .topic-6 in styles.css

  // An example question, in its topic's colour. Something to say, not to
  // tap: the app is voice only, so there is nowhere to send it to.
  const askExample = (question, i) => {
    const chip = document.createElement("span");
    chip.className = `guide-ask topic-${i % TOPIC_COLOURS}`;
    chip.textContent = question;
    return chip;
  };

  // The topics, pinned above the chat. The guide inside the chat goes with
  // the first message, and a rep mid-call still needs to know what they can
  // ask -- so the areas stay in view the whole time, as coloured bubbles.
  // Picking one opens its example questions under the bar; picking it again
  // closes them.
  if (el.topicBar && el.topicBarTopics && el.topicBarPanel) {
    let picked = null;
    el.topicBarTopics.innerHTML = "";
    const showPanel = () => {
      const panel = el.topicBarPanel;
      panel.innerHTML = "";
      panel.hidden = picked === null;
      [...el.topicBarTopics.children].forEach((b, i) => {
        b.classList.toggle("is-picked", picked === i);
        b.setAttribute("aria-expanded", String(picked === i));
      });
      if (picked === null) return;
      const group = groups[picked];
      for (const question of (group.questions || []).slice(0, 6)) {
        panel.appendChild(askExample(question, picked));
      }
    };
    groups.forEach((group, i) => {
      const bubble = document.createElement("button");
      bubble.type = "button";
      bubble.className = `guide-topic topic-${i % TOPIC_COLOURS}`;
      bubble.textContent = group.subject;
      bubble.setAttribute("aria-expanded", "false");
      bubble.setAttribute("aria-controls", "topicBarPanel");
      // The tables behind a topic, what they hold and what they cannot answer:
      // there for anyone who wants it, costing nothing to anyone who does not.
      bubble.title = (group.tables || [])
        .map((t) => [t.label || t.name, t.about,
                     t.notCovered && "Not in this data: " + t.notCovered]
          .filter(Boolean).join("\n"))
        .join("\n\n");
      bubble.addEventListener("click", () => {
        picked = picked === i ? null : i;
        showPanel();
      });
      el.topicBarTopics.appendChild(bubble);
    });
    el.topicBar.hidden = false;
  }

  // Before the first message: one example per topic, in its topic's colour,
  // so the empty chat shows what a question sounds like.
  const asking = document.createElement("div");
  asking.className = "guide-group";
  const askingLabel = document.createElement("span");
  askingLabel.className = "guide-subject";
  askingLabel.textContent = "Try asking";
  asking.appendChild(askingLabel);
  const asks = document.createElement("div");
  asks.className = "guide-asks";
  groups.forEach((group, i) => {
    const question = (group.questions || [])[0];
    if (question) asks.appendChild(askExample(question, i));
  });
  asking.appendChild(asks);
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
    el.guideGroups.appendChild(block);
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
    state.loading = false;
    render();
    return;
  }

  el.agentIdInput.value = state.config.agentId || "";

  if (!state.config.hasApiKey) {
    el.keyNote.textContent =
      "No API key loaded. Add ELEVENLABS_API_KEY to the .env file next to server.py and restart the server.";
    el.keyNote.classList.add("warn");
    el.settingsPanel.hidden = false;
    showError("ELEVENLABS_API_KEY is missing from .env — the agent cannot be reached.");
    state.loading = false;
    render();
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
  loadGuide();

  // Hold the call button until all three are in: the agent, the rep list, and
  // a warehouse that answers, so the first question is not the slow one. They
  // run side by side; the rep list usually also wakes the warehouse.
  const allOption = el.repSelect && el.repSelect.options[0];
  if (allOption) allOption.textContent = "Loading reps…";
  const [, , ready] = await Promise.allSettled([
    refreshAgentInfo(),
    loadReps(),
    getJSON("/api/ready"),
  ]);
  if (allOption) allOption.textContent = "All reps (manager view)";
  const awake = ready.status === "fulfilled" && ready.value && ready.value.ready;
  log("out", "ready", awake ? "data warehouse awake" : "data warehouse still waking");
  if (!awake) {
    showRepNote("The data is still waking up, so the first answer may take a little longer.");
  }
  state.loading = false;
  render();
}

init();
