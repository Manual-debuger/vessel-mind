// The route badge: the route a turn took, its models and their token usage.
// Pure functions, no DOM, so a test can run them outside a browser.
(function (global) {
  "use strict";

  function tokens(u) {
    return `in ${u.input_tokens}, out ${u.output_tokens}, thinking ${u.thinking_tokens}`;
  }

  // route: the turn's route event. done: its done event, or null while it runs.
  // The router's usage is the classification; done.usage is the answering model's.
  function describe(route, done) {
    const parts = [`${route.route} path (${route.reason})`];
    if (route.router_model !== null) {
      parts.push(`router ${route.router_model}: ${tokens(route.router_usage)}`);
    }
    parts.push(done ? `answer ${route.model}: ${tokens(done.usage)}` : `answer ${route.model}`);
    return parts.join(" · ");
  }

  global.VesselRoute = { describe };
})(window);
