/* Falcon BMS kneeboard front end.
 *
 * Fetches the parsed briefing from /api/state and renders it as tabbed pages.
 * A light /api/token poll detects BMS rewriting briefing.txt and reloads.
 */

"use strict";

const TABS = [
  { id: "home", label: "Home" },
  { id: "brief", label: "Brief" },
  { id: "loadout", label: "Loadout" },
  { id: "steer", label: "Steer" },
  { id: "comms", label: "Comms" },
  { id: "threats", label: "Threats" },
  { id: "wx", label: "Weather" },
  { id: "charts", label: "Charts" },
  { id: "maps", label: "Maps" },
];

let DATA = null;
// The board opens on the sim chooser, so a load never lands you in whichever sim
// happened to be newest. Polling calls load() rather than reloading the page, so
// this cannot pull you back to Home mid-flight.
let activeTab = "home";
let SIMS = null;
let lastToken = null;
const viewerChoice = {};

/* ------------------------------------------------------------- theme */

const themeNow = () =>
  document.documentElement.dataset.theme === "day" ? "day" : "night";

function applyTheme(name) {
  const theme = name === "day" ? "day" : "night";
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("kb.theme", theme);
  } catch (e) {}
  const btn = document.getElementById("theme-btn");
  // The button offers the theme you would switch *to*.
  if (btn) {
    btn.textContent = theme === "day" ? "Night" : "Day";
    btn.classList.toggle("on", theme === "day");
  }
  // A redraw keeps the viewer overlay colours consistent when the theme
  // changes while a chart or map is open.
  if (DATA && (activeTab === "charts" || activeTab === "maps")) renderMain();
}

/* ------------------------------------------------------------- helpers */

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

/** Treat BMS's "--" placeholder as empty. */
const val = (text) => {
  const t = String(text ?? "").trim();
  return !t || t === "--" ? "" : t;
};

const dim = (text) => (val(text) ? esc(text) : '<span class="dim">&mdash;</span>');

const card = (title, body, cls = "") =>
  `<div class="card ${cls}">${title ? `<h3>${esc(title)}</h3>` : ""}${body}</div>`;

const stat = (title, value, sub = "", tone = "") =>
  card(
    title,
    `<div class="value ${tone}${String(value).length > 13 ? " small" : ""}">${
      val(value) ? esc(value) : "&mdash;"
    }</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}`,
    "stat"
  );

const kv = (pairs) =>
  `<dl class="kv">${pairs
    .filter(([, v]) => val(v))
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join("")}</dl>`;

function table(headers, rows, opts = {}) {
  if (!rows.length) return `<div class="empty">${esc(opts.empty || "No data.")}</div>`;
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows
    .map((cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`)
    .join("");
  return `<div class="scroll-x"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function prose(groups, empty = "Nothing recorded.") {
  if (!groups || !groups.length) return `<div class="empty">${esc(empty)}</div>`;
  return `<div class="prose">${groups
    .map((group) => {
      const heading = group.heading ? `<h4>${esc(group.heading)}</h4>` : "";
      // Render in source order, collapsing each run of bullets into one list,
      // so a list stays attached to the sentence that introduces it.
      let body = "";
      let inList = false;
      for (const item of group.items) {
        if (item.kind === "bullet") {
          if (!inList) {
            body += "<ul>";
            inList = true;
          }
          body += `<li>${esc(item.text)}</li>`;
        } else {
          if (inList) {
            body += "</ul>";
            inList = false;
          }
          body += `<p>${esc(item.text)}</p>`;
        }
      }
      if (inList) body += "</ul>";
      return heading + body;
    })
    .join("")}</div>`;
}

const banners = (list) =>
  (list || [])
    .map(
      (w) =>
        `<div class="banner ${w.level === "error" ? "error" : ""}">${esc(w.text)}</div>`
    )
    .join("");

const pageHead = (title, note = "") =>
  `<div class="page-head"><h2>${esc(title)}</h2>${
    note ? `<span class="note">${esc(note)}</span>` : ""
  }</div>`;

/* ---------------------------------------------------------------- home */

/** The landing page: pick which sim to read. */
function renderHome() {
  if (!SIMS) return pageHead("Choose a sim") + '<div class="empty">Checking your installs&hellip;</div>';

  const cards = SIMS.map((s) => {
    const state = !s.found
      ? '<span class="tag red">not found</span>'
      : !s.ready
      ? '<span class="tag amber">no mission</span>'
      : '<span class="tag green">ready</span>';
    const newest = s.ready && s.newest ? ' <span class="tag cyan">newest</span>' : "";

    const lines = [
      s.source ? ["Mission", s.source] : null,
      s.updated ? ["Written", s.updated] : null,
      s.detail ? ["", s.detail] : null,
    ].filter(Boolean);

    const body =
      `<div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span style="font-family:var(--mono);font-size:18px;color:var(--amber)">${esc(s.title)}</span>
        ${state}${newest}
      </div>` +
      (lines.length
        ? `<dl class="kv">${lines
            .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
            .join("")}</dl>`
        : "") +
      (s.hint ? `<div class="hint" style="margin-top:9px">${esc(s.hint)}</div>` : "") +
      `<div style="margin-top:12px">
        <button class="link-btn sim-enter" data-sim="${esc(s.key)}" ${s.found ? "" : "disabled"}
                style="min-height:38px;padding:8px 16px;${
                  s.ready ? "border-color:var(--amber);color:var(--amber)" : ""
                }">
          ${s.ready ? "Open" : "Open anyway"} ${esc(s.label)}
        </button>
      </div>`;

    return card("", body);
  }).join("");

  const anyReady = SIMS.some((s) => s.ready);
  return (
    pageHead("Choose a sim", anyReady ? "" : "no missions found in any sim yet") +
    `<div class="grid c3">${cards}</div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "",
      '<div class="hint">Pinning a sim here keeps the board on it. To follow whichever sim ' +
        "wrote a mission most recently instead, use the button below the nav until it reads " +
        "<b>(auto)</b>.</div>"
    )}</div>`
  );
}

/** Pin a sim and go straight to its brief. */
async function enterSim(key) {
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sim: key }),
    });
  } catch (e) {}
  await load(true);
  setTab("brief");
}

async function loadSims() {
  try {
    const res = await fetch("/api/sims");
    SIMS = (await res.json()).sims || [];
  } catch (e) {
    SIMS = [];
  }
}

/* --------------------------------------------------------------- brief */

const SIM_LABELS = { bms: "BMS", dcs: "DCS", il2: "IL-2" };

const simIs = (name, d) => (d || DATA || {}).sim === name;
const isDcs = (d) => simIs("dcs", d);
const isIl2 = (d) => simIs("il2", d);

