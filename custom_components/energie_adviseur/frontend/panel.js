/*
 * Energie Adviseur - zijbalkpagina.
 * Cycli bekijken, een naam geven, splitsen door in de grafiek te klikken en historie inlezen.
 * Geen bouwstap en geen afhankelijkheden: een gewoon custom element.
 */

const DOMAIN = "energie_adviseur";

const TEXT = {
  nl: {
    title: "Energie Adviseur",
    loading: "Cycli laden…",
    noAppliances: "Er zijn nog geen apparaten toegevoegd. Voeg de integratie opnieuw toe voor je wasmachine, droger of vaatwasser.",
    importDays: "Dagen terug",
    importButton: "Historie inlezen",
    importing: "Bezig met inlezen…",
    imported: (n) => (n === 0 ? "Geen nieuwe cycli gevonden." : n === 1 ? "1 nieuwe cyclus gevonden." : `${n} nieuwe cycli gevonden.`),
    refresh: "Vernieuwen",
    programs: "Geleerde programma's",
    noPrograms: "Nog geen programma's geleerd. Geef hieronder een cyclus een naam.",
    times: (n) => `${n}×`,
    filterUnknown: (n) => `Zonder naam (${n})`,
    filterAll: (n) => `Alle cycli (${n})`,
    noCycles: "Geen cycli. Lees de historie in of wacht tot het apparaat heeft gedraaid.",
    noUnknown: "Alle cycli hebben een naam.",
    unknown: "Zonder naam",
    name: "Programma",
    namePlaceholder: "Naam, bijv. Katoen 40",
    save: "Opslaan",
    saved: (name) => `Opgeslagen als ${name}.`,
    chartHint: "Klik in de grafiek op het moment waar je wilt splitsen.",
    chartLabel: "Vermogen tijdens de cyclus",
    splitAt: "Splitsen om",
    afterStart: "na de start",
    splitMinutes: "Minuten na de start",
    part1: "Deel 1",
    part2: "Deel 2",
    dropped: "vervalt",
    keep: "Bewaren",
    keepBoth: "Beide delen",
    keepFirst: "Alleen deel 1",
    keepSecond: "Alleen deel 2",
    split: "Splitsen",
    cancel: "Annuleren",
    useQuiet: "Rustigste periode gebruiken",
    quietNote: "De rustigste periode zelf vervalt.",
    stepFine: "Splitsen gaat per 30 seconden. Fijn afstellen kan met het minutenveld of met de pijltjestoetsen in de grafiek (Shift = 5 minuten).",
    stepCoarse: "Van deze cyclus is het verloop alleen per 5 minuten bewaard. Met ‘Opnieuw ophalen’ lees je het per 30 seconden in.",
    undoNote: "Een splitsing maak je later ongedaan met ‘Splitsing ongedaan maken’.",
    undo: "Splitsing ongedaan maken",
    refetch: "Opnieuw ophalen",
    refetchTitle: "Leest deze periode opnieuw uit de historie van Home Assistant, per 30 seconden. Delen van een eerdere splitsing worden weer één cyclus.",
    restored: (n) => (n === 1 ? "Oorspronkelijke cyclus teruggehaald." : `${n} cycli teruggehaald.`),
    splitDone: "Cyclus gesplitst.",
    remove: "Verwijderen",
    removeAsk: "Deze cyclus verwijderen? Historie inlezen haalt hem niet terug.",
    removeYes: "Ja, verwijderen",
    removed: "Cyclus verwijderd.",
    running: "draait nu",
    paused: (duration) => `${duration} pauze niet meegeteld`,
    peak: "piek",
  },
  en: {
    title: "Energie Adviseur",
    loading: "Loading cycles…",
    noAppliances: "No appliances yet. Add the integration again for your washer, dryer or dishwasher.",
    importDays: "Days back",
    importButton: "Import history",
    importing: "Importing…",
    imported: (n) => (n === 0 ? "No new cycles found." : n === 1 ? "1 new cycle found." : `${n} new cycles found.`),
    refresh: "Refresh",
    programs: "Learned programmes",
    noPrograms: "No programmes learned yet. Name a cycle below.",
    times: (n) => `${n}×`,
    filterUnknown: (n) => `Unnamed (${n})`,
    filterAll: (n) => `All cycles (${n})`,
    noCycles: "No cycles. Import the history or wait until the appliance has run.",
    noUnknown: "Every cycle has a name.",
    unknown: "Unnamed",
    name: "Programme",
    namePlaceholder: "Name, e.g. Cotton 40",
    save: "Save",
    saved: (name) => `Saved as ${name}.`,
    chartHint: "Click the chart at the moment where you want to split.",
    chartLabel: "Power during the cycle",
    splitAt: "Split at",
    afterStart: "after the start",
    splitMinutes: "Minutes after the start",
    part1: "Part 1",
    part2: "Part 2",
    dropped: "dropped",
    keep: "Keep",
    keepBoth: "Both parts",
    keepFirst: "Part 1 only",
    keepSecond: "Part 2 only",
    split: "Split",
    cancel: "Cancel",
    useQuiet: "Use quietest period",
    quietNote: "The quietest period itself is dropped.",
    stepFine: "Splitting works in 30-second steps. Fine-tune with the minutes field or the arrow keys in the chart (Shift = 5 minutes).",
    stepCoarse: "This cycle was only stored in 5-minute steps. Use ‘Fetch again’ to read it in 30-second detail.",
    undoNote: "You can undo a split later with ‘Undo split’.",
    undo: "Undo split",
    refetch: "Fetch again",
    refetchTitle: "Reads this period again from the Home Assistant history, in 30-second detail. Parts of an earlier split become one cycle again.",
    restored: (n) => (n === 1 ? "Original cycle restored." : `${n} cycles restored.`),
    splitDone: "Cycle split.",
    remove: "Delete",
    removeAsk: "Delete this cycle? Importing history will not bring it back.",
    removeYes: "Yes, delete",
    removed: "Cycle deleted.",
    running: "running now",
    paused: (duration) => `${duration} paused, not counted`,
    peak: "peak",
  },
};

