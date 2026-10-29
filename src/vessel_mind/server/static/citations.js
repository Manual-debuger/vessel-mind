// Citations: maps the [n] in an answer to the search_docs result numbered n.
// Pure functions, no DOM, so a test can run them outside a browser.
(function (global) {
  "use strict";

  // [1], [2, 3]. A group with a number no result has stays plain text.
  const GROUP = /\[(\d+(?:\s*,\s*\d+)*)\]/g;
  const LINKABLE = "https://github.com/";

  // The results of one search_docs call -> Map(n -> {n, source, url}).
  // A result whose url is not on GitHub is never linked.
  function fromResults(results) {
    const map = new Map();
    for (const r of results || []) {
      if (Number.isInteger(r.n) && typeof r.url === "string" && r.url.startsWith(LINKABLE)) {
        map.set(r.n, { n: r.n, source: String(r.source), url: r.url });
      }
    }
    return map;
  }

  // text -> an array of strings and {n, source, url} citations, in order.
  function split(text, map) {
    const parts = [];
    let plain = "";
    let last = 0;
    for (const match of text.matchAll(GROUP)) {
      const cited = match[1].split(",").map((n) => map.get(Number(n.trim())));
      plain += text.slice(last, match.index);
      if (cited.every(Boolean)) {
        if (plain) parts.push(plain);
        plain = "";
        parts.push(...cited);
      } else {
        plain += match[0];
      }
      last = match.index + match[0].length;
    }
    plain += text.slice(last);
    if (plain) parts.push(plain);
    return parts;
  }

  global.VesselCitations = { fromResults, split };
})(window);
