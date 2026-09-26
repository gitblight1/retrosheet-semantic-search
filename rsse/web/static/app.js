/* RSSE local UI (spec/09-WEB.md).
 *
 * Plain JS, no framework, no build step. Two rules run through all of it:
 *
 * - Source text (event strings, comments, info records) comes from files and
 *   is only ever inserted as text: every element is built by `h()`, which
 *   uses text nodes, and nothing here assigns innerHTML (§8).
 * - A count is never shown without what was searched. Every query result is
 *   rendered by `renderResult`, which puts the disclosure panel beside the
 *   count and words an empty result as "no matching plays in N games" (§4).
 */
"use strict";

// -- DOM ---------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const $app = () => document.getElementById("app");

function show(...children) {
  const app = $app();
  app.replaceChildren();
  append(app, children);
  window.scrollTo(0, 0);
}

// -- formatting ----------------------------------------------------------------

const fmt = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"));
const POS = {1: "P", 2: "C", 3: "1B", 4: "2B", 5: "3B", 6: "SS", 7: "LF",
             8: "CF", 9: "RF", 10: "DH", 11: "PH", 12: "PR"};
const pos = (p) => POS[p] || (p === 0 || p === null || p === undefined ? "" : String(p));

function bases(b) {
  if (b === null || b === undefined) return h("span", {class: "muted"}, "unknown");
  if (b === "000") return h("span", {class: "bases muted"}, "empty");
  return h("span", {class: "bases", title: `bases ${b}`},
           [...b].map((c, i) => (c === "1" ? String(i + 1) : "–")).join(" "));
}

function half(halfName, inning) {
  return `${halfName === "top" ? "Top" : "Bot"} ${inning}`;
}

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function personName(p) {
  if (!p) return null;
  return [p.nickname || p.first, p.last].filter(Boolean).join(" ") || p.person_id;
}

const link = (href, text, cls) => h("a", {href, class: cls}, text);
const playerLink = (id, name) => (id ? link(`#/player/${encodeURIComponent(id)}`, name || id) : "—");
const teamLink = (id, season, text) => (id ? link(season ? `#/team/${encodeURIComponent(id)}/${season}` : `#/team/${encodeURIComponent(id)}`, text || id) : "—");
const gameLink = (key, text) => link(`#/game/${key}`, text);
const dateLink = (d) => (d ? link(`#/date/${d}`, d) : "—");

function isMissing(x) {
  return x && typeof x === "object" && !Array.isArray(x) && "missing_table" in x;
}

function missingNote(x) {
  return h("p", {class: "banner"},
           `Not available: this database has no ${x.missing_table}. `,
           x.build ? ["Build it with ", h("code", {}, x.build), "."] : "");
}

function table(headers, rows, opts = {}) {
  return h("div", {class: "table-wrap"},
    h("table", {class: opts.class},
      h("thead", {}, h("tr", {}, headers.map((c) =>
        h("th", {class: typeof c === "object" ? c.cls : null},
          typeof c === "object" ? c.label : c)))),
      h("tbody", {}, rows)));
}

const td = (content, cls) => h("td", {class: cls}, content);

function kv(pairs) {
  return h("dl", {class: "kv"}, pairs.filter(([, v]) => v !== undefined).map(([k, v]) =>
    [h("dt", {}, k), h("dd", {}, v === null || v === "" ? h("span", {class: "muted"}, "—") : v)]));
}

// -- API ----------------------------------------------------------------------

class HttpError extends Error {
  constructor(status, body) {
    super(body && body.error ? body.error : `HTTP ${status}`);
    this.status = status;
    this.body = body || {};
  }
}

async function request(method, path, body, signal) {
  const res = await fetch(path, {
    method,
    headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch (e) { data = null; }
  if (!res.ok) throw new HttpError(res.status, data);
  return data;
}

const get = (path) => request("GET", path);
const post = (path, body, signal) => request("POST", path, body, signal);

let SCHEMA = null;
async function schema() {
  if (!SCHEMA) SCHEMA = await get("/api/schema");
  return SCHEMA;
}

// -- query parameters <-> URL (§3: every view is a URL) ------------------------

const PAGE_KEYS = ["limit", "offset", "order"];

function parseQuery(qs, sch) {
  const sp = new URLSearchParams(qs);
  const params = {};
  const kinds = Object.fromEntries(sch.params.map((p) => [p.name, p.kind]));
  for (const key of new Set(sp.keys())) {
    if (PAGE_KEYS.includes(key)) continue;
    const kind = kinds[key];
    // Unknown keys are passed through so the server refuses them by name,
    // rather than the page dropping them and widening the query (§2).
    if (kind === "list") params[key] = sp.getAll(key);
    else if (kind === "switch") params[key] = true;
    else params[key] = sp.get(key);
  }
  const page = {
    limit: Number(sp.get("limit")) || sch.page_sizes[0],
    offset: Number(sp.get("offset")) || 0,
    order: sp.get("order") || "chronological",
  };
  return {params, page};
}

function buildQuery(params, page) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, x));
    else if (v === true) sp.append(k, "1");
    else if (v !== "" && v !== null && v !== undefined) sp.append(k, v);
  }
  if (page) {
    if (page.limit && page.limit !== SCHEMA.page_sizes[0]) sp.set("limit", page.limit);
    if (page.offset) sp.set("offset", page.offset);
    if (page.order && page.order !== "chronological") sp.set("order", page.order);
  }
  return sp.toString();
}

const queryHref = (params, page) => `#/query?${buildQuery(params, page)}`;

// -- name autocomplete (§3.5) -----------------------------------------------------

function autocomplete(input, box, kinds, onPick) {
  let timer = null, items = [], active = -1, seq = 0;

  function close() { box.hidden = true; box.replaceChildren(); items = []; active = -1; }

  function render(data) {
    box.replaceChildren();
    items = [];
    const labels = {people: "People", teams: "Teams", parks: "Parks"};
    for (const kind of kinds) {
      const hits = data[kind] || [];
      if (!hits.length) continue;
      box.appendChild(h("div", {class: "kind"}, labels[kind]));
      for (const hit of hits) {
        const a = h("a", {href: "#", onmousedown: (e) => { e.preventDefault(); pick(kind, hit); }},
                    hit.label, hit.detail ? h("span", {class: "detail"}, hit.detail) : null,
                    h("span", {class: "detail mono"}, hit.id));
        items.push({a, kind, hit});
        box.appendChild(a);
      }
    }
    if (!items.length) box.appendChild(h("div", {class: "kind"}, "No matches"));
    box.hidden = false;
  }

  function pick(kind, hit) { close(); onPick(kind, hit); }

  function setActive(i) {
    items.forEach((it, j) => it.a.classList.toggle("active", j === i));
    active = i;
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const data = await get(`/api/names?q=${encodeURIComponent(q)}`);
        if (mine === seq) render(data);
      } catch (e) { close(); }
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (box.hidden || !items.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActive(Math.min(active + 1, items.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive(Math.max(active - 1, 0)); }
    else if (e.key === "Enter" && active >= 0) { e.preventDefault(); pick(items[active].kind, items[active].hit); }
    else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 150));
}