function renderBrief(d) {
  const b = d.briefing || {};
  const o = b.overview || {};
  const alt = b.alternate_airfield || {};

  // DCS carries no package construct, but does carry the airframe, theatre and
  // mission date, which are worth the same real estate.
  const cards = (
    isDcs(d)
      ? [
          stat("Flight", o.flight, o.aircraft_type || "", "amber"),
          stat("Task", o.role, o.theatre || ""),
          stat("Time on Target", o.time_on_target, "", "green"),
          stat("Start", o.start_time, o.mission_date || ""),
        ]
      : [
          stat("Flight", o.flight, o.role || "", "amber"),
          stat("Package", o.package, o.package_type || ""),
          stat("Time on Target", o.time_on_target, "", "green"),
          stat("Target", o.target_icao || "—", o.target_area || ""),
        ]
  ).join("");

  if (isIl2(d)) {
    const c = b.consumables || {};
    const r = c.rounds || {};
    const roster = b.roster || { headers: [], rows: [] };
    return (
      pageHead("Mission Brief", o.mission || "") +
      banners(d.warnings) +
      `<div class="grid c4">
        ${stat("Flight", o.flight, o.pilot || "", "amber")}
        ${stat("Aircraft", o.aircraft_type, o.country || "")}
        ${stat("Takeoff", o.start_time, o.mission_date || "", "green")}
        ${stat("Theatre", o.theatre, (b.airbases || {}).departure || "")}
      </div>` +
      `<div class="grid c4" style="margin-top:12px">
        ${stat("Fuel", c.fuel_pct != null ? `${c.fuel_pct}%` : "—", "of internal capacity")}
        ${stat("MG rounds", r.bullets != null ? String(r.bullets) : "—", "at spawn")}
        ${stat("Cannon", r.shells != null ? String(r.shells) : "—", "at spawn")}
        ${stat("Bombs / rockets", `${r.bombs ?? "—"} / ${r.rockets ?? "—"}`, "at spawn")}
      </div>` +
      `<div class="grid" style="margin-top:12px">${card(
        "Mission Briefing",
        prose(b.situation, "This mission carries no briefing text.")
      )}</div>` +
      `<div class="grid" style="margin-top:12px">${card(
        "Your Flight",
        table(
          ["#", ...(roster.headers || [])],
          (roster.rows || []).map((row) => [
            esc(row.callsign),
            ...row.pilots.map((p) => esc(p)),
          ]),
          { empty: "No other aircraft in your flight." }
        )
      )}</div>`
    );
  }

  if (isDcs(d)) {
    const c = b.consumables || {};
    return (
      pageHead("Mission Brief", o.mission || "") +
      banners(d.warnings) +
      `<div class="grid c4">${cards}</div>` +
      `<div class="grid c4" style="margin-top:12px">
        ${stat("Fuel", c.fuel_lb ? `${c.fuel_lb.toLocaleString()} lb` : "—", "internal + external")}
        ${stat("Flares", c.flare != null ? String(c.flare) : "—")}
        ${stat("Chaff", c.chaff != null ? String(c.chaff) : "—")}
        ${stat("Gun", c.gun_percent != null ? `${c.gun_percent}%` : "—")}
      </div>` +
      `<div class="grid" style="margin-top:12px">${card(
        "Mission Briefing",
        prose(b.situation, "This mission carries no briefing text.")
      )}</div>`
    );
  }

  const detail = [
    card(
      "Mission",
      kv([
        ["Task", o.mission],
        ["Target area", o.target_area],
        ["Sunrise", o.sunrise],
        ["Sunset", o.sunset],
      ]) || '<div class="empty">No detail.</div>'
    ),
    card(
      "Recovery",
      kv([
        ["Departure", (b.airbases || {}).departure],
        ["Recovery", (b.airbases || {}).recovery],
        ["Alternate", alt.text || (b.airbases || {}).alternate],
      ])
    ),
  ].join("");

  const pkgRows = (b.package || []).map((e) => [
    `${esc(e.callsign)}${e.primary ? ' <span class="tag amber">PRI</span>' : ""}`,
    dim(e.flight_number),
    esc(e.role),
    esc(e.aircraft),
    e.timing.map((t) => `${esc(t.label)} ${esc(t.value)}`).join("<br>") || "&mdash;",
    `<span class="wrap">${esc(e.task)}</span>`,
  ]);

  const roster = b.roster || { headers: [], rows: [] };
  const rosterRows = (roster.rows || []).map((r) => [
    esc(r.callsign),
    ...Array.from({ length: Math.max(roster.headers.length, r.pilots.length) }, (_, i) =>
      r.pilots[i] && r.pilots[i] !== "Unassigned"
        ? `<span class="tag green">${esc(r.pilots[i])}</span>`
        : '<span class="dim">unassigned</span>'
    ),
  ]);

  return (
    pageHead("Mission Brief", b.generated ? `generated ${b.generated}` : "") +
    banners(d.warnings) +
    `<div class="grid c4">${cards}</div>` +
    `<div class="grid c2" style="margin-top:12px">${detail}</div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "Package Elements",
      table(
        ["Callsign", "Flt #", "Role", "Aircraft", "Timing", "Task"],
        pkgRows,
        { empty: "No package elements." }
      )
    )}</div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "Pilot Roster",
      table(["Callsign", ...(roster.headers || [])], rosterRows, {
        empty: "No roster.",
      })
    )}</div>` +
    `<div class="grid c2" style="margin-top:12px">${card(
      "Situation",
      prose(b.situation)
    )}${card("Rules of Engagement", prose(b.roe))}</div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "Emergency Procedures",
      prose(b.emergency)
    )}</div>`
  );
}

/* ------------------------------------------------------------- loadout */

/** Each sim reads its loadout from somewhere different, and the caveats differ. */
const PROVENANCE = {
  bms:
    "Stores are read from the Ordnance section of briefing.txt. Weights and missile " +
    "ranges come from Falcon4_WCD.xml in your BMS install. Range is only shown for " +
    "missiles &mdash; BMS stores a placeholder value for bombs that would be misleading " +
    "as a release range. Employment guidance, fuzing and laser applicability are curated " +
    "reference notes, not game data. External tank weights are dry weights, not fuel loads.",
  dcs:
    "Stores are read from the pylon table in the mission's .miz. Employment guidance, " +
    "fuzing and laser applicability are curated reference notes covering the F/A-18C, " +
    "A-10C and AV-8B; anything else shows its raw CLSID. DCS publishes no per-store " +
    "weight or range this board can trust, so those fields stay empty.",
  il2:
    "Store names come from IL-2's own tables inside Scripts.gtp and Swf.gtp, read " +
    "directly rather than curated &mdash; the payload id in the mission is an index into " +
    "the aircraft's own ammunition list. Quantities use the game's label, cross-checked " +
    "against the ordnance entries behind it. IL-2 publishes no per-store weight or range, " +
    "so those stay empty, and there is no employment guidance to draw on.",
};

