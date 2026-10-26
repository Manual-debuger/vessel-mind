// The operator console. It uses only the event protocol: POST /turns,
// GET /turns/{id}/events (through the htmx SSE extension) and
// POST /turns/{id}/cancel.
(function () {
  "use strict";

  const form = document.getElementById("ask");
  const question = document.getElementById("question");
  const send = document.getElementById("send");
  const stop = document.getElementById("stop");
  const turns = document.getElementById("turns");

  let current = null; // { id, source }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function running(isRunning) {
    send.disabled = isRunning;
    stop.disabled = !isRunning;
  }

  function finish(turn, status, text) {
    status.textContent = text;
    if (turn.source) turn.source.close(); // or EventSource reconnects and replays
    if (current === turn) {
      current = null;
      running(false);
    }
  }

  function pretty(value) {
    return JSON.stringify(value, null, 2);
  }

  // A collapsible card: the tool's name in the summary, its input and result inside.
  function toolCard(event) {
    const root = element("details", "tool");
    const summary = element("summary");
    summary.append(element("code", "tool-name", event.name));
    const state = element("span", "tool-state", " running…");
    summary.append(state);
    root.append(summary);
    root.append(element("p", "tool-label", "input"));
    root.append(element("pre", "tool-input", pretty(event.input)));
    const result = element("pre", "tool-result", "");
    root.append(element("p", "tool-label", "result"), result);
    return { root, state, result };
  }

  function showResult(card, event) {
    card.state.textContent = event.is_error ? " error" : " done";
    card.root.classList.toggle("tool-error", event.is_error);
    card.result.textContent = pretty(event.result);
  }

  function describeDone(event) {
    const u = event.usage;
    return `${event.reason} · ${event.steps} step(s) · tokens in ${u.input_tokens}, ` +
      `out ${u.output_tokens}, thinking ${u.thinking_tokens}`;
  }

  // The session of this page. Reloading the page starts a new session.
  let sessionId = null;

  async function startTurn(text) {
    const body = sessionId ? { session_id: sessionId, text } : { text };
    const response = await fetch("/turns", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => null);
      const detail = error && error.detail && error.detail.message;
      throw new Error(detail || `POST /turns: HTTP ${response.status}`);
    }
    const started = await response.json();
    sessionId = started.session_id;
    return started.turn_id;
  }

  form.addEventListener("submit", async (submit) => {
    submit.preventDefault();
    const text = question.value.trim();
    if (!text || current) return;

    const article = element("article", "turn");
    article.append(element("p", "question", text));
    const answer = element("p", "answer");
    const status = element("p", "status", "starting…");
    article.append(answer, status);
    turns.append(article);
    question.value = "";
    running(true);

    let turnId;
    try {
      turnId = await startTurn(text);
    } catch (error) {
      status.textContent = String(error);
      running(false);
      return;
    }
    const turn = { id: turnId, source: null };
    current = turn;
    status.textContent = "streaming…";

    // The SSE extension opens the stream; each event is rendered here, as
    // text, never as HTML.
    article.setAttribute("hx-ext", "sse");
    article.setAttribute("sse-connect", `/turns/${encodeURIComponent(turnId)}/events`);
    answer.setAttribute("sse-swap", "text,tool_start,tool_result,done,error");
    article.addEventListener("htmx:sseOpen", (open) => {
      turn.source = open.detail.source;
    });
    // One card per tool call, keyed by call_id; text after a tool call goes
    // into a new paragraph below its card.
    const cards = new Map();
    let paragraph = answer;
    answer.addEventListener("htmx:sseBeforeMessage", (message) => {
      message.preventDefault();
      const event = JSON.parse(message.detail.data);
      if (event.type === "text") {
        paragraph.append(document.createTextNode(event.text));
      } else if (event.type === "tool_start") {
        const card = toolCard(event);
        cards.set(event.call_id, card);
        answer.append(card.root);
        paragraph = element("span", "answer-part");
        answer.append(paragraph);
      } else if (event.type === "tool_result") {
        const card = cards.get(event.call_id);
        if (card) showResult(card, event);
      } else if (event.type === "done") {
        finish(turn, status, describeDone(event));
      } else if (event.type === "error") {
        finish(turn, status, `error (${event.code}): ${event.message}`);
      }
    });
    htmx.process(article);
  });

  stop.addEventListener("click", async () => {
    if (!current) return;
    stop.disabled = true;
    // The stream reports the end with done or error; nothing else to do here.
    await fetch(`/turns/${encodeURIComponent(current.id)}/cancel`, { method: "POST" });
  });

  question.addEventListener("keydown", (key) => {
    if (key.key === "Enter" && (key.ctrlKey || key.metaKey)) form.requestSubmit();
  });
})();