function setupNameBox() {
  const input = document.getElementById("namebox");
  const box = document.getElementById("namehits");
  const routes = {people: "player", teams: "team", parks: "park"};
  autocomplete(input, box, ["people", "teams", "parks"], (kind, hit) => {
    input.value = "";
    location.hash = `#/${routes[kind]}/${encodeURIComponent(hit.id)}`;
  });
}

// -- query page ------------------------------------------------------------------

const GROUPS = [
  ["context", "Situation"], ["play", "The play"], ["fielding", "Fielding & outs"],
  ["players", "Players"], ["games", "Games & seasons"], ["quality", "Quality filters"],
  ["game_type", "Game types"],
];

//: Id fields that get a name picker; choosing a name puts the *id* in the
//: form, so a query built this way is never ambiguous (§7).
const PICKERS = {batter: "people", team: "teams", batting_team: "teams",
                 fielding_team: "teams", park: "parks"};

let CURRENT = null;     // the running search, so navigation can cancel it

function cancelCurrent() {
  if (!CURRENT) return;
  const {id, controller} = CURRENT;
  CURRENT = null;
  controller.abort();
  if (id) post(`/api/cancel/${id}`).catch(() => {});
}

function queryForm(sch, params, page, onSubmit) {
  const byGroup = {};
  for (const p of sch.params) (byGroup[p.group] = byGroup[p.group] || []).push(p);
  const inputs = {};
  let tagList = Array.isArray(params.tag) ? [...params.tag] : (params.tag ? [params.tag] : []);

  function field(p) {
    const id = `f-${p.name}`;
    const value = params[p.name];
    const label = p.name.replace(/_/g, " ");
    if (p.kind === "switch") {
      const input = h("input", {type: "checkbox", id, checked: !!value});
      inputs[p.name] = input;
      return h("div", {class: "field switch"}, input, h("label", {for: id, title: p.flag}, label),
               p.help ? h("div", {class: "help"}, p.help) : null);
    }
    if (p.kind === "list") {
      const chips = h("div", {class: "chips"});
      const opts = sch.tags.filter((t) => t.in_database).map((t) => h("option", {value: t.name}, t.category));
      const dl = h("datalist", {id: "taglist"}, opts);
      const input = h("input", {id, list: "taglist", placeholder: "type a tag, press Enter"});
      const renderChips = () => {
        chips.replaceChildren(...tagList.map((t, i) => h("span", {class: "chip"}, t,
          h("button", {type: "button", title: "remove", onclick: () => { tagList.splice(i, 1); renderChips(); }}, "×"))));
      };
      const add = () => {
        const v = input.value.trim();
        if (v && !tagList.includes(v)) tagList.push(v);
        input.value = "";
        renderChips();
      };
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } });
      input.addEventListener("change", () => { if (sch.tags.some((t) => t.name === input.value)) add(); });
      renderChips();
      inputs[p.name] = {get value() { add(); return tagList; }};
      return h("div", {class: "field"}, h("label", {for: id, title: p.flag}, label), input, dl, chips,
               h("div", {class: "help"}, p.help, " · ", link("#/tags", "all tags")));
    }
    let input;
    if (p.kind === "choice") {
      input = h("select", {id}, h("option", {value: ""}, ""),
                p.choices.map((c) => h("option", {value: c}, c)));
      input.value = value || "";
    } else {
      input = h("input", {id, type: p.kind === "int" ? "number" : "text", value: value || "",
                          spellcheck: "false", autocomplete: "off"});
    }
    inputs[p.name] = input;
    const wrap = h("div", {class: "field"}, h("label", {for: id, title: p.flag}, label));
    if (PICKERS[p.name]) {
      const box = h("div", {class: "hits", hidden: true});
      const holder = h("div", {class: "search"}, input, box);
      autocomplete(input, box, [PICKERS[p.name]], (kind, hit) => { input.value = hit.id; });
      wrap.appendChild(holder);
    } else {
      wrap.appendChild(input);
    }
    if (p.help) wrap.appendChild(h("div", {class: "help"}, p.help));
    return wrap;
  }

  const groups = GROUPS.filter(([g]) => byGroup[g]).map(([g, title]) => {
    const set = byGroup[g].some((p) => p.name in params);
    return h("fieldset", {class: set ? "set" : null}, h("legend", {}, title), byGroup[g].map(field));
  });

  const limit = h("select", {"aria-label": "page size"},
                  sch.page_sizes.map((n) => h("option", {value: n}, `${n} per page`)));
  limit.value = String(page.limit);
  const order = h("select", {"aria-label": "order"},
                  sch.orders.map((o) => h("option", {value: o}, o)));
  order.value = page.order;

  function collect() {
    const out = {};
    for (const [name, input] of Object.entries(inputs)) {
      if (input.type === "checkbox") { if (input.checked) out[name] = true; }
      else {
        const v = input.value;
        if (Array.isArray(v)) { if (v.length) out[name] = v; }
        else if (String(v).trim() !== "") out[name] = String(v).trim();
      }
    }
    return out;
  }

  const form = h("form", {onsubmit: (e) => {
    e.preventDefault();
    onSubmit(collect(), {limit: Number(limit.value), offset: 0, order: order.value});
  }},
    h("div", {class: "form-groups"}, groups),
    h("div", {class: "actions"},
      h("button", {class: "primary", type: "submit"}, "Search"),
      limit, order,
      h("button", {type: "button", onclick: () => { location.hash = "#/query"; }}, "Clear")));
  return form;
}

async function pageQuery(qs, isHome) {
  const sch = await schema();
  const {params, page} = parseQuery(qs, sch);
  const hasQuery = Object.keys(params).length > 0;
  const resultArea = h("div", {});

  const form = queryForm(sch, params, page, (p, pg) => {
    const href = queryHref(p, pg);
    if (location.hash === href) runQuery(p, pg, resultArea);
    else location.hash = href;
  });

  // With a query in the URL the results matter more than the form, so the
  // form folds away behind a one-line statement of what is being asked.
  const summary = h("summary", {}, hasQuery
    ? [h("strong", {}, "Asking: "), h("span", {class: "chips inline"}, querySummary(params, sch))]
    : "Query builder");
  const holder = h("details", {class: "query-form", open: !hasQuery}, summary, form);
  show(
    isHome ? homeIntro() : h("h1", {}, "Query"),
    holder,
    resultArea,
  );
  if (hasQuery) runQuery(params, page, resultArea);
}

function querySummary(params, sch) {
  const kinds = Object.fromEntries(sch.params.map((p) => [p.name, p.kind]));
  return Object.entries(params).flatMap(([k, v]) => {
    const label = k.replace(/_/g, " ");
    if (kinds[k] === "switch") return [h("span", {class: "chip"}, label)];
    if (Array.isArray(v)) return v.map((x) => h("span", {class: "chip"}, `${label} ${x}`));
    return [h("span", {class: "chip"}, `${label} ${v}`)];
  });
}