function storeBlock(store, index, prefix) {
  const facts = [];
  if (store.range_nm) facts.push(`RNG <b>${store.range_nm} nm</b>`);
  if (store.weight_lb) facts.push(`WT <b>${store.weight_lb} lb</b>`);
  if (store.blast_radius) facts.push(`BLAST <b>${store.blast_radius} ft</b>`);

  const tags = [];
  if (store.category_label)
    tags.push(`<span class="tag cyan">${esc(store.category_label)}</span>`);
  if (store.laser) tags.push('<span class="tag amber">LASER CODE</span>');
  if (!store.known) tags.push('<span class="tag red">NO REF DATA</span>');

  const body = [];
  if (store.guidance)
    body.push(`<h5>Guidance</h5><div>${esc(store.guidance)}</div>`);
  if (store.role) body.push(`<h5>Role</h5><div>${esc(store.role)}</div>`);
  if (store.employment && store.employment.length)
    body.push(
      `<h5>Employment</h5><ul>${store.employment
        .map((e) => `<li>${esc(e)}</li>`)
        .join("")}</ul>`
    );
  if (store.fuzing) body.push(`<h5>Fuzing</h5><div>${esc(store.fuzing)}</div>`);
  if (store.laser_note)
    body.push(`<h5>Laser</h5><div>${esc(store.laser_note)}</div>`);
  if (store.requires && store.requires.length)
    body.push(
      `<h5>Requires</h5><ul>${store.requires
        .map((r) => `<li>${esc(r)}</li>`)
        .join("")}</ul>`
    );
  if (!body.length)
    body.push(
      '<h5>Reference</h5><div class="dim">No curated employment data for this store. ' +
        "Weight and range above come from the game files.</div>"
    );

  return `<div class="store" id="${prefix}-${index}">
    <div class="store-head" data-store="${prefix}-${index}">
      ${store.station ? `<span class="count dim" title="station">${esc(store.station)}</span>` : ""}
      <span class="count">${store.count}&times;</span>
      <span class="name">${esc(store.name)}</span>
      ${tags.join(" ")}
      <span class="store-facts">${facts.join("")}</span>
      <span class="chev">&#9662;</span>
    </div>
    <div class="store-body">${body.join("")}</div>
  </div>`;
}

function renderLoadout(d) {
  const loadout = d.loadout || {};
  const flights = loadout.flights || [];
  if (!flights.length)
    return pageHead("Loadout") + '<div class="empty">No ordnance in the briefing.</div>';

  const player = flights.find((f) => f.is_player) || flights[0];
  const others = flights.filter((f) => f !== player);
  const lead = player.aircraft[0] || { stores: [], total_weight_lb: null };

  // IL-2 has two loadout sources of differing authority, and no laser weapons.
  const src = loadout.source || null;
  const il2Head = isDcs(d)
    ? ""
    : src
    ? stat(
        src.kind === "as-flown" ? "As Flown" : "Planned",
        src.kind === "as-flown" ? "confirmed" : "default",
        src.kind === "as-flown" ? src.log : "mission default",
        src.kind === "as-flown" ? "green" : "amber"
      )
    : "";

  const head = [
    stat("Flight", player.callsign, `${player.aircraft.length} aircraft`, "amber"),
    // DCS publishes no per-store weight worth trusting, so the slot shows the
    // airframe instead of an empty figure with a misleading caption.
    isDcs(d) || isIl2(d)
      ? stat("Aircraft", lead.label || "—", "from the mission file")
      : stat(
          "Stores Weight",
          lead.total_weight_lb ? `${lead.total_weight_lb} lb` : "—",
          "per aircraft, game data"
        ),
    stat("Store Types", String(lead.stores.length), "distinct stores loaded"),
    // IL-2 predates laser designation, so that slot carries fuel instead.
    isIl2(d)
      ? stat(
          src && src.kind === "as-flown" ? "As Flown" : "Planned",
          (loadout.as_flown || loadout.planned || {}).fuel_pct != null
            ? `${(loadout.as_flown || loadout.planned).fuel_pct}% fuel`
            : "—",
          src ? src.confidence : "",
          src && src.kind === "as-flown" ? "green" : "amber"
        )
      : stat(
          "Laser Code",
          d.laser.needed ? d.laser.code || "not set" : "n/a",
          d.laser.needed ? "required by loadout" : "no laser weapons",
          d.laser.needed ? "amber" : ""
        ),
  ].join("");

  const stores = lead.stores
    .map((s, i) => storeBlock(s, i, "pl"))
    .join("");

  const notUniform = !player.uniform
    ? '<div class="banner">Wingmen are not carrying the same loadout as the lead. ' +
      "Per-aircraft detail is below.</div>"
    : "";

  // IL-2 gets a source panel where the other sims get the laser-code panel.
  const laserPanel = isIl2(d) ? renderIl2SourcePanel(d, loadout) : renderLaserPanel(d);

  const otherBlocks = others.length
    ? card(
        "Package Mates",
        others
          .map((f) => {
            const ac = f.aircraft[0] || { stores: [] };
            return `<div style="margin-bottom:10px"><div style="font-family:var(--mono);color:var(--cyan);margin-bottom:5px">${esc(
              f.callsign
            )} <span class="dim">&mdash; ${f.aircraft.length} aircraft</span></div>${table(
              ["Qty", "Store", "Weight", "Range"],
              ac.stores.map((s) => [
                `${s.count}&times;`,
                esc(s.name),
                s.weight_lb ? `${s.weight_lb} lb` : '<span class="dim">&mdash;</span>',
                s.range_nm ? `${s.range_nm} nm` : '<span class="dim">&mdash;</span>',
              ])
            )}</div>`;
          })
          .join("")
      )
    : "";

  const perAircraft = !player.uniform
    ? card(
        "Per-Aircraft Detail",
        player.aircraft
          .map(
            (ac) =>
              `<div style="margin-bottom:10px"><div style="font-family:var(--mono);color:var(--cyan);margin-bottom:5px">${esc(
                ac.label
              )}</div>${table(
                ["Qty", "Store"],
                ac.stores.map((s) => [`${s.count}&times;`, esc(s.name)])
              )}</div>`
          )
          .join("")
      )
    : "";

  return (
    pageHead("Loadout", `${player.callsign} — click a store for employment detail`) +
    notUniform +
    `<div class="grid c4">${head}</div>` +
    `<div style="margin-top:12px">${stores}</div>` +
    laserPanel +
    (perAircraft ? `<div class="grid" style="margin-top:12px">${perAircraft}</div>` : "") +
    (otherBlocks ? `<div class="grid" style="margin-top:12px">${otherBlocks}</div>` : "") +
    `<div class="grid" style="margin-top:12px">${card(
      "Where this comes from",
      `<div class="hint">${PROVENANCE[d.sim] || PROVENANCE.bms}</div>`
    )}</div>`
  );
}