// Grafiekmaten in viewBox-eenheden. Op een smal scherm een kleiner tekenvlak,
// zodat de tekst leesbaar blijft en de grafiek hoog genoeg is om in te tikken.
const PLOT = { left: 46, right: 10, top: 10, bottom: 24 };
const WIDE = { w: 800, h: 190, ticks: 6 };
const NARROW = { w: 400, h: 200, ticks: 3 };
const NARROW_BELOW = 600;

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const STYLE = `
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color, #fafafa);
    color: var(--primary-text-color, #212121);
    font-family: var(--ha-font-family-body, var(--paper-font-body1_-_font-family, Roboto, "Segoe UI", sans-serif));
    font-size: 14px;
    line-height: 1.45;
    --ea-accent: var(--primary-color, #03a9f4);
    --ea-muted: var(--secondary-text-color, #727272);
    --ea-line: var(--divider-color, rgba(0, 0, 0, 0.12));
    --ea-card: var(--card-background-color, #fff);
    --ea-radius: var(--ha-card-border-radius, 12px);
    --ea-error: var(--error-color, #db4437);
    --ea-quiet: var(--warning-color, #ffa600);
  }
  * { box-sizing: border-box; }
  header {
    position: sticky; top: 0; z-index: 2;
    display: flex; align-items: center; gap: 4px;
    height: var(--header-height, 56px); padding: 0 12px;
    background: var(--app-header-background-color, var(--ea-accent));
    color: var(--app-header-text-color, #fff);
    border-bottom: 1px solid var(--app-header-border-bottom, transparent);
  }
  header h1 { margin: 0 0 0 8px; font-size: 20px; font-weight: 400; flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  main { max-width: 980px; margin: 0 auto; padding: 16px; }
  .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  button, input, select { font: inherit; color: inherit; }
  button {
    min-height: 40px; padding: 0 16px; border-radius: 20px; cursor: pointer;
    border: 1px solid var(--ea-line); background: transparent;
  }
  button:hover:not(:disabled) { background: color-mix(in srgb, var(--ea-accent) 10%, transparent); }
  button:focus-visible, input:focus-visible, svg:focus-visible { outline: 2px solid var(--ea-accent); outline-offset: 2px; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.primary { background: var(--ea-accent); border-color: var(--ea-accent); color: var(--text-primary-color, #fff); }
  button.primary:hover:not(:disabled) { background: var(--ea-accent); filter: brightness(1.08); }
  button.danger { border-color: var(--ea-error); color: var(--ea-error); }
  button.danger:hover:not(:disabled) { background: color-mix(in srgb, var(--ea-error) 10%, transparent); }
  button.danger.solid { background: var(--ea-error); color: #fff; }
  button.danger.solid:hover:not(:disabled) { background: var(--ea-error); filter: brightness(1.08); }
  .confirm { border-top: 1px solid var(--ea-line); margin-top: 12px; padding-top: 12px; }
  .confirm p { margin: 0 0 8px; }
  button.tab[aria-selected="true"], button.chip[aria-pressed="true"] {
    background: color-mix(in srgb, var(--ea-accent) 16%, transparent); border-color: var(--ea-accent); color: var(--ea-accent); font-weight: 500;
  }
  input[type="text"], input[type="number"] {
    min-height: 40px; padding: 0 12px; border-radius: 8px;
    border: 1px solid var(--ea-line); background: var(--ea-card); min-width: 0;
  }
  input[type="number"] { width: 84px; }
  label { color: var(--ea-muted); font-size: 12px; display: flex; flex-direction: column; gap: 4px; }
  .card { background: var(--ea-card); border: 1px solid var(--ea-line); border-radius: var(--ea-radius); padding: 16px; margin-bottom: 16px; }
  .row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 12px; }
  .grow { flex: 1 1 180px; }
  .muted { color: var(--ea-muted); }
  .status { margin: 8px 0 0; color: var(--ea-muted); }
  .status:empty { display: none; }
  input[type="radio"] { accent-color: var(--ea-accent); width: 18px; height: 18px; margin: 0; }
  .error { color: var(--ea-error); }
  .banner { border-color: var(--ea-error); color: var(--ea-error); }
  h2 { font-size: 16px; font-weight: 500; margin: 0 0 8px; }
  .programs { display: flex; flex-wrap: wrap; gap: 8px; margin: 0; padding: 0; list-style: none; }
  .programs li { border: 1px solid var(--ea-line); border-radius: 16px; padding: 4px 12px; }
  .programs b { font-weight: 500; }
  .cycle-head { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 16px; margin-bottom: 8px; }
  .cycle-head .when { font-size: 16px; font-weight: 500; }
  .cycle-head .num { font-variant-numeric: tabular-nums; }
  .badge { border-radius: 12px; padding: 2px 10px; font-size: 12px; border: 1px solid var(--ea-line); }
  .badge.named { border-color: var(--ea-accent); color: var(--ea-accent); }
  svg.chart { display: block; width: 100%; height: auto; cursor: crosshair; touch-action: pan-y; border-radius: 8px; }
  .chart .area { fill: color-mix(in srgb, var(--ea-accent) 28%, transparent); stroke: var(--ea-accent); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .chart .grid { stroke: var(--ea-line); stroke-width: 1; vector-effect: non-scaling-stroke; }
  .chart text { fill: var(--ea-muted); font-size: 11px; }
  .chart .quiet { fill: color-mix(in srgb, var(--ea-quiet) 22%, transparent); }
  .chart .cut { fill: color-mix(in srgb, var(--ea-error) 18%, transparent); }
  .chart .marker { stroke: var(--ea-error); stroke-width: 2; vector-effect: non-scaling-stroke; }
  .chart .hover { stroke: var(--primary-text-color, #212121); stroke-width: 1; stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
  .readout { min-height: 20px; font-variant-numeric: tabular-nums; margin: 4px 0 12px; }
  .split { border-top: 1px solid var(--ea-line); margin-top: 12px; padding-top: 12px; }
  .parts { display: flex; flex-wrap: wrap; gap: 8px 24px; margin: 8px 0; font-variant-numeric: tabular-nums; }
  .parts .off { text-decoration: line-through; color: var(--ea-muted); }
  fieldset { border: 0; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 16px; }
  legend { padding: 0; color: var(--ea-muted); font-size: 12px; margin-bottom: 4px; }
  .radio { flex-direction: row; align-items: center; gap: 6px; font-size: 14px; color: inherit; min-height: 32px; }
  @media (max-width: 600px) {
    main { padding: 12px 16px; }
    .card { padding: 12px; }
    .row > * { flex: 1 1 100%; }
    input[type="number"] { width: 100%; }
  }
  @media (prefers-reduced-motion: no-preference) { button { transition: background-color 120ms; } }
`;

class EnergieAdviseurPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._narrow = false;
    this._data = null; // antwoord van energie_adviseur/cycles
    this._selected = null; // entry_id van het gekozen apparaat
    this._filter = null; // "unknown" | "all"
    this._names = {}; // cycle_id -> getypte naam
    this._marker = null; // { cycleId, type: "at", idx } | { cycleId, type: "gap" }
    this._keep = "both";
    this._confirm = null; // cycle_id waarvoor om bevestiging van verwijderen wordt gevraagd
    this._days = 10;
    this._busy = false;
    this._status = null; // { text, error }
    this._error = null;
    this._built = false;
    this._dims = WIDE;
    this._resize = new ResizeObserver(() => {
      const dims = this.clientWidth && this.clientWidth < NARROW_BELOW ? NARROW : WIDE;
      if (dims !== this._dims) {
        this._dims = dims;
        this._render();
      }
    });
  }

  disconnectedCallback() {
    this._resize.disconnect();
  }

  // Home Assistant zet deze eigenschappen; hass verandert bij elke toestandswijziging,
  // dus daarop wordt niet opnieuw getekend.
  set hass(hass) {
    const first = this._hass === null;
    this._hass = hass;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.hass = hass;
    if (first && this.isConnected) this._load();
  }

  get hass() {
    return this._hass;
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    if (menu) menu.narrow = narrow;
  }

  connectedCallback() {
    if (!this._built) {
      this._build();
      this._built = true;
    }
    this._dims = this.clientWidth && this.clientWidth < NARROW_BELOW ? NARROW : WIDE;
    this._resize.observe(this);
    if (this._hass && !this._data) this._load();
  }

  get _t() {
    const language = (this._hass && (this._hass.locale?.language || this._hass.language)) || "nl";
    return language.toLowerCase().startsWith("nl") ? TEXT.nl : TEXT.en;
  }

  get _locale() {
    return this._t === TEXT.nl ? "nl-NL" : "en-GB";
  }

  // ---------------------------------------------------------------- gegevens

  async _load() {
    try {
      this._data = await this._hass.callWS({ type: `${DOMAIN}/cycles` });
      this._error = null;
    } catch (err) {
      this._error = err?.message || String(err);
    }
    const appliances = this._data?.appliances || [];
    if (!appliances.some((a) => a.entry_id === this._selected)) {
      this._selected = appliances.length ? appliances[0].entry_id : null;
    }
    if (this._marker && !this._cycle(this._marker.cycleId)) this._marker = null;
    if (this._confirm && !this._cycle(this._confirm)) this._confirm = null;
    this._render();
  }

  /** Roep een actie van de integratie aan. Alleen acties die een antwoord geven mogen erom vragen. */
  async _service(service, data, wantResponse = false) {
    const message = { type: "call_service", domain: DOMAIN, service, service_data: data };
    if (wantResponse) message.return_response = true;
    const result = await this._hass.callWS(message);
    return result?.response;
  }

  async _run(action, done) {
    if (this._busy) return;
    this._busy = true;
    this._status = null;
    this._render();
    try {
      const response = await action();
      this._status = { text: done(response), error: false };
    } catch (err) {
      this._status = { text: err?.message || String(err), error: true };
    }
    this._busy = false;
    // ook als melding onderin, want de statusregel staat bovenaan de pagina
    this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message: this._status.text }, bubbles: true, composed: true }));
    await this._load();
  }

  get _appliance() {
    return (this._data?.appliances || []).find((a) => a.entry_id === this._selected) || null;
  }

  _cycle(cycleId) {
    return this._appliance?.cycles.find((c) => c.cycle_id === cycleId) || null;
  }

  // ---------------------------------------------------------------- rekenen

  /** Lengte in seconden van een blok in het verloop van deze cyclus: 30, of 300 bij oudere cycli. */
  _slot(cycle) {
    return cycle?.step_s || this._data?.slot_s || 300;
  }

  /** Verbruik (kWh) en duur (s) van de profielblokken [first, stop). */
  _part(cycle, first, stop) {
    const slot = this._slot(cycle);
    if (stop <= first) return { duration: 0, kwh: 0 };
    const start = cycle.start + first * slot;
    const active = cycle.duration_s ?? cycle.end - cycle.start; // zonder pauzes
    const end = Math.min(cycle.start + active, cycle.start + stop * slot);
    let wh = 0;
    for (let i = first; i < stop; i += 1) {
      const length = Math.min(slot, active - i * slot);
      wh += (cycle.series[i] * Math.max(0, length)) / 3600;
    }
    return { duration: end - start, kwh: wh / 1000, start, end };
  }

  /** De twee delen bij de huidige markering: [deel1, deel2]. */
  _parts(cycle) {
    const n = cycle.series.length;
    if (this._marker.type === "gap") {
      const [first, count] = cycle.quiet_gap;
      return [this._part(cycle, 0, first), this._part(cycle, first + count, n)];
    }
    return [this._part(cycle, 0, this._marker.idx), this._part(cycle, this._marker.idx, n)];
  }

  // ---------------------------------------------------------------- opmaak

  /** Duur; met `exact` worden halve minuten als seconden getoond in plaats van afgerond. */
  _duration(seconds, exact = false) {
    const total = exact ? Math.round(seconds) : Math.round(seconds / 60) * 60;
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const rest = total % 60 ? ` ${total % 60} s` : "";
    if (this._t === TEXT.nl) return (h ? `${h}u${String(m).padStart(2, "0")}` : `${m} min`) + rest;
    return (h ? `${h}h ${String(m).padStart(2, "0")}m` : `${m} min`) + rest;
  }

  _kwh(value) {
    return `${value.toLocaleString(this._locale, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} kWh`;
  }

  _clock(ts, seconds = false) {
    const options = { hour: "2-digit", minute: "2-digit" };
    if (seconds) options.second = "2-digit";
    return new Date(ts * 1000).toLocaleTimeString(this._locale, options);
  }

  _day(ts) {
    return new Date(ts * 1000).toLocaleDateString(this._locale, { weekday: "short", day: "numeric", month: "short" });
  }

  // ---------------------------------------------------------------- tekenen

  _build() {
    this.shadowRoot.innerHTML = `<style>${STYLE}</style>
      <header><ha-menu-button></ha-menu-button><h1></h1></header>
      <main></main>`;
    const menu = this.shadowRoot.querySelector("ha-menu-button");
    menu.hass = this._hass;
    menu.narrow = this._narrow;
    const main = this.shadowRoot.querySelector("main");
    main.addEventListener("click", (event) => this._onClick(event));
    main.addEventListener("input", (event) => this._onInput(event));
    main.addEventListener("change", (event) => this._onChange(event));
    main.addEventListener("keydown", (event) => this._onKey(event));
    main.addEventListener("pointermove", (event) => this._onHover(event));
    main.addEventListener("pointerleave", (event) => this._onHover(event), true);
    this._render();
  }

  _render() {
    if (!this._built) return;
    const t = this._t;
    this.shadowRoot.querySelector("h1").textContent = t.title;
    const main = this.shadowRoot.querySelector("main");
    const active = this.shadowRoot.activeElement;
    const focus = active && active.dataset ? { key: active.dataset.focus, start: active.selectionStart } : null;

    if (this._error) {
      main.innerHTML = `<div class="card banner" role="alert">${esc(this._error)}</div>`;
      return;
    }
    if (!this._data) {
      main.innerHTML = `<p class="muted">${esc(t.loading)}</p>`;
      return;
    }
    const appliance = this._appliance;
    if (!appliance) {
      main.innerHTML = `<div class="card">${esc(t.noAppliances)}</div>`;
      return;
    }

    const unknown = appliance.cycles.filter((c) => !c.program);
    const filter = this._filter || (unknown.length ? "unknown" : "all");
    const shown = (filter === "unknown" ? unknown : appliance.cycles).slice().sort((a, b) => b.start - a.start);

    const tabs = this._data.appliances
      .map(
        (a) => `<button class="tab" role="tab" aria-selected="${a.entry_id === appliance.entry_id}" data-action="appliance"
          data-id="${esc(a.entry_id)}">${esc(a.name)}${a.running ? ` · ${esc(t.running)}` : ""}</button>`
      )
      .join("");

    const programs = appliance.programs.length
      ? `<ul class="programs">${appliance.programs
          .map(
            (p) => `<li><b>${esc(p.name)}</b> <span class="muted">${esc(t.times(p.count))} · ${esc(this._duration(p.duration_s))} · ${esc(
              this._kwh(p.energy_kwh)
            )}</span></li>`
          )
          .join("")}</ul>`
      : `<p class="muted">${esc(t.noPrograms)}</p>`;

    const status = this._status
      ? `<p class="status ${this._status.error ? "error" : ""}" role="status">${esc(this._status.text)}</p>`
      : `<p class="status" role="status">${this._busy ? esc(t.importing) : ""}</p>`;

    const empty = appliance.cycles.length ? t.noUnknown : t.noCycles;

    main.innerHTML = `
      <div class="tabs" role="tablist">${tabs}</div>
      <div class="card">
        <div class="row">
          <label>${esc(t.importDays)}
            <input type="number" min="1" max="730" step="1" value="${esc(this._days)}" data-field="days" data-focus="days">
          </label>
          <button class="primary" data-action="import" ${this._busy ? "disabled" : ""}>${esc(t.importButton)}</button>
          <button data-action="refresh" ${this._busy ? "disabled" : ""}>${esc(t.refresh)}</button>
        </div>
        ${status}
      </div>
      <div class="card"><h2>${esc(t.programs)}</h2>${programs}</div>
      <div class="tabs">
        <button class="chip" aria-pressed="${filter === "unknown"}" data-action="filter" data-id="unknown">${esc(t.filterUnknown(unknown.length))}</button>
        <button class="chip" aria-pressed="${filter === "all"}" data-action="filter" data-id="all">${esc(t.filterAll(appliance.cycles.length))}</button>
      </div>
      <datalist id="ea-programs">${appliance.programs.map((p) => `<option value="${esc(p.name)}"></option>`).join("")}</datalist>
      ${shown.length ? shown.map((c) => this._cycleCard(c)).join("") : `<div class="card muted">${esc(empty)}</div>`}`;

    if (focus && focus.key) {
      const target = main.querySelector(`[data-focus="${CSS.escape(focus.key)}"]`);
      if (target) {
        target.focus();
        if (focus.start !== null && focus.start !== undefined && target.type === "text") target.setSelectionRange(focus.start, focus.start);
      }
    }
  }

  _cycleCard(cycle) {
    const t = this._t;
    const typed = this._names[cycle.cycle_id];
    const value = typed !== undefined ? typed : cycle.program || "";
    const canSave = value.trim() !== "" && value.trim() !== (cycle.program || "") && !this._busy;
    const marked = this._marker && this._marker.cycleId === cycle.cycle_id;
    return `
      <section class="card" data-cycle="${esc(cycle.cycle_id)}">
        <div class="cycle-head">
          <span class="when">${esc(this._day(cycle.start))} ${esc(this._clock(cycle.start))}–${esc(this._clock(cycle.end))}</span>
          <span class="num">${esc(this._duration(cycle.duration_s ?? cycle.end - cycle.start))}</span>
          ${cycle.paused_s ? `<span class="num muted">${esc(t.paused(this._duration(cycle.paused_s)))}</span>` : ""}
          <span class="num">${esc(this._kwh(cycle.energy_kwh))}</span>
          <span class="num muted">${esc(t.peak)} ${Math.round(cycle.peak_w)} W</span>
          <span class="badge ${cycle.program ? "named" : ""}">${esc(cycle.program || t.unknown)}</span>
        </div>
        ${this._chart(cycle)}
        <p class="readout muted" data-readout>${esc(t.chartHint)}</p>
        <div class="row">
          <label class="grow">${esc(t.name)}
            <input type="text" list="ea-programs" maxlength="60" autocomplete="off" placeholder="${esc(t.namePlaceholder)}"
              value="${esc(value)}" data-field="name" data-focus="name-${esc(cycle.cycle_id)}">
          </label>
          <button class="primary" data-action="save" ${canSave ? "" : "disabled"}>${esc(t.save)}</button>
          <button data-action="restore" title="${esc(t.refetchTitle)}" ${this._busy ? "disabled" : ""}>${esc(cycle.has_original ? t.undo : t.refetch)}</button>
          <button class="danger" data-action="remove" data-focus="remove-${esc(cycle.cycle_id)}" ${this._busy ? "disabled" : ""}>${esc(t.remove)}</button>
        </div>
        ${
          this._confirm === cycle.cycle_id
            ? `<div class="confirm" role="alertdialog" aria-label="${esc(t.remove)}">
                <p>${esc(t.removeAsk)}</p>
                <div class="row">
                  <button class="danger solid" data-action="remove-yes" data-focus="remove-yes-${esc(cycle.cycle_id)}" ${this._busy ? "disabled" : ""}>${esc(t.removeYes)}</button>
                  <button data-action="remove-no">${esc(t.cancel)}</button>
                </div>
              </div>`
            : ""
        }
        ${marked ? this._splitPanel(cycle) : ""}
      </section>`;
  }

  _chart(cycle) {
    const t = this._t;
    const slot = this._slot(cycle);
    const n = cycle.series.length;
    const peak = Math.max(...cycle.series, 1);
    const magnitude = 10 ** Math.floor(Math.log10(peak));
    const top = [1, 2, 2.5, 5, 10].map((f) => f * magnitude).find((v) => v >= peak);
    const VB_W = this._dims.w;
    const VB_H = this._dims.h;
    const PLOT_W = VB_W - PLOT.left - PLOT.right;
    const PLOT_H = VB_H - PLOT.top - PLOT.bottom;
    const x = (i) => PLOT.left + (i / n) * PLOT_W;
    const y = (w) => PLOT.top + PLOT_H - (w / top) * PLOT_H;
    const base = PLOT.top + PLOT_H;

    let path = `M${x(0).toFixed(1)},${base}`;
    cycle.series.forEach((w, i) => {
      path += ` L${x(i).toFixed(1)},${y(w).toFixed(1)} L${x(i + 1).toFixed(1)},${y(w).toFixed(1)}`;
    });
    path += ` L${x(n).toFixed(1)},${base} Z`;

    const grid = [0, 0.5, 1]
      .map((f) => {
        const gy = y(top * f);
        return `<line class="grid" x1="${PLOT.left}" x2="${VB_W - PLOT.right}" y1="${gy}" y2="${gy}"></line>
          <text x="${PLOT.left - 6}" y="${gy + 4}" text-anchor="end">${Math.round(top * f)} W</text>`;
      })
      .join("");

    // tijdlabels op hele blokken
    const step = Math.max(1, Math.ceil(n / this._dims.ticks));
    let ticks = "";
    for (let i = 0; i <= n; i += step) {
      const anchor = i === 0 ? "start" : i >= n - step / 2 ? "end" : "middle";
      ticks += `<text x="${x(i).toFixed(1)}" y="${VB_H - 6}" text-anchor="${anchor}">${esc(this._clock(cycle.start + i * slot))}</text>`;
    }

    const marker = this._marker && this._marker.cycleId === cycle.cycle_id ? this._marker : null;
    let bands = "";
    if (cycle.quiet_gap && !(marker && marker.type === "gap")) {
      const [first, count] = cycle.quiet_gap;
      bands += `<rect class="quiet" x="${x(first)}" y="${PLOT.top}" width="${x(first + count) - x(first)}" height="${PLOT_H}"></rect>`;
    }
    let lines = "";
    if (marker && marker.type === "gap") {
      const [first, count] = cycle.quiet_gap;
      bands += `<rect class="cut" x="${x(first)}" y="${PLOT.top}" width="${x(first + count) - x(first)}" height="${PLOT_H}"></rect>`;
      lines += `<line class="marker" x1="${x(first)}" x2="${x(first)}" y1="${PLOT.top}" y2="${base}"></line>
        <line class="marker" x1="${x(first + count)}" x2="${x(first + count)}" y1="${PLOT.top}" y2="${base}"></line>`;
    } else if (marker) {
      lines += `<line class="marker" x1="${x(marker.idx)}" x2="${x(marker.idx)}" y1="${PLOT.top}" y2="${base}"></line>`;
    }

    return `<svg class="chart" viewBox="0 0 ${VB_W} ${VB_H}" role="img" tabindex="0" data-focus="chart-${esc(cycle.cycle_id)}"
        aria-label="${esc(t.chartLabel)}. ${esc(t.chartHint)}">
      ${grid}${bands}<path class="area" d="${path}"></path>${ticks}${lines}
      <line class="hover" data-hover x1="0" x2="0" y1="${PLOT.top}" y2="${base}" visibility="hidden"></line>
    </svg>`;
  }

  _splitPanel(cycle) {
    const t = this._t;
    const slot = this._slot(cycle);
    const [first, second] = this._parts(cycle);
    const gap = this._marker.type === "gap";
    const at = gap ? cycle.quiet_gap[0] : this._marker.idx;
    const keep = this._keep;
    const fine = slot < 60;
    const part = (label, p, kept) =>
      `<span class="${kept ? "" : "off"}">${esc(label)}: ${esc(this._clock(p.start, fine))}–${esc(this._clock(p.end, fine))} · ${esc(this._duration(p.duration, true))} · ${esc(
        this._kwh(p.kwh)
      )}${kept ? "" : ` (${esc(t.dropped)})`}</span>`;
    const radio = (value, label) =>
      `<label class="radio"><input type="radio" name="keep-${esc(cycle.cycle_id)}" value="${value}" data-field="keep" ${keep === value ? "checked" : ""}>${esc(label)}</label>`;
    const minutes = (at * slot) / 60;
    return `
      <div class="split">
        <div class="row">
          <label>${esc(t.splitMinutes)}
            <input type="number" min="${slot / 60}" max="${(cycle.series.length - 1) * (slot / 60)}" step="${slot / 60}" value="${minutes}"
              data-field="minutes" data-focus="minutes-${esc(cycle.cycle_id)}">
          </label>
          <span class="grow"><b>${esc(t.splitAt)} ${esc(this._clock(cycle.start + at * slot, fine))}</b>
            <span class="muted">(${esc(this._duration(at * slot, true))} ${esc(t.afterStart)})</span></span>
          ${cycle.quiet_gap && !gap ? `<button data-action="quiet">${esc(t.useQuiet)}</button>` : ""}
        </div>
        <div class="parts">${part(t.part1, first, keep !== "second")}${part(t.part2, second, keep !== "first")}</div>
        <fieldset><legend>${esc(t.keep)}</legend>
          ${radio("both", t.keepBoth)}${radio("first", t.keepFirst)}${radio("second", t.keepSecond)}
        </fieldset>
        <p class="muted">${gap ? `${esc(t.quietNote)} ` : ""}${esc(fine ? t.stepFine : t.stepCoarse)} ${esc(t.undoNote)}</p>
        <div class="row">
          <button class="primary" data-action="split" ${this._busy ? "disabled" : ""}>${esc(t.split)}</button>
          <button data-action="cancel">${esc(t.cancel)}</button>
        </div>
      </div>`;
  }

  // ---------------------------------------------------------------- bediening

  _cycleOf(target) {
    const section = target.closest("[data-cycle]");
    return section ? this._cycle(section.dataset.cycle) : null;
  }

  /** Blokgrens (1 … n-1) die het dichtst bij de aanwijzer ligt. */
  _slotAt(svg, cycle, clientX) {
    const rect = svg.getBoundingClientRect();
    const vbX = ((clientX - rect.left) / rect.width) * this._dims.w;
    const fraction = (vbX - PLOT.left) / (this._dims.w - PLOT.left - PLOT.right);
    const n = cycle.series.length;
    return Math.min(n - 1, Math.max(1, Math.round(fraction * n)));
  }

  _setMarker(cycle, idx) {
    if (cycle.series.length < 2) return;
    this._marker = { cycleId: cycle.cycle_id, type: "at", idx: Math.min(cycle.series.length - 1, Math.max(1, idx)) };
    this._render();
  }

  _onClick(event) {
    const svg = event.target.closest("svg.chart");
    if (svg) {
      const cycle = this._cycleOf(svg);
      if (!cycle) return;
      if (!this._marker || this._marker.cycleId !== cycle.cycle_id) this._keep = "both";
      this._setMarker(cycle, this._slotAt(svg, cycle, event.clientX));
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) return;
    const action = button.dataset.action;
    const cycle = this._cycleOf(button);
    const appliance = this._appliance;

    if (action === "appliance") {
      this._selected = button.dataset.id;
      this._filter = null;
      this._marker = null;
      this._confirm = null;
      this._status = null;
      this._render();
    } else if (action === "filter") {
      this._filter = button.dataset.id;
      this._render();
    } else if (action === "refresh") {
      this._status = null;
      this._load();
    } else if (action === "import") {
      const days = Math.min(730, Math.max(1, Math.round(Number(this._days) || 10)));
      this._run(
        () => this._service("import_history", { entry: appliance.entry_id, days }, true),
        (response) => this._t.imported(response?.imported ?? 0)
      );
    } else if (action === "save" && cycle) {
      const name = (this._names[cycle.cycle_id] ?? "").trim();
      if (!name) return;
      this._run(
        () => this._service("label_cycle", { appliance: appliance.entry_id, program: name, cycle_id: cycle.cycle_id }),
        () => {
          delete this._names[cycle.cycle_id];
          return this._t.saved(name);
        }
      );
    } else if (action === "restore" && cycle) {
      this._run(
        () => this._service("restore_cycle", { appliance: appliance.entry_id, cycle_id: cycle.cycle_id }, true),
        (response) => {
          if (this._marker && this._marker.cycleId === cycle.cycle_id) this._marker = null;
          return this._t.restored(response?.cycles?.length ?? 1);
        }
      );
    } else if (action === "remove" && cycle) {
      this._confirm = cycle.cycle_id;
      this._render();
      this.shadowRoot.querySelector(`[data-focus="remove-yes-${CSS.escape(cycle.cycle_id)}"]`)?.focus();
    } else if (action === "remove-no") {
      this._confirm = null;
      this._render();
    } else if (action === "remove-yes" && cycle) {
      this._run(
        () => this._service("delete_cycle", { appliance: appliance.entry_id, cycle_id: cycle.cycle_id }),
        () => {
          this._confirm = null;
          delete this._names[cycle.cycle_id];
          if (this._marker && this._marker.cycleId === cycle.cycle_id) this._marker = null;
          return this._t.removed;
        }
      );
    } else if (action === "quiet" && cycle) {
      this._marker = { cycleId: cycle.cycle_id, type: "gap" };
      this._render();
    } else if (action === "cancel") {
      this._marker = null;
      this._render();
    } else if (action === "split" && cycle && this._marker) {
      const data = { appliance: appliance.entry_id, cycle_id: cycle.cycle_id, keep: this._keep };
      if (this._marker.type === "at") data.minutes = (this._marker.idx * this._slot(cycle)) / 60;
      this._run(
        () => this._service("split_cycle", data, true),
        () => {
          this._marker = null;
          return this._t.splitDone;
        }
      );
    }
  }

  _onInput(event) {
    const field = event.target.dataset?.field;
    if (field === "name") {
      const cycle = this._cycleOf(event.target);
      if (!cycle) return;
      this._names[cycle.cycle_id] = event.target.value;
      const value = event.target.value.trim();
      const save = event.target.closest("[data-cycle]").querySelector('[data-action="save"]');
      save.disabled = value === "" || value === (cycle.program || "") || this._busy;
    } else if (field === "days") {
      this._days = event.target.value;
    }
  }

  _onChange(event) {
    const field = event.target.dataset?.field;
    const cycle = this._cycleOf(event.target);
    if (field === "keep") {
      this._keep = event.target.value;
      this._render();
    } else if (field === "minutes" && cycle) {
      const minutes = Number(event.target.value);
      if (Number.isFinite(minutes)) this._setMarker(cycle, Math.round((minutes * 60) / this._slot(cycle)));
    }
  }

  _onKey(event) {
    if (event.key === "Enter" && event.target.dataset?.field === "name") {
      const save = event.target.closest("[data-cycle]").querySelector('[data-action="save"]');
      if (!save.disabled) save.click();
      return;
    }
    const svg = event.target.closest?.("svg.chart");
    if (!svg || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    const cycle = this._cycleOf(svg);
    if (!cycle) return;
    event.preventDefault();
    const current =
      this._marker && this._marker.cycleId === cycle.cycle_id && this._marker.type === "at"
        ? this._marker.idx
        : Math.round(cycle.series.length / 2);
    const jump = event.shiftKey ? Math.max(1, Math.round(300 / this._slot(cycle))) : 1;
    this._setMarker(cycle, current + (event.key === "ArrowRight" ? jump : -jump));
  }

  _onHover(event) {
    const svg = event.target.closest?.("svg.chart");
    if (!svg) return;
    const cycle = this._cycleOf(svg);
    const line = svg.querySelector("[data-hover]");
    const readout = svg.closest("[data-cycle]").querySelector("[data-readout]");
    if (!cycle || event.type === "pointerleave") {
      line.setAttribute("visibility", "hidden");
      readout.textContent = this._t.chartHint;
      return;
    }
    const idx = this._slotAt(svg, cycle, event.clientX);
    const px = PLOT.left + (idx / cycle.series.length) * (this._dims.w - PLOT.left - PLOT.right);
    line.setAttribute("x1", px);
    line.setAttribute("x2", px);
    line.setAttribute("visibility", "visible");
    const before = Math.round(cycle.series[idx - 1]);
    const after = Math.round(cycle.series[idx]);
    const step = this._slot(cycle);
    readout.textContent = `${this._clock(cycle.start + idx * step, step < 60)} · ${this._duration(idx * step, true)} ${this._t.afterStart} · ${before} W → ${after} W`;
  }
}

if (!customElements.get("energie-adviseur-panel")) {
  customElements.define("energie-adviseur-panel", EnergieAdviseurPanel);
}