function homeIntro() {
  const ex = [
    ["Bases loaded, two out, dropped third strike, catcher to pitcher, force at home",
     {bases_loaded: true, outs: "2", dropped_third: true, force_play_at: "H", putout_sequence: "2,1"}],
    ["Babe Ruth's 1927 home runs", {batter: "ruthb101", season: "1927", tag: ["HomeRun"]}],
    ["Walk-off inside-the-park home runs", {walkoff: true, tag: ["InsideTheParkHomeRun"]}],
    ["Triple plays, 2000–2025", {triple_play: true, seasons: "2000,2025"}],
    ["“Jack Robinson” — an ambiguous name", {batter_named: "Jack Robinson"}],
  ];
  return h("section", {},
    h("h1", {}, "Retrosheet Semantic Search"),
    h("p", {class: "muted"},
      "Every play Retrosheet has published, searchable in baseball terms. ",
      "Results always say what was searched, so an empty result means “not in these games”, not “never happened”."),
    h("h2", {}, "Try"),
    h("ul", {class: "examples"}, ex.map(([label, p]) => h("li", {}, link(queryHref(p), label)))),
    h("h2", {}, "Build a query"));
}

async function runQuery(params, page, area) {
  cancelCurrent();
  const body = {params, limit: page.limit, offset: page.offset, order: page.order};
  const controller = new AbortController();
  const started = performance.now();
  const clock = h("span", {class: "muted"}, "0.0 s");
  const cancel = h("button", {type: "button", onclick: () => {
    cancelCurrent();
    area.replaceChildren(h("p", {class: "banner"}, "Cancelled."));
  }}, "Cancel");
  area.replaceChildren(h("div", {class: "running"}, h("div", {class: "spinner"}), "Searching…", clock, cancel));
  const tick = setInterval(() => {
    clock.textContent = `${((performance.now() - started) / 1000).toFixed(1)} s`;
  }, 100);
  const mine = {id: null, controller};
  CURRENT = mine;
  try {
    const {id} = await post("/api/query/start", {}, controller.signal);
    mine.id = id;
    const result = await post(`/api/query/${id}`, body, controller.signal);
    if (CURRENT !== mine) return;
    area.replaceChildren(renderResult(result, body));
  } catch (err) {
    if (err.name === "AbortError" || CURRENT !== mine) return;
    area.replaceChildren(renderQueryError(err, params, page, body));
  } finally {
    clearInterval(tick);
    if (CURRENT === mine) CURRENT = null;
  }
}

function renderQueryError(err, params, page, body) {
  const b = err.body || {};
  if (err.status === 409 && b.candidates) {
    // An ambiguous name: offer the people it could mean (§7).
    const replaceWith = {person: ["batter_named", "batter"], park: ["park_named", "park"],
                         team: ["team_named", "team"]};
    return h("div", {class: "banner candidates"},
      h("p", {}, `${b.candidates.length} matches for that name. Which did you mean?`),
      b.candidates.map((c) => {
        const [from, to] = replaceWith[c.kind];
        const next = {...params};
        delete next[from];
        next[to] = c.id;
        return h("button", {type: "button", onclick: () => { location.hash = queryHref(next, page); }},
                 c.label, " ", h("span", {class: "muted mono"}, c.id));
      }));
  }
  const box = h("div", {class: "banner bad"}, h("p", {}, err.message));
  if (err.status === 504 || err.status === 503) {
    if (b.elapsed_ms) box.appendChild(h("p", {class: "muted"}, `stopped after ${(b.elapsed_ms / 1000).toFixed(1)} s`));
    box.appendChild(h("button", {type: "button", onclick: async () => {
      box.appendChild(await explainPanel(body));
    }}, "Explain this query"));
  }
  return box;
}

// -- the one render path for results (§4) ---------------------------------------

function seasonRange(cov) {
  if (!cov.seasons || !cov.seasons.length) return "no seasons";
  const lo = Math.min(...cov.seasons), hi = Math.max(...cov.seasons);
  return lo === hi ? String(lo) : `${lo}–${hi}`;
}

function headline(result) {
  const cov = result.coverage;
  if (result.total === 0) {
    // Never "no results": the count is always bounded by what was searched.
    return h("span", {class: "count"},
             `No matching plays in ${fmt(cov.games)} games, ${seasonRange(cov)}`);
  }
  const first = result.offset + 1, last = result.offset + result.rows.length;
  return [h("span", {class: "count"}, `${fmt(result.total)} matching play${result.total === 1 ? "" : "s"}`),
          h("span", {class: "muted"}, result.rows.length < result.total ? `showing ${fmt(first)}–${fmt(last)}` : "",
            ` · ${(result.elapsed_ms / 1000).toFixed(2)} s`)];
}

function disclosure(result) {
  const ex = result.excluded;
  const params = result.params;
  const page = {limit: result.limit, order: "chronological"};
  const rerun = (flag) => {
    const next = {...params};
    next[flag] = true;
    return link(queryHref(next, page), `include them`);
  };
  const exItems = [];
  if (ex.uncertain_tags) exItems.push([`${fmt(ex.uncertain_tags)} with uncertain tags`, rerun("include_uncertain")]);
  if (ex.untrusted_state) exItems.push([`${fmt(ex.untrusted_state)} on an untrusted base state`, rerun("include_untrusted")]);
  if (ex.unparsed) exItems.push([`${fmt(ex.unparsed)} unparsed`, rerun("include_unparsed")]);
  if (ex.other_status) exItems.push([`${fmt(ex.other_status)} with another parse status`, rerun("include_unparsed")]);
  if (ex.curated) exItems.push([`${fmt(ex.curated)} curated`, null]);
  const cov = result.coverage;
  return h("aside", {class: "disclosure", "aria-label": "what was searched"},
    h("h3", {}, "Searched"),
    h("p", {}, result.describe.coverage),
    cov.notes && cov.notes.length ? h("ul", {}, cov.notes.map((n) => h("li", {}, n))) : null,
    h("h3", {}, "Excluded"),
    exItems.length ? h("ul", {}, exItems.map(([t, a]) => h("li", {}, t, a ? [" — ", a] : null)))
                   : h("p", {}, "nothing excluded"),
    result.describe.force ? [h("h3", {}, "Force certainty"), h("p", {}, result.describe.force)] : null,
    h("h3", {}, "Versions"),
    h("p", {class: "small"}, `corpus ${result.corpus_version}; ontology ${result.ontology_version}`));
}

function resultRows(rows) {
  return rows.map((r) => h("tr", {class: r.parse_status !== "ok" ? "bad" : null},
    td(dateLink(r.date), "nowrap"),
    td(link(`#/game/id/${encodeURIComponent(r.game_id)}`, r.game_id), "mono"),
    td(half(r.half, r.inning), "nowrap"),
    td(r.outs_before === null ? h("span", {class: "muted"}, "unknown") : [`${r.outs_before} out, `, bases(r.bases_before)], "nowrap"),
    td(playerLink(r.batter_id, r.batter_name)),
    td(link(`#/play/${r.play_id}`, r.event_raw), "event"),
    td(h("div", {class: "tags"}, r.tags.map((t) => h("span", {class: "tag"}, t))))));
}

