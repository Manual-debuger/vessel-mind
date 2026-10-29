// Reading a turn's SSE stream. Pure functions, no DOM, so a test can run them
// outside a browser.
(function (global) {
  "use strict";

  // The state of one turn's stream: the position of the last event shown.
  function start() {
    return { seen: -1 };
  }

  // message: an SSE message ({data, lastEventId}). Returns
  //   {kind: "event", event}  a protocol event to show;
  //   {kind: "duplicate"}     an event already shown, sent again after a reconnect;
  //   {kind: "disconnected"}  the connection dropped: EventSource's own `error`
  //                           event, which has no data.
  function read(message, state) {
    if (message.data === undefined || message.data === null) return { kind: "disconnected" };
    const id = message.lastEventId;
    if (typeof id === "string" && /^\d+$/.test(id)) {
      const position = Number(id);
      if (position <= state.seen) return { kind: "duplicate" };
      state.seen = position;
    }
    return { kind: "event", event: JSON.parse(message.data) };
  }

  global.VesselStream = { start, read };
})(window);
