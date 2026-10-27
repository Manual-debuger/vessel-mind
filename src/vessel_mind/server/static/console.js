// The operator console. It uses only the event protocol: POST /turns,
// GET /turns/{id}/events (through the htmx SSE extension),
// POST /turns/{id}/cancel and POST /proposals/{id}/approve or /reject.
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
    if (turn.source) turn.source.close(); // or EventSource reconnects when the stream ends
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

  // A string stays text; a citation becomes a link to its section on GitHub.
  // citations.js links only https://github.com/ URLs.
  function citationNode(part) {
    if (typeof part === "string") return document.createTextNode(part);
    const link = element("a", "citation", `[${part.n}]`);
    link.href = part.url;
    link.title = part.source;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  }

  // A proposal: a write the model asked for, which runs only on Approve.
  function proposalCard(card, event) {
    card.state.textContent = " awaiting approval";
    card.root.classList.add("tool-proposal");
    card.root.open = true;
    const box = element("div", "proposal");
    const expires = new Date(event.expires_at).toLocaleTimeString();
    const note = element("p", "proposal-note",
      `${event.effect}: runs only if you approve it, before ${expires}.`);
    const approve = element("button", "approve", "Approve");
    const reject = element("button", "reject", "Reject");
    approve.type = "button";
    reject.type = "button";
    const outcome = element("pre", "proposal-outcome", "");
    box.append(note, approve, reject, outcome);
    card.root.append(box);
    const proposal = { id: event.proposal_id, card, note, approve, reject, outcome, open: true };
    approve.addEventListener("click", () => decide(proposal, "approve"));
    reject.addEventListener("click", () => decide(proposal, "reject"));
    return proposal;
  }

  function closeProposal(proposal, stateText) {
    proposal.open = false;
    proposal.approve.disabled = true;
    proposal.reject.disabled = true;
    proposal.card.state.textContent = ` ${stateText}`;
  }

  async function decide(proposal, decision) {
    const proposalId = proposal.id;
    proposal.approve.disabled = true;
    proposal.reject.disabled = true;
    const response = await fetch(
      `/proposals/${encodeURIComponent(proposalId)}/${decision}`, { method: "POST" });
    const body = await response.json().catch(() => null);
    if (response.ok) {
      closeProposal(proposal, body.status);
      if (body.status === "approved") {
        proposal.card.root.classList.toggle("tool-error", body.is_error === true);
        proposal.outcome.textContent = pretty(body.result);
      }
      return;
    }
    const detail = body && body.detail;
    const message = (detail && detail.message) || `HTTP ${response.status}`;
    proposal.outcome.textContent = message;
    if (detail && detail.code === "demo_mode") {
      // Demo mode refuses approvals; the proposal is still pending and can be rejected.
      proposal.reject.disabled = false;
    } else if (detail && detail.code && response.status === 409) {
      closeProposal(proposal, detail.code);
    } else {
      proposal.approve.disabled = false;
      proposal.reject.disabled = false;
    }
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
    // Filled by the route event: the route, the models and their token usage.
    const badge = element("p", "route-badge");
    badge.hidden = true;
    article.append(badge, answer, status);
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
    answer.setAttribute("sse-swap", "route,text,tool_start,proposal,tool_result,done,error");
    article.addEventListener("htmx:sseOpen", (open) => {
      turn.source = open.detail.source;
    });
    // One card per tool call, keyed by call_id; text after a tool call goes
    // into a new part below its card.
    const cards = new Map();
    const proposals = [];
    // [n] in the text cites result n of the latest search_docs call before it.
    let citations = new Map();
    const newPart = () => {
      const part = { node: element("span", "answer-part"), text: "" };
      answer.append(part.node);
      return part;
    };
    let part = newPart();
    let route = null;
    const stream = VesselStream.start();
    answer.addEventListener("htmx:sseBeforeMessage", (message) => {
      message.preventDefault();
      const read = VesselStream.read(message.detail, stream);
      if (read.kind === "disconnected") {
        // EventSource reconnects by itself and sends Last-Event-ID, so the
        // stream resumes after the last event shown; CLOSED means it gave up.
        const closed = turn.source && turn.source.readyState === EventSource.CLOSED;
        status.textContent = closed ? "disconnected" : "connection lost, reconnecting…";
        return;
      }
      if (read.kind === "duplicate") return;
      status.textContent = "streaming…";
      const event = read.event;
      if (event.type === "route") {
        route = event;
        badge.className = `route-badge route-${event.route}`;
        badge.textContent = VesselRoute.describe(route, null);
        badge.hidden = false;
      } else if (event.type === "text") {
        // Re-rendered whole: a citation may arrive split across two deltas.
        part.text += event.text;
        part.node.replaceChildren(...VesselCitations.split(part.text, citations).map(citationNode));
      } else if (event.type === "tool_start") {
        const card = toolCard(event);
        cards.set(event.call_id, card);
        answer.append(card.root);
        part = newPart();
      } else if (event.type === "proposal") {
        const card = cards.get(event.call_id);
        if (card) {
          card.proposed = true;
          proposals.push(proposalCard(card, event));
        }
      } else if (event.type === "tool_result") {
        const card = cards.get(event.call_id);
        // A proposal's result only says it awaits approval; the card keeps its state.
        if (card && card.proposed) card.result.textContent = pretty(event.result);
        else if (card) showResult(card, event);
        if (event.name === "search_docs" && !event.is_error && event.result) {
          citations = VesselCitations.fromResults(event.result.results);
        }
      } else if (event.type === "done") {
        if (event.reason === "cancelled") {
          // The server withdraws the proposals of a cancelled turn.
          for (const p of proposals) if (p.open) closeProposal(p, "withdrawn");
        }
        if (route) badge.textContent = VesselRoute.describe(route, event);
        finish(turn, status, describeDone(event));
      } else if (event.type === "error") {
        // The server withdraws the proposals of a failed turn too.
        for (const p of proposals) if (p.open) closeProposal(p, "withdrawn");
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