function renderResult(result, body) {
  const page = {limit: result.limit, offset: result.offset, order: body.order};
  const params = result.params;
  const tabs = h("div", {class: "tabs"});
  const panel = h("div", {});
  const pager = h("div", {class: "pager"});
  if (result.total > result.rows.length) {
    const prev = result.offset > 0 ? link(queryHref(params, {...page, offset: Math.max(0, result.offset - result.limit)}), "← previous") : null;
    const next = result.offset + result.rows.length < result.total ? link(queryHref(params, {...page, offset: result.offset + result.limit}), "next →") : null;
    append(pager, [prev, next]);
  }
  const exportQ = encodeURIComponent(JSON.stringify(body));
  const views = {
    Results: () => [
      result.rows.length ? table(["Date", "Game", "Inn", "State", "Batter", "Event", "Tags"], resultRows(result.rows)) : null,
      pager,
    ],
    Explain: () => explainPanel(body),
    Export: () => h("div", {},
      h("p", {}, "This page of results, with the Retrosheet attribution notice and the corpus version:"),
      h("div", {class: "actions"},
        h("a", {href: `/api/export?format=csv&q=${exportQ}`, download: "rsse-query.csv"}, h("button", {type: "button"}, "Download CSV")),
        h("a", {href: `/api/export?format=json&q=${exportQ}`, download: "rsse-query.json"}, h("button", {type: "button"}, "Download JSON"))),
      h("p", {}, "For more than one page, the same query from the command line:"),
      h("pre", {}, result.command.replace(/--limit \d+/, `--limit ${result.total || 1}`) + " --format csv")),
  };
  const select = async (name) => {
    [...tabs.children].forEach((b) => b.classList.toggle("on", b.textContent === name));
    panel.replaceChildren(h("p", {class: "muted"}, "…"));
    const content = await views[name]();
    panel.replaceChildren();
    append(panel, [content]);
  };
  for (const name of Object.keys(views)) {
    tabs.appendChild(h("button", {type: "button", onclick: () => select(name)}, name));
  }
  select("Results");
  return h("section", {},
    h("div", {class: "result-grid"},
      h("div", {}, h("div", {class: "headline"}, headline(result)), tabs, panel),
      disclosure(result)));
}

async function explainPanel(body) {
  try {
    const info = await post("/api/explain", body);
    return h("div", {},
      info.warnings.map((w) => h("p", {class: "banner"}, w)),
      h("h3", {}, "SQL"), h("pre", {}, info.sql),
      h("h3", {}, "Parameters"), h("pre", {}, JSON.stringify(info.params)),
      h("h3", {}, "Plan"), h("pre", {}, info.plan.map((r) => r[r.length - 1]).join("\n")));
  } catch (err) {
    return h("p", {class: "banner bad"}, err.message);
  }
}

// -- play (§3.1) ------------------------------------------------------------------

async function pagePlay(id) {
  const d = await get(`/api/play/${encodeURIComponent(id)}`);
  const p = d.play, g = d.game;
  const names = d.names || {};
  const battingName = p.batting_team === 0 ? (g.away_name || g.away_team) : (g.home_name || g.home_team);
  const nav = h("div", {class: "pager"},
    d.prev ? link(`#/play/${d.prev}`, "← previous play") : null,
    gameLink(p.game_key, "whole game"),
    d.next ? link(`#/play/${d.next}`, "next play →") : null);

  const lineupSection = () => {
    if (isMissing(d.lineup)) return missingNote(d.lineup);
    const side = (team, label) => h("div", {},
      h("h3", {}, label),
      table(["Pos", "Player"], d.lineup[team].defense.map((e) =>
        h("tr", {}, td(pos(e.position)), td(playerLink(e.player_id, e.player_name))))));
    const fielding = 1 - p.batting_team;
    return [h("p", {class: "muted small"}, "Who held each position at this play, by the lineup timeline (the last entry that had taken effect), not by who touched the ball."),
      h("div", {class: "cols"},
        side(fielding, `Fielding: ${fielding === 0 ? g.away_name || g.away_team : g.home_name || g.home_team}`),
        side(p.batting_team, `Batting: ${battingName}`))];
  };

  show(
    h("p", {class: "muted"}, dateLink(g.date), " · ", gameLink(p.game_key, `${g.away_name || g.away_team} at ${g.home_name || g.home_team}`),
      " · ", half(p.half, p.inning), ` · play ${p.seq}`),
    h("h1", {class: "mono"}, p.event_raw),
    p.parse_status !== "ok" ? h("p", {class: "banner bad"},
      `Parse status: ${p.parse_status}. `, p.parse_error || "",
      p.outs_before === null ? " The base-out state is unknown, not empty." : "") : null,
    nav,
    h("div", {class: "cols"},
      h("section", {}, h("h2", {}, "Situation"), kv([
        ["Batter", playerLink(p.batter_id, names[p.batter_id])],
        ["Batting", battingName],
        ["Outs before → after", p.outs_before === null ? "unknown" : `${p.outs_before} → ${p.outs_after}`],
        ["Bases before", bases(p.bases_before)],
        ["Bases after", bases(p.bases_after)],
        ["Runners before", [p.runner_1_before, p.runner_2_before, p.runner_3_before].map((r, i) =>
          r ? [`${i + 1}B `, playerLink(r, names[r]), " "] : null)],
        ["Score before (bat–field)", `${p.score_batting_before}–${p.score_fielding_before}`],
        ["Runs on play", p.runs_on_play],
        ["Batter ran", p.batter_ran],
        ["Flags", ["is_inning_ending", "is_final_play", "is_walkoff", "is_go_ahead"]
          .filter((k) => p[k]).map((k) => k.replace("is_", "").replace(/_/g, " ")).join(", ")],
      ])),
      h("section", {}, h("h2", {}, "As recorded"), kv([
        ["Event", h("code", {}, p.event_raw)],
        ["Basic", h("code", {}, p.event_basic)],
        ["Modifiers", h("code", {}, p.event_modifiers)],
        ["Advances", h("code", {}, p.event_advances)],
        ["Annotations", h("code", {}, p.annotations)],
        ["Location", p.event_location],
        ["Count", p.count_balls === null ? null : `${p.count_balls}-${p.count_strikes}`],
        ["Pitches", p.pitch_seq ? h("code", {}, p.pitch_seq) : null],
        ["Source", isMissing(d.source) ? h("span", {class: "muted"}, `no archive (${d.source.build})`)
                                       : d.source ? [h("code", {}, d.source.raw_line), h("div", {class: "small muted"}, `${d.source.path}:${d.source.line_no}`)] : null],
        ["Parser", `${p.parser_version}, status ${p.parse_status}`],
      ]))),
    h("h2", {}, "Tags"),
    d.tags.length ? table(["Tag", "Category", "Confidence", "Source"], d.tags.map((t) => h("tr", {},
      td(link(queryHref({tag: [t.name]}), t.name)), td(t.category), td(t.confidence), td(t.source))))
      : h("p", {class: "muted"}, "No tags."),
    h("h2", {}, "Runner advances"),
    d.advances.length ? table(["#", "Runner", "From", "To", "Out", "Force", "Certainty", "Scored", "Raw"], d.advances.map((a) => h("tr", {},
      td(a.seq), td(a.runner_id ? playerLink(a.runner_id, names[a.runner_id]) : "—"), td(a.origin), td(a.destination),
      td(a.is_out ? "out" : ""), td(a.is_force ? "force" : ""), td(a.force_certainty), td(a.scored ? "run" : ""),
      td(a.raw, "event")))) : h("p", {class: "muted"}, "None."),
    h("h2", {}, "Fielding"),
    d.credits.length ? table(["Scope", "Seq", "Order", "Fielder", "Credit"], d.credits.map((c) => h("tr", {},
      td(c.scope), td(c.scope_seq), td(c.pos_in_seq), td(`${pos(c.fielder)} (${c.fielder ?? "?"})`), td(c.credit))))
      : h("p", {class: "muted"}, "No fielding credits."),
    d.sequences.length ? [h("h3", {}, "Throw sequences"), table(["Scope", "Seq", "Sequence", "Error", "Out"], d.sequences.map((s) => h("tr", {},
      td(s.scope), td(s.scope_seq), td(link(queryHref({putout_sequence: s.seq_text.split("").join(",")}), s.seq_text), "mono"),
      td(s.has_error ? "yes" : ""), td(s.records_out ? "yes" : ""))))] : null,
    h("h2", {}, "Comments"),
    isMissing(d.comments) ? missingNote(d.comments)
      : d.comments.length ? h("ul", {}, d.comments.map((c) => h("li", {}, c.kind !== "text" ? h("span", {class: "tag"}, c.kind) : null, " ", c.text)))
      : h("p", {class: "muted"}, "None attached to this play."),
    h("h2", {}, "Lineup at this play"),
    lineupSection());
}