/** Where IL-2's loadout came from, and how planned differs from as flown. */
function renderIl2SourcePanel(d, loadout) {
  const src = loadout.source || {};
  const planned = loadout.planned || {};
  const flown = loadout.as_flown;
  const differs = loadout.differs || [];

  const warning = differs.length
    ? `<div class="banner">The mission's default loadout differs from what you actually flew ` +
      `(${differs.map((x) => esc(x)).join(", ")}). Showing as flown.</div>`
    : "";

  const rows = [
    [
      "Payload",
      planned.payload_id != null ? `#${planned.payload_id}` : "&mdash;",
      flown && flown.payload_id != null ? `#${flown.payload_id}` : "&mdash;",
    ],
    [
      "Fuel",
      planned.fuel_pct != null ? `${planned.fuel_pct}%` : "&mdash;",
      flown && flown.fuel_pct != null ? `${flown.fuel_pct}%` : "&mdash;",
    ],
  ];

  return (
    warning +
    `<div class="grid c2" style="margin-top:12px">
      ${card(
        src.kind === "as-flown" ? "Source — as flown" : "Source — mission default",
        `<div class="hint">${esc(src.note || "")}</div>` +
          (src.raw
            ? `<h5 style="margin:11px 0 4px;font-size:10px;letter-spacing:.12em;` +
              `text-transform:uppercase;color:var(--text-faint)">Game's own label</h5>` +
              `<div class="mono-block">${esc(src.raw)}</div>`
            : "") +
          ((src.reasons || []).length
            ? `<div class="hint" style="margin-top:8px">${(src.reasons || [])
                .map((r) => esc(r))
                .join("<br>")}</div>`
            : "")
      )}
      ${card(
        "Planned vs as flown",
        table(["", "Planned", "As flown"], rows) +
          '<div class="hint" style="margin-top:8px">Weapon modifications and gun round ' +
          "counts are not shown: IL-2's modification bitmask digit order and whether a " +
          "round count is per-gun or a total are both unconfirmed, so displaying them " +
          "would mean guessing.</div>"
      )}
    </div>`
  );
}

function renderLaserPanel(d) {
  const laser = d.laser || {};
  const ref = laser.reference || {};
  const needed = laser.needed;

  const stores = laser.player_laser_stores || [];
  const applies = stores.length
    ? `<div class="hint" style="margin-top:8px">Applies to: ${stores
        .map((s) => `<span class="tag amber">${esc(s)}</span>`)
        .join(" ")}</div>`
    : "";

  const rules = (ref.rules || []).map((r) => `<li>${esc(r)}</li>`).join("");

  return `<div class="grid c2" style="margin-top:12px">
    ${card(
      needed ? "Laser Code — required by this loadout" : "Laser Code",
      `<div class="laser-row">
        <input class="laser-input" id="laser-code" value="${esc(laser.code || "")}"
               maxlength="4" inputmode="numeric" aria-label="Laser code">
        <div>
          <div class="hint">Valid ${esc(ref.valid_range || "1111 - 1788")}. Default ${esc(
        ref.default || "1688"
      )}.</div>
          <div class="save-state" id="laser-save"></div>
        </div>
      </div>
      <div class="laser-row" style="margin-top:10px">
        <label class="hint" for="wing-code">Buddy / wingman code</label>
        <input class="laser-input" id="wing-code" style="font-size:19px;width:92px"
               value="${esc(laser.wingman_code || "")}" maxlength="4" inputmode="numeric">
      </div>
      ${applies}
      <div class="hint" style="margin-top:10px">${esc(laser.source_note || "")}</div>`
    )}
    ${card("Laser Code Rules", `<ul class="prose" style="padding-left:17px">${rules}</ul>`)}
  </div>`;
}

/* --------------------------------------------------------------- steer */

function renderSteer(d) {
  const points = (d.briefing || {}).steerpoints || [];
  const rows = points.map((p) => [
    `<span style="color:var(--amber)">${esc(p.index)}</span>`,
    dim(p.description),
    dim(p.time),
    dim(p.distance),
    dim(p.heading),
    dim(p.cas),
    dim(p.altitude),
    dim(p.action),
    dim(p.formation),
    `<span class="wrap">${val(p.comments) ? esc(p.comments) : ""}</span>`,
  ]);
  return (
    pageHead("Steerpoints", `${points.length} points`) +
    card(
      "",
      table(
        ["#", "Desc", "Time", "Dist", "Hdg", "CAS", "Alt", "Action", "Form", "Comments"],
        rows,
        { empty: "No steerpoints in the briefing." }
      )
    )
  );
}

/* --------------------------------------------------------------- comms */

function renderComms(d) {
  const b = d.briefing || {};
  const comms = b.comms || { rows: [] };
  const dtc = d.dtc || { uhf: [], vhf: [] };

  // IL-2 models no tunable radio, so there are no frequencies to publish at all.
  // What it does have is callsigns, which are worth knowing.
  if (isIl2(d)) {
    const rows = comms.rows.map((r) => [
      esc(r.agency),
      `<span style="color:var(--cyan)">${esc(r.callsign)}</span>`,
      `<span class="wrap dim">${esc(r.notes)}</span>`,
    ]);
    return (
      pageHead("Communications", "callsigns only") +
      `<div class="grid">${card(
        "Callsigns",
        table(["Agency", "Callsign", "Notes"], rows, {
          empty: "No callsigns could be resolved for this mission.",
        })
      )}</div>` +
      `<div class="grid" style="margin-top:12px">${card(
        "Note",
        '<div class="hint">IL-2 aircraft of this era have no tunable radio and the mission ' +
          "files carry no frequencies, so there is nothing to list. Callsigns come from " +
          "the mission's callsign and number fields resolved against the game's own " +
          "callsign table. The preset, IFF and Link 16 panels do not apply.</div>"
      )}</div>`
    );
  }

  // DCS has no comm ladder or IFF rotation; what it does have is the aircraft's
  // programmed preset channels, one block per radio.
  if (isDcs(d)) {
    const rows = comms.rows.map((r) => [
      esc(r.agency),
      `<span style="color:var(--cyan)">${esc(r.callsign)}</span>`,
      dim(r.uhf),
      `<span class="wrap dim">${esc(r.notes)}</span>`,
    ]);
    const radios = (b.radios || [])
      .map((radio) =>
        card(
          radio.radio,
          table(
            ["#", "Freq"],
            radio.presets.map((p) => [
              `<span style="color:var(--amber)">${p.preset}</span>`,
              esc(p.frequency),
            ])
          )
        )
      )
      .join("");
    return (
      pageHead("Communications", "from the mission's radio presets") +
      `<div class="grid">${card(
        "Flight and Support",
        table(["Agency", "Callsign", "Frequency", "Notes"], rows, {
          empty: "No frequencies in the mission.",
        })
      )}</div>` +
      `<div class="grid c3" style="margin-top:12px">${
        radios || card("Presets", '<div class="empty">No presets programmed.</div>')
      }</div>` +
      `<div class="grid" style="margin-top:12px">${card(
        "Note",
        '<div class="hint">These are the preset channels stored in the mission for your ' +
          "aircraft. DCS has no comm-ladder or IFF-rotation equivalent, so those panels " +
          "are not shown. Only tanker and AWACS groups are listed above &mdash; DCS group " +
          "tasks do not reliably describe what a generated group actually does, so " +
          "labelling the rest would be guesswork.</div>"
      )}</div>`
    );
  }

  const ladderRows = comms.rows.map((r) => [
    esc(r.agency),
    val(r.callsign) && r.callsign !== "None"
      ? `<span style="color:var(--cyan)">${esc(r.callsign)}</span>`
      : '<span class="dim">&mdash;</span>',
    dim(r.uhf),
    dim(r.vhf),
    `<span class="wrap dim">${esc(r.notes)}</span>`,
  ]);

  const presetTable = (list, band) =>
    table(
      ["#", "Freq", "Assignment"],
      list.map((p) => [
        `<span style="color:var(--amber)">${p.preset}</span>`,
        esc(p.frequency),
        p.open ? '<span class="dim">open</span>' : esc(p.comment),
      ]),
      { empty: `No ${band} presets found.` }
    );

  const iffBlocks = ((b.iff || {}).blocks || [])
    .map((block) => {
      const rows = block.rows.map(
        (r) =>
          `<tr><th style="border:0;padding:3px 9px 3px 0">${esc(
            r.label
          )}</th>${r.values
            .map((v) => `<td style="border:0;padding:3px 9px 3px 0">${esc(v)}</td>`)
            .join("")}</tr>`
      );
      return `<h5 style="margin:10px 0 4px;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--amber)">${esc(
        block.heading || "General"
      )}</h5><div class="scroll-x"><table style="width:auto">${rows.join("")}</table></div>`;
    })
    .join("");

  const link16 = (b.link16 || [])
    .map((file) => {
      const meta = file.meta.length
        ? kv(file.meta.map((m) => [m.label, m.value]))
        : "";
      const stnRows = file.stns.map((s) => [
        `<span style="color:var(--cyan)">${esc(s.label)}</span>`,
        ...s.values.map((v) => (v === "----" ? '<span class="dim">&mdash;</span>' : esc(v))),
        s.channels.map((c) => `${esc(c.label)} ${esc(c.value)}`).join("<br>"),
      ]);
      return `<div style="margin-bottom:12px"><div style="font-family:var(--mono);color:var(--amber);margin-bottom:6px">${esc(
        file.name
      )}</div>${meta}${table(
        ["", ...(file.headers.length ? file.headers.slice(0, 8) : ["#1", "#2", "#3", "#4", "#5", "#6", "#7", "#8"]), "Channels"],
        stnRows
      )}</div>`;
    })
    .join("");

  const dtcNote = dtc.generated
    ? `dtc_comm.txt generated ${dtc.generated}`
    : "dtc_comm.txt not found";

  return (
    pageHead("Communications") +
    banners((d.warnings || []).filter((w) => /preset/i.test(w.text))) +
    `<div class="grid">${card("Comm Ladder", table(
      ["Agency", "Callsign", "UHF", "VHF", "Notes"],
      ladderRows,
      { empty: "No comm ladder in the briefing." }
    ))}</div>` +
    `<div class="grid c2" style="margin-top:12px">
      ${card(`UHF Presets`, presetTable(dtc.uhf, "UHF"))}
      ${card(`VHF Presets`, presetTable(dtc.vhf, "VHF"))}
    </div>
    <div class="hint" style="margin-top:6px">${esc(dtcNote)}</div>` +
    `<div class="grid c2" style="margin-top:12px">
      ${card("IFF", iffBlocks || '<div class="empty">No IFF data.</div>')}
      ${card("Link 16", link16 || '<div class="empty">No Link 16 data.</div>')}
    </div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "Note on IFF rotation",
      '<div class="hint">The rotation rows are shown exactly as BMS writes them. ' +
        "BMS does not always emit the same number of values in each row, so the rows " +
        "are deliberately not zipped into aligned columns &mdash; doing so risks " +
        "pairing a code with the wrong time block. Read across each row as printed.</div>"
    )}</div>`
  );
}

/* ------------------------------------------------------------- threats */

function renderThreats(d) {
  const b = d.briefing || {};
  const support = b.support || [];
  const rows = support.map((s) => [
    `<span style="color:var(--cyan)">${esc(s.callsign)}</span>`,
    s.kind ? `<span class="tag violet">${esc(s.kind)}</span>` : "",
    esc(s.asset),
    s.tacan ? `<span class="tag green">TCN ${esc(s.tacan)}</span>` : '<span class="dim">&mdash;</span>',
    `<span class="wrap dim">${esc(s.detail)}</span>`,
  ]);

  if (isIl2(d)) {
    return (
      pageHead("Threats & Support") +
      '<div class="banner">IL-2 missions carry no threat brief. The mission file places every ' +
      "unit on the map, but nothing marks which threaten your route, so building a threat " +
      "picture from it would be invention rather than reading. The mission briefing on the " +
      "Brief page is what the game itself tells you.</div>"
    );
  }

  if (isDcs(d)) {
    return (
      pageHead("Threats & Support") +
      '<div class="banner">DCS missions carry no threat brief. The mission file lists every ' +
      "unit on the map, but nothing marks which are a threat to your route, so building " +
      "a threat picture from it would be invention rather than reading. Check the mission " +
      "briefing text and your own kneeboard pages instead.</div>" +
      `<div class="grid" style="margin-top:12px">${card(
        "Support Assets",
        table(["Callsign", "Type", "Asset", "TACAN", "Station"], rows, {
          empty: "No tanker or AWACS groups found in the mission.",
        })
      )}</div>`
    );
  }

  return (
    pageHead("Threats & Support") +
    `<div class="grid">${card("Threat Analysis", prose(b.threats))}</div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "Support Assets",
      table(["Callsign", "Type", "Asset", "TACAN", "Station"], rows, {
        empty: "No support assets.",
      })
    )}</div>`
  );
}

/* ------------------------------------------------------------- weather */

function renderWeather(d) {
  const wx = (d.briefing || {}).weather || { headers: [], rows: [] };
  const rows = (wx.rows || []).map((r) => [
    `<span class="dim">${esc(r.label)}</span>`,
    ...r.values.map((v) => esc(v)),
  ]);
  const pick = (label) => {
    const row = (wx.rows || []).find((r) => r.label.toLowerCase().startsWith(label));
    return row ? row.values[0] : "";
  };
  // BMS forecasts per phase of flight; DCS and IL-2 weather apply mission-wide.
  const when = isDcs(d) || isIl2(d) ? "mission-wide" : "at takeoff";
  // IL-2 records no visibility distance -- it models haze instead -- so that slot
  // would always be empty. Show temperature there rather than a dash.
  const fourth = isIl2(d)
    ? stat("Temperature", pick("temperature"), when, "green")
    : stat("Visibility", pick("visibility"), when, "green");
  return (
    pageHead("Weather") +
    `<div class="grid c4">
      ${stat("Conditions", pick("situation"), when)}
      ${stat("Wind", pick("wind"), when)}
      ${fourth}
      ${stat("Cloud Base", pick("cloud"), when)}
    </div>` +
    `<div class="grid" style="margin-top:12px">${card(
      "Full Forecast",
      table(["", ...(wx.headers || [])], rows, { empty: "No weather in the briefing." })
    )}</div>`
  );
}

/* -------------------------------------------------------------- charts */

function renderCharts(d) {
  const resolved = (d.charts || {}).resolved || [];
  const airfields = (d.charts || {}).airfields || [];
  const summary = (d.charts || {}).summary || {};

  // IL-2 ships no approach plates either, but its mission files do carry taxi
  // routes for each airfield, which are drawn here from their coordinates.
  if (isIl2(d)) {
    const taxi = (d.charts || {}).taxi || [];
    const pages = (d.charts || {}).pages || [];

    // A scripted campaign carries its own briefing map instead of taxi routes.
    if (pages.length) {
      let chosen = viewerChoice.page;
      if (!chosen || !pages.some((p) => p.entry === chosen)) chosen = pages[0].entry;
      const buttons = pages
        .map(
          (p) =>
            `<button data-page="${esc(p.entry)}" class="${p.entry === chosen ? "active" : ""}">${esc(
              p.name
            )}</button>`
        )
        .join("");
      return (
        pageHead("Campaign Map", "shipped with this campaign mission") +
        `<div class="chart-list">${buttons}</div>` +
        viewerFor(`/il2page/${encodeURI(chosen)}`, chosen)
      );
    }

    if (!taxi.length) {
      return (
        pageHead("Airfield Diagrams") +
        '<div class="banner">No taxi route is recorded for your departure field, and IL-2 ' +
        "ships no approach plates, so there is nothing to draw here.</div>"
      );
    }
    return (
      pageHead("Airfield Diagrams", "taxi routes from the mission file") +
      `<div class="grid c2">${taxi.map(taxiCard).join("")}</div>` +
      `<div class="grid" style="margin-top:12px">${card(
        "Note",
        '<div class="hint">These are the taxi routes the mission file records for each ' +
          "field, in metres relative to the field origin. IL-2 publishes no approach " +
          "plates, so this is the only airfield diagram available.</div>"
      )}</div>`
    );
  }

  // DCS ships no approach plates. What it can carry is kneeboard pages the
  // mission generator embedded in the .miz, which is what goes here instead.
  if (isDcs(d)) {
    const pages = (d.charts || {}).pages || [];
    if (!pages.length) {
      return (
        pageHead("Kneeboard Pages") +
        '<div class="banner">This mission embeds no kneeboard pages, and DCS ships no ' +
        "approach plates, so there is nothing to show here. Mission generators such as " +
        "Retribution add pages to the .miz; stock missions usually do not.</div>"
      );
    }
    let chosen = viewerChoice.page;
    if (!chosen || !pages.some((p) => p.entry === chosen)) chosen = pages[0].entry;
    const buttons = pages
      .map(
        (p, i) =>
          `<button data-page="${esc(p.entry)}" class="${p.entry === chosen ? "active" : ""}">${
            esc(p.aircraft ? `${p.aircraft} ` : "")
          }${esc(p.name)}</button>`
      )
      .join("");
    return (
      pageHead("Kneeboard Pages", `${pages.length} embedded in the mission`) +
      `<div class="chart-list">${buttons}</div>` +
      viewerFor(`/page/${encodeURI(chosen)}`, chosen)
    );
  }

  const found = resolved.filter((r) => r.found);
  const options = airfields
    .map(
      (a) =>
        `<option value="${esc(a.folder)}">${esc(a.name)}${
          a.icao ? ` (${esc(a.icao)})` : ""
        } — ${esc(a.country)}</option>`
    )
    .join("");

  const roleButtons = found
    .map(
      (r) =>
        `<button class="link-btn" data-airfield="${esc(
          r.airfield.folder
        )}">${esc(r.label)}: ${esc(r.airfield.name)}${
          r.airfield.icao ? ` (${esc(r.airfield.icao)})` : ""
        }</button>`
    )
    .join("");

  const missing = resolved
    .filter((r) => !r.found)
    .map(
      (r) =>
        `<div class="banner">No charts found for ${esc(
          r.label.toLowerCase()
        )} field &ldquo;${esc(r.requested)}&rdquo;.</div>`
    )
    .join("");

  const current =
    viewerChoice.airfield ||
    (found.length ? found[0].airfield.folder : airfields.length ? airfields[0].folder : "");

  return (
    pageHead(
      "Charts",
      `${summary.chart_count || 0} charts, ${summary.airfield_count || 0} airfields`
    ) +
    missing +
    `<div class="row-flow">
      ${roleButtons}
      <select class="picker" id="airfield-picker">${options}</select>
    </div>
    <div id="chart-area" data-airfield="${esc(current)}"></div>`
  );
}

function renderChartArea() {
  const host = document.getElementById("chart-area");
  if (!host) return;
  const folder = host.dataset.airfield;
  const airfield = ((DATA.charts || {}).airfields || []).find((a) => a.folder === folder);
  if (!airfield) {
    host.innerHTML = '<div class="empty">Select an airfield.</div>';
    return;
  }

  const key = `chart:${folder}`;
  let chosen = viewerChoice[key];
  if (!chosen || !airfield.charts.some((c) => c.rel === chosen))
    chosen = airfield.charts.length ? airfield.charts[0].rel : "";

  const buttons = airfield.charts
    .map(
      (c) =>
        `<button data-chart="${esc(c.rel)}" class="${c.rel === chosen ? "active" : ""}">${esc(
          c.kind
        )}${c.title ? ` · ${esc(c.title)}` : ""}</button>`
    )
    .join("");

  host.innerHTML =
    `<div class="chart-list">${buttons}</div>` +
    viewerFor(`/chart/${encodeURI(chosen)}`, chosen);

  host.querySelectorAll("[data-chart]").forEach((btn) =>
    btn.addEventListener("click", () => {
      viewerChoice[key] = btn.dataset.chart;
      renderChartArea();
    })
  );
  wirePanZoom(host);
}

/** Draw one airfield's taxi route as inline SVG.
 *
 * Point types are parking (0), taxiway (1) and runway (2). Drawn rather than
 * served as an image, so no file-serving route or whitelist is involved.
 */
function taxiCard(entry) {
  const points = entry.points || [];
  if (!points.length) return "";

  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 20;
  const width = Math.max(maxX - minX, 1);
  const height = Math.max(maxY - minY, 1);

  // Fit into a fixed viewBox, preserving aspect ratio.
  const box = 300;
  const scale = Math.min((box - pad * 2) / width, (box - pad * 2) / height);
  const px = (p) => pad + (p.x - minX) * scale;
  const py = (p) => box - (pad + (p.y - minY) * scale);

  const path = points.map((p, i) => `${i ? "L" : "M"}${px(p).toFixed(1)},${py(p).toFixed(1)}`).join(" ");
  const dots = points
    .map((p) => {
      const colour =
        p.type === 2 ? "var(--green)" : p.type === 0 ? "var(--amber)" : "var(--cyan)";
      return `<circle cx="${px(p).toFixed(1)}" cy="${py(p).toFixed(1)}" r="3" fill="${colour}"/>`;
    })
    .join("");

  const extent = `${Math.round(width)} x ${Math.round(height)} m`;
  return card(
    `${entry.label} — ${entry.airfield}`,
    `<svg viewBox="0 0 ${box} ${box}" style="width:100%;height:auto;background:var(--viewer-bg);border-radius:4px">
      <path d="${path}" fill="none" stroke="var(--border-bright)" stroke-width="2"/>
      ${dots}
    </svg>
    <div class="hint" style="margin-top:8px">${esc(extent)}${
      entry.callsign ? ` &middot; callsign ${esc(entry.callsign)}` : ""
    } &middot; <span style="color:var(--amber)">parking</span>,
    <span style="color:var(--cyan)">taxiway</span>,
    <span style="color:var(--green)">runway</span></div>`
  );
}

/* ---------------------------------------------------------------- maps */

function renderMaps(d) {
  const maps = (d.charts || {}).maps || [];
  if (isIl2(d) && !maps.length)
    return (
      pageHead("Maps") +
      '<div class="banner">IL-2 keeps its theatre maps inside packed archives as terrain ' +
      "data rather than as images, so there is nothing to show here. The Charts tab " +
      "carries the taxi diagrams the mission file does record.</div>"
    );
  if (isDcs(d) && !maps.length)
    return (
      pageHead("Maps") +
      '<div class="banner">DCS ships no theatre maps as image files, so there is nothing ' +
      "to show here. The Kneeboard Pages tab carries whatever the mission embedded.</div>"
    );
  if (!maps.length)
    return pageHead("Maps") + '<div class="empty">No maps found in the BMS Docs folder.</div>';

  let chosen = viewerChoice.map;
  if (!chosen || !maps.some((m) => m.rel === chosen)) chosen = maps[0].rel;

  const buttons = maps
    .map(
      (m) =>
        `<button data-map="${esc(m.rel)}" class="${m.rel === chosen ? "active" : ""}">${esc(
          m.title
        )} <span class="dim">${m.size_mb} MB</span></button>`
    )
    .join("");

  return (
    pageHead("Theater Maps", "drag to pan, scroll to zoom") +
    `<div class="chart-list">${buttons}</div>` +
    viewerFor(`/map/${encodeURI(chosen)}`, chosen)
  );
}

/* -------------------------------------------------------------- viewer */

function viewerFor(url, rel) {
  if (!rel) return '<div class="empty">Nothing to display.</div>';
  if (rel.toLowerCase().endsWith(".pdf")) {
    return `<div class="viewer"><iframe src="${esc(url)}#view=FitH" title="chart"></iframe></div>`;
  }
  return `<div class="viewer">
    <div class="pan" data-pan>
      <img src="${esc(url)}" alt="chart" draggable="false">
    </div>
    <div class="viewer-bar">
      <button data-zoom="in">+</button>
      <button data-zoom="out">&minus;</button>
      <button data-zoom="fit">FIT</button>
      <button data-zoom="1">1:1</button>
    </div>
  </div>`;
}

/** Drag-to-pan and scroll-to-zoom for the chart and map images. */
function wirePanZoom(root = document) {
  root.querySelectorAll("[data-pan]").forEach((pan) => {
    if (pan.dataset.wired) return;
    pan.dataset.wired = "1";

    const img = pan.querySelector("img");
    const view = { scale: 1, x: 0, y: 0 };

    const apply = () => {
      img.style.transform = `translate(${view.x}px, ${view.y}px) scale(${view.scale})`;
    };

    const fit = () => {
      if (!img.naturalWidth) return;
      const sx = pan.clientWidth / img.naturalWidth;
      const sy = pan.clientHeight / img.naturalHeight;
      view.scale = Math.min(sx, sy);
      view.x = (pan.clientWidth - img.naturalWidth * view.scale) / 2;
      view.y = (pan.clientHeight - img.naturalHeight * view.scale) / 2;
      apply();
    };

    if (img.complete && img.naturalWidth) fit();
    else img.addEventListener("load", fit, { once: true });

    let dragging = false;
    let startX = 0;
    let startY = 0;

    pan.addEventListener("mousedown", (e) => {
      dragging = true;
      startX = e.clientX - view.x;
      startY = e.clientY - view.y;
      pan.classList.add("dragging");
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      view.x = e.clientX - startX;
      view.y = e.clientY - startY;
      apply();
    });
    window.addEventListener("mouseup", () => {
      dragging = false;
      pan.classList.remove("dragging");
    });

    pan.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const rect = pan.getBoundingClientRect();
        const cx = e.clientX - rect.left;
        const cy = e.clientY - rect.top;
        const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        const next = Math.min(8, Math.max(0.05, view.scale * factor));
        // Keep the point under the cursor fixed while zooming.
        view.x = cx - ((cx - view.x) * next) / view.scale;
        view.y = cy - ((cy - view.y) * next) / view.scale;
        view.scale = next;
        apply();
      },
      { passive: false }
    );

    const bar = pan.parentElement.querySelector(".viewer-bar");
    if (bar) {
      bar.addEventListener("click", (e) => {
        const mode = e.target.dataset.zoom;
        if (!mode) return;
        if (mode === "fit") return fit();
        if (mode === "1") {
          view.scale = 1;
          view.x = 0;
          view.y = 0;
          return apply();
        }
        const factor = mode === "in" ? 1.25 : 1 / 1.25;
        view.scale = Math.min(8, Math.max(0.05, view.scale * factor));
        apply();
      });
    }
  });
}

/* ---------------------------------------------------------------- shell */

const RENDERERS = {
  home: renderHome,
  brief: renderBrief,
  loadout: renderLoadout,
  steer: renderSteer,
  comms: renderComms,
  threats: renderThreats,
  wx: renderWeather,
  charts: renderCharts,
  maps: renderMaps,
};

// The board pages keep their 1-8 shortcuts; Home sits outside that sequence on H
// so adding it did not renumber everything.
const PAGE_TABS = TABS.filter((t) => t.id !== "home");

function renderNav() {
  document.getElementById("nav").innerHTML = TABS.map((t) => {
    const index = PAGE_TABS.indexOf(t);
    const key = t.id === "home" ? "H" : String(index + 1);
    return `<button data-tab="${t.id}" class="${t.id === activeTab ? "active" : ""}">${esc(
      t.label
    )}<span class="key">${key}</span></button>`;
  }).join("");
  document.querySelectorAll("#nav button").forEach((btn) =>
    btn.addEventListener("click", () => setTab(btn.dataset.tab))
  );
}

function setTab(id) {
  if (!RENDERERS[id]) return;
  activeTab = id;
  localStorage.setItem("kb.tab", id);
  renderNav();
  renderMain();
  // Refresh the chooser's status each time it is opened, so it never shows a
  // stale "no mission" after you have flown one.
  if (id === "home") loadSims().then(renderMain);
}

function renderMain() {
  const main = document.getElementById("main");

  // The chooser must work even when the selected sim has nothing to show --
  // that is exactly when you need to switch to another one.
  if (activeTab === "home") {
    main.innerHTML = renderHome();
    main.scrollTop = 0;
    main.querySelectorAll(".sim-enter").forEach((btn) =>
      btn.addEventListener("click", () => enterSim(btn.dataset.sim))
    );
    return;
  }

  if (!DATA) {
    main.innerHTML = '<div class="empty">Loading&hellip;</div>';
    return;
  }
  if (!DATA.ok) {
    main.innerHTML =
      pageHead("Setup Required") +
      banners(DATA.warnings) +
      '<div class="row-flow" style="margin-top:10px"><button class="link-btn" ' +
      'onclick="setTab(\'home\')">Back to sim chooser</button></div>';
    return;
  }

  main.innerHTML = RENDERERS[activeTab](DATA);
  main.scrollTop = 0;

  // Expandable store rows.
  main.querySelectorAll("[data-store]").forEach((head) =>
    head.addEventListener("click", () =>
      document.getElementById(head.dataset.store).classList.toggle("open")
    )
  );

  // Laser code fields.
  const wire = (id, field) => {
    const input = document.getElementById(id);
    if (input) input.addEventListener("change", () => saveLaser(field, input));
  };
  wire("laser-code", "laser_code");
  wire("wing-code", "wingman_laser_code");

  if (activeTab === "charts") {
    const picker = document.getElementById("airfield-picker");
    const area = document.getElementById("chart-area");
    if (picker && area) {
      picker.value = area.dataset.airfield;
      picker.addEventListener("change", () => {
        viewerChoice.airfield = picker.value;
        area.dataset.airfield = picker.value;
        renderChartArea();
      });
    }
    main.querySelectorAll("[data-airfield]").forEach((btn) => {
      if (btn.tagName !== "BUTTON") return;
      btn.addEventListener("click", () => {
        viewerChoice.airfield = btn.dataset.airfield;
        area.dataset.airfield = btn.dataset.airfield;
        if (picker) picker.value = btn.dataset.airfield;
        renderChartArea();
      });
    });
    renderChartArea();
  }

  if (activeTab === "maps") {
    main.querySelectorAll("[data-map]").forEach((btn) =>
      btn.addEventListener("click", () => {
        viewerChoice.map = btn.dataset.map;
        renderMain();
      })
    );
    wirePanZoom(main);
  }

  // DCS kneeboard pages live on the charts tab.
  main.querySelectorAll("[data-page]").forEach((btn) =>
    btn.addEventListener("click", () => {
      viewerChoice.page = btn.dataset.page;
      renderMain();
    })
  );
  if (isDcs()) wirePanZoom(main);
}

/** Cycle the sim preference: auto -> bms -> dcs -> auto. */
async function cycleSim() {
  const sims = (DATA && DATA.sims) || { available: [], preference: "auto" };
  const order = ["auto", ...sims.available];
  const next = order[(order.indexOf(sims.preference) + 1) % order.length];
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sim: next }),
    });
    await load(true);
  } catch (e) {}
}

async function saveLaser(field, input) {
  const note = document.getElementById("laser-save");
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: input.value.trim() }),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) {
      input.classList.add("bad");
      if (note) {
        note.textContent = body.error || "Could not save.";
        note.className = "save-state bad";
      }
      return;
    }
    input.classList.remove("bad");
    if (note) {
      note.textContent = "Saved";
      note.className = "save-state ok";
      setTimeout(() => (note.textContent = ""), 2000);
    }
    if (DATA && DATA.laser) DATA.laser.code = body.settings.laser_code;
  } catch (err) {
    if (note) {
      note.textContent = "Could not reach the kneeboard server.";
      note.className = "save-state bad";
    }
  }
}

function renderStatus() {
  const sub = document.getElementById("brand-sub");
  const fresh = document.getElementById("freshness");
  const gen = document.getElementById("generated");

  if (!DATA || !DATA.ok) {
    sub.textContent = "not connected";
    fresh.innerHTML = '<span class="stale">setup required</span>';
    return;
  }
  const install = DATA.install || {};
  const sims = DATA.sims || { available: [], preference: "auto", active: DATA.sim };
  sub.textContent = `${SIM_LABELS[DATA.sim] || "BMS"} ${
    install.version || (DATA.sim === "bms" ? "?" : "")
  }`.trim();
  fresh.innerHTML = '<span class="live">&#9679; live</span>';

  // The sim button doubles as the indicator of which sim is showing.
  const simBtn = document.getElementById("sim-btn");
  if (simBtn) {
    const key = sims.active || DATA.sim || "";
    const active = SIM_LABELS[key] || key.toUpperCase();
    simBtn.textContent = sims.preference === "auto" ? `${active} (auto)` : active;
    simBtn.classList.toggle("on", sims.preference !== "auto");
    simBtn.title =
      sims.available.length > 1
        ? "Click to change which sim the board reads (auto follows the newest mission)"
        : "Only one sim was found";
  }

  const b = DATA.briefing || {};
  const app = DATA.app || {};
  const upd = app.update || {};

  // Version line, plus a warning when self-update is not actually working.
  const build = upd.local_version ? ` · ${upd.local_version}` : "";
  const stalled = ["skipped-dirty", "skipped-local-commits", "not-a-repo", "no-git"];
  const versionLine = stalled.includes(upd.status)
    ? `<span class="stale">v${esc(app.version || "?")}${esc(build)} · not auto-updating</span>`
    : `v${esc(app.version || "?")}${esc(build)}${
        upd.status === "updated" ? ' <span class="live">· updated</span>' : ""
      }`;

  const sourceLine =
    DATA.sim === "dcs" || DATA.sim === "il2"
      ? install.mission_name
        ? `${DATA.sim === "il2" ? "mission" : "miz"} ${esc(install.mission_name)}`
        : "no mission found"
      : b.generated
      ? `brief ${esc(b.generated)}`
      : "no briefing yet";

  gen.innerHTML = sourceLine + `<br>${versionLine}`;
}

async function load(force = false) {
  const res = await fetch(`/api/state${force ? "?force=1" : ""}`);
  DATA = await res.json();
  lastToken = DATA.token;
  renderStatus();
  renderMain();
}

/** Poll for BMS rewriting the briefing, and pull a fresh board when it does. */
async function pollFreshness() {
  try {
    const res = await fetch("/api/token");
    const body = await res.json();
    if (lastToken !== null && body.token !== lastToken) await load();
  } catch (err) {
    const fresh = document.getElementById("freshness");
    if (fresh) fresh.innerHTML = '<span class="stale">&#9679; offline</span>';
  }
}

document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  const index = parseInt(e.key, 10);
  if (index >= 1 && index <= PAGE_TABS.length) setTab(PAGE_TABS[index - 1].id);
  if (e.key === "h" || e.key === "H") setTab("home");
  if (e.key === "r" || e.key === "R") load(true);
  if (e.key === "t" || e.key === "T") applyTheme(themeNow() === "day" ? "night" : "day");
});

document
  .getElementById("theme-btn")
  .addEventListener("click", () => applyTheme(themeNow() === "day" ? "night" : "day"));
document.getElementById("sim-btn").addEventListener("click", cycleSim);

applyTheme(themeNow());
renderNav();
loadSims().then(renderMain);
load().catch(() => {
  document.getElementById("main").innerHTML =
    '<div class="empty">Could not reach the kneeboard server.</div>';
});
setInterval(pollFreshness, 2000);