// -- game (§3.2) ------------------------------------------------------------------

async function pageGameById(gameId) {
  const d = await get(`/api/game/id/${encodeURIComponent(gameId)}`);
  if (d.games.length === 1) { location.replace(`#/game/${d.games[0].game_key}`); return; }
  show(h("h1", {}, gameId),
    d.games.length ? [h("p", {class: "banner"}, `${d.games.length} games carry this id. They are listed rather than one being picked.`),
      table(["Occurrence", "Date", "Game", "Score", "Plays"], d.games.map((g) => h("tr", {},
        td(g.occurrence), td(g.date), td(gameLink(g.game_key, `${g.away_team} at ${g.home_team}`)),
        td(`${g.final_away ?? "?"}–${g.final_home ?? "?"}`), td(g.plays, "num"))))]
      : h("p", {}, "No game has that id."));
}

async function pageGame(key) {
  const d = await get(`/api/game/${encodeURIComponent(key)}`);
  const g = d.game;
  const away = g.away_name || g.away_team, home = g.home_name || g.home_team;
  const inGameNames = {};
  if (Array.isArray(d.lineup)) d.lineup.forEach((e) => { inGameNames[e.player_id] = e.player_name; });
  const nameOf = (id) => inGameNames[id] || (d.names || {})[id] || id;

  const ls = d.line_score;
  const inningsHead = [...Array(ls.innings).keys()].map((i) => ({label: String(i + 1), cls: "num"}));
  const lineRow = (label, runs, total, final) => h("tr", {}, td(label),
    runs.map((r) => td(r === null ? "x" : r)), td(h("strong", {}, total)), td(final ?? "—"));
  const logs = Array.isArray(d.game_log) ? d.game_log : [];

  const pbp = [];
  const subsAfter = {}, commentsAfter = {}, preGame = [];
  if (Array.isArray(d.lineup)) d.lineup.filter((e) => e.is_sub).forEach((e) => (subsAfter[e.play_id] = subsAfter[e.play_id] || []).push(e));
  if (Array.isArray(d.comments)) d.comments.forEach((c) => (c.play_id ? (commentsAfter[c.play_id] = commentsAfter[c.play_id] || []).push(c) : preGame.push(c)));
  preGame.forEach((c) => pbp.push(h("tr", {class: "note comment"}, h("td", {colspan: 7}, c.text))));
  let lastHalf = null;
  for (const p of d.plays) {
    const key = `${p.inning}-${p.half}`;
    if (key !== lastHalf) {
      lastHalf = key;
      pbp.push(h("tr", {class: "half-head"}, h("td", {colspan: 7},
        `${p.half === "top" ? "Top" : "Bottom"} of the ${ordinal(p.inning)} — ${p.batting_team === 0 ? away : home} batting`)));
    }
    const [a, hm] = p.batting_team === 0
      ? [p.score_batting_before + p.runs_on_play, p.score_fielding_before]
      : [p.score_fielding_before, p.score_batting_before + p.runs_on_play];
    pbp.push(h("tr", {class: p.parse_status !== "ok" ? "bad" : null},
      td(playerLink(p.batter_id, nameOf(p.batter_id))),
      td(p.outs_before === null ? "?" : p.outs_before, "num"),
      td(bases(p.bases_before)),
      td(p.pitch_seq ? h("span", {class: "mono small"}, p.pitch_seq) : (p.count_balls !== null ? `${p.count_balls}-${p.count_strikes}` : "")),
      td(link(`#/play/${p.play_id}`, p.event_raw), "event"),
      td(p.runs_on_play ? h("strong", {}, `+${p.runs_on_play}`) : "", "num"),
      td(`${a}–${hm}`, "num")));
    for (const s of subsAfter[p.play_id] || []) {
      pbp.push(h("tr", {class: "note"}, h("td", {colspan: 7},
        `${s.team === 0 ? g.away_team : g.home_team}: `, playerLink(s.player_id, s.player_name),
        ` enters — ${pos(s.position)}${s.batting_order ? `, batting ${ordinal(s.batting_order)}` : ""}`)));
    }
    for (const c of commentsAfter[p.play_id] || []) {
      pbp.push(h("tr", {class: "note comment"}, h("td", {colspan: 7}, c.kind !== "text" ? h("span", {class: "tag"}, c.kind) : null, " ", c.text)));
    }
  }

  const starters = (team) => table(["#", "Player", "Pos"], (Array.isArray(d.lineup) ? d.lineup : [])
    .filter((e) => !e.is_sub && e.team === team)
    .map((e) => h("tr", {}, td(e.batting_order || ""), td(playerLink(e.player_id, e.player_name)), td(pos(e.position)))));

  const er = d.earned_runs;
  show(
    h("p", {class: "muted"}, dateLink(g.date), g.game_number ? ` · game ${g.game_number}` : "", " · ",
      g.site ? link(`#/park/${encodeURIComponent(g.site)}`, g.site_name || g.site) : "park unknown",
      " · ", g.game_type ? g.game_type : "game type not stated", " · ", h("span", {class: "mono"}, g.game_id),
      g.occurrence > 1 ? ` (occurrence ${g.occurrence})` : ""),
    h("h1", {}, teamLink(g.away_team, g.season, away), ` ${g.final_away ?? "?"} at `,
      teamLink(g.home_team, g.season, home), ` ${g.final_home ?? "?"}`),
    g.parse_status !== "ok" ? h("p", {class: "banner bad"}, `Game parse status: ${g.parse_status}`) : null,
    ls.agrees === false ? h("p", {class: "banner bad"},
      `The plays sum to ${ls.away_total}–${ls.home_total}, but the stored final is ${g.final_away}–${g.final_home}. Both are shown; neither is chosen.`) : null,
    h("h2", {}, "Line score"),
    table([{label: ""}, ...inningsHead, {label: "R"}, {label: "Final", cls: "num"}], [
      lineRow(away, ls.away, ls.away_total, ls.final_away),
      lineRow(home, ls.home, ls.home_total, ls.final_home),
      ...logs.map((l) => h("tr", {class: "note"}, h("td", {colspan: ls.innings + 1}, `Game log (${l.series})`),
        td(""), td(`${l.away_score}–${l.home_score}`))),
    ], {class: "linescore"}),
    isMissing(d.game_log) ? h("p", {class: "small muted"}, `Game logs not available (${d.game_log.build}).`)
      : !logs.length ? h("p", {class: "small muted"}, "The game logs have no row for this game.")
      : logs.some((l) => l.away_score !== g.final_away || l.home_score !== g.final_home)
        ? h("p", {class: "banner"}, "The game log's score differs from the event file's.") : null,
    h("h2", {}, "Play by play"),
    table(["Batter", {label: "Outs", cls: "num"}, "Bases", "Pitches", "Event", {label: "R", cls: "num"}, {label: "Score", cls: "num"}], pbp),
    h("h2", {}, "Starting lineups"),
    isMissing(d.lineup) ? missingNote(d.lineup)
      : h("div", {class: "cols"}, h("div", {}, h("h3", {}, away), starters(0)), h("div", {}, h("h3", {}, home), starters(1))),
    h("h2", {}, "Earned runs"),
    isMissing(er) ? missingNote(er) : !er.length ? h("p", {class: "muted"}, "No runs scored.") : [
      h("p", {class: "small muted"}, "Derived, not copied: “scorer’s judgement” counts runs the rules leave to the official scorer — they are not zeros."),
      table(["Pitcher charged", {label: "Runs", cls: "num"}, {label: "Earned", cls: "num"}, {label: "Unearned", cls: "num"},
             {label: "Scorer's judgement", cls: "num"}, "Certainty"],
        er.map((r) => h("tr", {}, td(playerLink(r.pitcher_id, nameOf(r.pitcher_id))), td(r.runs, "num"), td(r.earned, "num"),
          td(r.unearned, "num"), td(r.judgement, "num"),
          td(["derived", "likely", "ambiguous", "untrusted"].filter((k) => r[k]).map((k) => `${r[k]} ${k}`).join(", ")))))],
    h("h2", {}, "Game information"),
    h("details", {class: "more"}, h("summary", {}, `${d.info.length} info records, as written`),
      table(["Key", "Value"], d.info.map((i) => h("tr", {}, td(i.key, "mono"), td(i.value)))))
  );
}

// -- player (§3.3) ----------------------------------------------------------------

const TAG_COLUMNS = ["Single", "Double", "Triple", "HomeRun", "Walk", "IntentionalWalk", "HitByPitch",
                     "Strikeout", "SacrificeHit", "SacrificeFly", "ReachedOnError", "DoublePlay"];

async function pagePlayer(id) {
  const d = await get(`/api/player/${encodeURIComponent(id)}`);
  const p = isMissing(d.person) ? null : d.person;
  const name = personName(p) || id;
  const counts = d.tag_counts.counts;
  const seasons = [...new Set(d.batting_seasons.map((s) => s.season))];
  const cols = TAG_COLUMNS.filter((t) => counts[t]);

  const apps = d.appearances;
  const appsTable = isMissing(apps) ? missingNote(apps) : !apps.length ? null :
    table(["Season", "Team", {label: "Games (Retrosheet)", cls: "num"}, {label: "In corpus", cls: "num"}, {label: "Share", cls: "num"}, "Positions"],
      apps.map((a) => {
        const posCols = ["p", "c", "1b", "2b", "3b", "ss", "lf", "cf", "rf", "of", "dh", "ph", "pr"]
          .filter((k) => a[`g_${k}`]).map((k) => `${k.toUpperCase()} ${a[`g_${k}`]}`).join(", ");
        return h("tr", {}, td(a.season), td(teamLink(a.team_id, a.season)), td(a.g, "num"), td(a.games_in_corpus, "num"),
          td(a.g ? [`${Math.round((100 * a.games_in_corpus) / a.g)}%`, a.games_in_corpus > a.g ? " *" : ""] : "—", "num"), td(posCols, "small"));
      }));
  // 207 rows hold more games than the file counts; unexplained, and kept as
  // measured rather than capped (spec/05-DATABASE.md §9).
  const overCount = Array.isArray(apps) && apps.some((a) => a.games_in_corpus > a.g);

  show(
    h("h1", {}, name),
    p && p.source === "observed" ? h("p", {class: "banner"}, "The corpus names this person, but no Retrosheet reference file describes them.") : null,
    p ? kv([
      ["Id", h("span", {class: "mono"}, id)],
      ["Legal name", [p.first, p.last].filter(Boolean).join(" ")],
      ["Born", [p.birthdate, [p.birth_city, p.birth_state, p.birth_country].filter(Boolean).join(", ")].filter(Boolean).join(", ")],
      ["Died", p.deathdate],
      ["Played", p.play_debut ? `${p.play_debut} – ${p.play_last || ""}` : null],
      ["Managed", p.mgr_debut ? `${p.mgr_debut} – ${p.mgr_last || ""}` : undefined],
      ["Umpired", p.ump_debut ? `${p.ump_debut} – ${p.ump_last || ""}` : undefined],
    ]) : isMissing(d.person) ? missingNote(d.person) : null,
    h("div", {class: "actions"}, link(queryHref({batter: id}), "Search plays by this batter")),
    // `appearances` is allplayers.csv, which covers the Negro Leagues only
    // (spec/05-DATABASE.md §9); a major-league career has no rows, and an
    // empty table would read as "never appeared".
    appsTable ? [h("h2", {}, "Negro Leagues appearances"),
      h("p", {class: "small muted"}, "From Retrosheet's allplayers.csv, which covers the Negro Leagues, 1903–1962: games Retrosheet counted from box scores and newspapers, beside the games the event files hold. The gap is what the corpus lacks."),
      appsTable,
      overCount ? h("p", {class: "small muted"}, "* The corpus holds more games than the file counts. 207 rows do this, mostly 1933–34; the two sources appear to count different things, and neither number is adjusted (spec/05-DATABASE.md §9).") : null] : null,
    h("h2", {}, "Tag counts"),
    h("p", {class: "small muted"},
      "Plays this person batted in that carry each tag, under the default quality filters. ",
      h("strong", {}, "These are tag counts, not a batting line"), ": a season the corpus holds part of gives part of a number. Each count links to the query that reproduces it."),
    cols.length ? table(["Season", ...cols.map((c) => ({label: c, cls: "num"}))], [
      ...seasons.map((s) => h("tr", {}, td(link(queryHref({batter: id, season: String(s)}), s)),
        cols.map((c) => td(counts[c][s] ? link(queryHref({batter: id, season: String(s), tag: [c]}), fmt(counts[c][s])) : "", "num")))),
      h("tr", {}, td(h("strong", {}, "All")), cols.map((c) => td(link(queryHref({batter: id, tag: [c]}),
        fmt(Object.values(counts[c]).reduce((x, y) => x + y, 0))), "num"))),
    ]) : h("p", {class: "muted"}, "No plays as a batter in the corpus."),
    h("h2", {}, "Seasons in the corpus"),
    table(["Season", {label: "Plays as batter", cls: "num"}, {label: "Games in a lineup", cls: "num"}], (() => {
      const lineup = Array.isArray(d.lineup_seasons) ? Object.fromEntries(d.lineup_seasons.map((s) => [s.season, s.games])) : {};
      const all = [...new Set([...seasons, ...Object.keys(lineup).map(Number)])].sort();
      const plays = Object.fromEntries(d.batting_seasons.map((s) => [s.season, s.plays]));
      return all.map((s) => h("tr", {}, td(s), td(plays[s] ? link(queryHref({batter: id, season: String(s)}), fmt(plays[s])) : "", "num"), td(fmt(lineup[s]), "num")));
    })()),
    h("h2", {}, "Roster lines"),
    isMissing(d.roster) ? missingNote(d.roster) : table(["Season", "Team", "Position", "Bats", "Throws", "Name on roster"],
      d.roster.map((r) => h("tr", {}, td(r.season), td([teamLink(r.team_id, r.season), r.stated_team_id ? h("span", {class: "muted small"}, ` (line says ${r.stated_team_id})`) : null]),
        td(r.position || h("span", {class: "muted"}, "none")), td(r.bats), td(r.throws), td([r.first, r.last].filter(Boolean).join(" "))))));
}

// -- teams (§3.4) -----------------------------------------------------------------

async function pageTeam(id) {
  const d = await get(`/api/team/${encodeURIComponent(id)}`);
  const f = d.franchise && !isMissing(d.franchise) ? d.franchise : null;
  show(
    h("h1", {}, f ? `${f.city || ""} ${f.nickname || ""}`.trim() : id),
    f ? kv([["Id", id], ["League", f.league], ["Seasons", `${f.first_season ?? "?"}–${f.last_season ?? "?"}`]]) : null,
    h("div", {class: "actions"}, link(queryHref({team: id}), "Search plays involving this team")),
    h("h2", {}, "Seasons"),
    isMissing(d.seasons) ? missingNote(d.seasons) : table(["Season", "League", "Name"], d.seasons.map((s) => h("tr", {},
      td(teamLink(id, s.season, s.season)), td(s.league || h("span", {class: "muted"}, "none")), td(`${s.city || ""} ${s.nickname || ""}`)))));
}

async function pageTeamSeason(id, season) {
  const d = await get(`/api/team/${encodeURIComponent(id)}/${encodeURIComponent(season)}`);
  const t = d.team && !isMissing(d.team) ? d.team : null;
  const title = t ? `${t.city || ""} ${t.nickname || ""}`.trim() : id;
  const s = Number(season);
  const logs = d.game_logs;
  show(
    h("p", {class: "muted"}, link(`#/season/${s - 1}`, `← ${s - 1}`), " · ", link(`#/season/${s}`, `${s} season`), " · ",
      link(`#/team/${id}/${s + 1}`, `${s + 1} →`), " · ", teamLink(id, null, "all seasons")),
    h("h1", {}, `${s} ${title}`),
    t ? h("p", {}, t.league ? `League: ${t.league}` : "No league recorded for this club this season.") : null,
    h("p", {}, `${fmt(d.record.won)}–${fmt(d.record.lost)}${d.record.tied ? `–${d.record.tied}` : ""} `,
      h("span", {class: "muted"}, `(${d.record.note}; ${d.games.length} games)`)),
    isMissing(logs) ? h("p", {class: "banner"}, `Completeness unknown: the game logs are not loaded (${logs.build}).`) :
      logs.missing_games.length ? h("p", {class: "banner"}, `The game logs list ${logs.regular_listed} regular-season games for this club; ${logs.missing_games.length} of them have no event file in the corpus.`)
        : h("p", {class: "muted"}, `All ${logs.regular_listed} regular-season games the game logs list are in the corpus.`),
    h("div", {class: "actions"}, link(queryHref({team: id, season: String(s)}), "Search this season's plays")),
    h("h2", {}, "Games in the corpus"),
    table(["Date", "Opponent", "Score", "", "Type"], d.games.map((g) => {
      const homeGame = g.home_team === id;
      const mine = homeGame ? g.final_home : g.final_away, theirs = homeGame ? g.final_away : g.final_home;
      const res = mine === null || theirs === null ? "" : mine > theirs ? "W" : mine < theirs ? "L" : "T";
      return h("tr", {class: g.parse_status !== "ok" ? "bad" : null}, td(dateLink(g.date)),
        td([homeGame ? "vs " : "at ", teamLink(homeGame ? g.away_team : g.home_team, s)]),
        td(gameLink(g.game_key, `${mine ?? "?"}–${theirs ?? "?"}`)), td(res), td(g.game_type || h("span", {class: "muted"}, "not stated")));
    })),
    !isMissing(logs) && logs.missing_games.length ? [h("h2", {}, "Listed in the game logs, not in the corpus"),
      table(["Date", "Game", "Matchup", "Score (log)"], logs.missing_games.map((r) => h("tr", {},
        td(r.date), td(r.game_number, "num"), td(`${r.away_team} at ${r.home_team}`), td(`${r.away_score}–${r.home_score}`))))] : null,
    !isMissing(logs) && logs.other_series.length ? [h("h2", {}, "Postseason and all-star games"),
      h("p", {class: "small muted"}, "The corpus holds no event files for these by design, so they are listed but not counted as missing."),
      table(["Date", "Series", "Matchup", "Score (log)"], logs.other_series.map((r) => h("tr", {},
        td(r.date), td(r.series), td(`${r.away_team} at ${r.home_team}`), td(`${r.away_score}–${r.home_score}`))))] : null,
    !isMissing(logs) && logs.duplicate_rows ? h("p", {class: "small muted"},
      `${logs.duplicate_rows} game-log rows repeat a game already listed (the same game loaded from more than one log file) and are shown once.`) : null,
    h("h2", {}, "Roster"),
    isMissing(d.roster) ? missingNote(d.roster) : table(["Player", "Position", "Bats", "Throws"], d.roster.map((r) => h("tr", {},
      td(playerLink(r.person_id, [r.first, r.last].filter(Boolean).join(" "))), td(r.position || h("span", {class: "muted"}, "none")), td(r.bats), td(r.throws)))));
}

// -- parks, dates, seasons, tags --------------------------------------------------

async function pagePark(id) {
  const d = await get(`/api/park/${encodeURIComponent(id)}`);
  const p = d.park && !isMissing(d.park) ? d.park : null;
  const detail = h("div", {});
  show(
    h("h1", {}, p ? p.name || id : id),
    p ? kv([["Id", id], ["Also known as", p.aka], ["City", [p.city, p.state].filter(Boolean).join(", ")],
            ["Opened", p.start_date], ["Closed", p.end_date], ["League", p.league], ["Notes", p.notes]])
      : isMissing(d.park) ? missingNote(d.park) : null,
    h("div", {class: "actions"}, link(queryHref({park: id}), "Search plays at this park")),
    h("h2", {}, "Games in the corpus"),
    table(["Season", {label: "Games", cls: "num"}, "First", "Last"], d.seasons.map((s) => h("tr", {},
      td(h("a", {href: "#", onclick: async (e) => {
        e.preventDefault();
        const g = await get(`/api/park/${encodeURIComponent(id)}/${s.season}`);
        detail.replaceChildren(h("h3", {}, `${s.season}`), table(["Date", "Game", "Score"], g.games.map((x) => h("tr", {},
          td(dateLink(x.date)), td(gameLink(x.game_key, `${x.away_team} at ${x.home_team}`)), td(`${x.final_away ?? "?"}–${x.final_home ?? "?"}`)))));
        detail.scrollIntoView({behavior: "smooth"});
      }}, s.season)), td(fmt(s.games), "num"), td(dateLink(s.first)), td(dateLink(s.last))))),
    detail);
}

function shiftDate(day, n) {
  const d = new Date(`${day}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

async function pageDate(day) {
  const d = await get(`/api/date/${encodeURIComponent(day)}`);
  show(
    h("p", {class: "muted"}, link(`#/date/${shiftDate(day, -1)}`, "← previous day"), " · ",
      link(`#/season/${day.slice(0, 4)}`, `${day.slice(0, 4)} season`), " · ", link(`#/date/${shiftDate(day, 1)}`, "next day →")),
    h("h1", {}, day),
    d.games.length ? table(["League", "Game", "Score", "Park", {label: "Plays", cls: "num"}], d.games.map((g) => h("tr", {},
      td(g.league || ""), td(gameLink(g.game_key, `${g.away_name || g.away_team} at ${g.home_name || g.home_team}${g.game_number ? ` (${g.game_number})` : ""}`)),
      td(`${g.final_away ?? "?"}–${g.final_home ?? "?"}`), td(g.site ? link(`#/park/${encodeURIComponent(g.site)}`, g.site) : ""), td(fmt(g.plays), "num"))))
      : h("p", {}, "No games in the corpus on this date."));
}

async function pageSeason(year) {
  const d = await get(`/api/season/${encodeURIComponent(year)}`);
  const y = Number(year);
  show(
    h("p", {class: "muted"}, link(`#/season/${y - 1}`, `← ${y - 1}`), " · ", link("#/seasons", "all seasons"), " · ", link(`#/season/${y + 1}`, `${y + 1} →`)),
    h("h1", {}, `${y}`),
    h("div", {class: "actions"}, link(queryHref({season: String(y)}), "Search this season")),
    h("h2", {}, "Coverage"),
    isMissing(d.coverage) ? missingNote(d.coverage) : table(["League", {label: "Games", cls: "num"}, {label: "Teams", cls: "num"}, "Dates",
      {label: "Plays", cls: "num"}, {label: "Unparsed", cls: "num"}, {label: "Games missing", cls: "num"}],
      d.coverage.map((c) => h("tr", {}, td(c.league), td(fmt(c.games), "num"), td(c.teams, "num"), td(`${c.first_date} – ${c.last_date}`),
        td(fmt(c.plays), "num"), td(fmt(c.plays_unparsed), "num"),
        td(c.games_missing === null ? h("span", {class: "muted"}, "unmeasured") : fmt(c.games_missing), "num")))),
    h("h2", {}, "Teams"),
    table(["Team", "League", {label: "Games in corpus", cls: "num"}], d.teams.map((t) => h("tr", {},
      td(teamLink(t.team_id, y, `${t.city || ""} ${t.nickname || ""}`.trim() || t.team_id)), td(t.league || ""), td(fmt(t.games_in_corpus), "num")))));
}

async function pageSeasons() {
  const d = await get("/api/seasons");
  show(h("h1", {}, "Seasons"),
    table(["Season", "Leagues", {label: "Games", cls: "num"}], d.seasons.map((s) => h("tr", {},
      td(link(`#/season/${s.season}`, s.season)), td(s.leagues || ""), td(fmt(s.games), "num")))));
}

async function pageTags() {
  const sch = await schema();
  const byCat = {};
  sch.tags.forEach((t) => (byCat[t.category] = byCat[t.category] || []).push(t));
  show(h("h1", {}, "Tags"),
    h("p", {class: "muted"}, `The ontology, version ${sch.ontology_version}. Every tag has a derivation rule, specified in spec/04-ONTOLOGY.md.`),
    Object.entries(byCat).map(([cat, tags]) => [h("h2", {}, cat),
      table(["Tag", "Spec", "Notes"], tags.map((t) => h("tr", {},
        td(t.in_database ? link(queryHref({tag: [t.name]}), t.name) : [t.name, h("span", {class: "muted small"}, " (not in this database)")]),
        td(t.spec, "mono small"),
        td([t.alias_of ? `alias of ${t.alias_of}. ` : "", t.curated_only ? "curated only. " : "", t.note || ""], "small"))))]));
}

// -- router -----------------------------------------------------------------------

const ROUTES = [
  [/^\/?$/, () => pageQuery("", true)],
  [/^\/query\/?$/, (m, qs) => pageQuery(qs, false)],
  [/^\/play\/(\d+)$/, (m) => pagePlay(m[1])],
  [/^\/game\/id\/([^/]+)$/, (m) => pageGameById(decodeURIComponent(m[1]))],
  [/^\/game\/(\d+)$/, (m) => pageGame(m[1])],
  [/^\/player\/([^/]+)$/, (m) => pagePlayer(decodeURIComponent(m[1]))],
  [/^\/team\/([^/]+)\/(\d{4})$/, (m) => pageTeamSeason(decodeURIComponent(m[1]), m[2])],
  [/^\/team\/([^/]+)$/, (m) => pageTeam(decodeURIComponent(m[1]))],
  [/^\/park\/([^/]+)$/, (m) => pagePark(decodeURIComponent(m[1]))],
  [/^\/date\/(\d{4}-\d{2}-\d{2})$/, (m) => pageDate(m[1])],
  [/^\/season\/(\d{4})$/, (m) => pageSeason(m[1])],
  [/^\/seasons$/, () => pageSeasons()],
  [/^\/tags$/, () => pageTags()],
];

async function route() {
  cancelCurrent();
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  for (const [re, fn] of ROUTES) {
    const m = path.match(re);
    if (!m) continue;
    try {
      await fn(m, qs || "");
    } catch (err) {
      show(h("h1", {}, err.status === 404 ? "Not found" : "Error"), h("p", {class: "banner bad"}, err.message));
    }
    document.title = `RSSE · ${(document.querySelector("main h1") || {}).textContent || ""}`;
    return;
  }
  show(h("h1", {}, "Not found"), h("p", {}, "No such page. ", link("#/", "Start over")));
}

async function boot() {
  setupNameBox();
  window.addEventListener("hashchange", route);
  try {
    const about = await get("/api/about");
    document.getElementById("attribution").textContent = about.attribution;
    document.getElementById("versions").textContent = `corpus ${about.corpus_version} · ontology ${about.ontology_version}`;
  } catch (e) { /* the page still works; the footer is blank */ }
  route();
}

boot();
