/* Energie Adviseur card v1.0.0
 * Eén overzicht van de integratie Energie Adviseur voor het mobiele dashboard (/mobile-first, pop-up #energie):
 * de wasdag (aantal wasjes, was- en droogprogramma per ronde, de planning van wasmachine en droger door elkaar)
 * en per apparaat het programma en het beste startmoment.
 * Leest alleen entiteiten en schrijft via acties, zodat het ook werkt voor wie geen beheerder is.
 * Vormgeving: de tokens van "Thuis" (--th-*), met de kleuren van Home Assistant als reserve.
 *
 * type: custom:energie-adviseur-card
 * laundry:                       # wasdag; weglaten als er geen wasmachine met droger is
 *   appliance: Wasmachine        # naam van de wasmachine in Energie Adviseur
 *   prefix: wasmachine           # begin van de entiteiten: number.<prefix>_aantal_wasjes_vandaag, sensor.<prefix>_wasplanning
 * appliances:
 *   - { name: Wasmachine, prefix: wasmachine, icon: mdi:washing-machine }
 *   - { name: Droger, prefix: droger, icon: mdi:tumble-dryer, also_select: select.wasmachine_droogprogramma }
 *   - { name: Vaatwasser, prefix: vaatwasser, icon: mdi:dishwasher }
 */
(() => {
  const TAG = "energie-adviseur-card";
  if (customElements.get(TAG)) return;

  const STYLE = `
    :host {
      display: block;
      --ea-surface: var(--th-surface, var(--card-background-color, #fff));
      --ea-sunken: var(--th-sunken, var(--secondary-background-color, #e1e8ee));
      --ea-line: var(--th-line, var(--divider-color, #d3dce4));
      --ea-ink: var(--th-ink, var(--primary-text-color, #12202e));
      --ea-muted: var(--th-muted, var(--secondary-text-color, #566576));
      --ea-on: var(--th-apparaat, #d24c8f);
      --ea-on-soft: var(--th-apparaat-soft, #f9e0ed);
      --ea-on-ink: var(--th-on-apparaat, #fff);
      --ea-good: var(--th-energie, #1e9a5c);
      --ea-good-soft: var(--th-energie-soft, #d6f2e3);
      --ea-good-ink: var(--th-on-energie, #fff);
      --ea-warn: var(--th-let-op-text, #b33811);
      --ea-font: var(--th-font, system-ui, sans-serif);
      --ea-display: var(--th-display, var(--ea-font));
      font-family: var(--ea-font);
      color: var(--ea-ink);
      font-size: 15px;
      line-height: 20px;
    }
    * { box-sizing: border-box; }
    .stack { display: flex; flex-direction: column; gap: 8px; }
    .label { display: flex; align-items: center; gap: 12px; padding: 20px 4px 2px; font-weight: 650; }
    .label::after { content: ""; flex: 1 1 auto; height: 1px; background: var(--ea-line); }
    .label:first-child { padding-top: 4px; }
    .panel { background: var(--ea-surface); border-radius: 24px; padding: 16px; }
    .muted { color: var(--ea-muted); }
    .small { font-size: 13px; line-height: 18px; }
    .num { font-variant-numeric: tabular-nums; }
    .warn { color: var(--ea-warn); }
    p { margin: 0; }

    /* aantal wasjes */
    .count { display: flex; align-items: center; gap: 12px; }
    .count .what { flex: 1 1 auto; min-width: 0; }
    .count .what b { display: block; font-weight: 600; }
    .stepper { display: flex; align-items: center; gap: 4px; flex: none; }
    .stepper output { font-family: var(--ea-display); font-size: 26px; line-height: 30px; font-weight: 600; min-width: 32px; text-align: center; }
    button.round {
      width: 44px; height: 44px; border-radius: 999px; border: 0; padding: 0; cursor: pointer;
      background: var(--ea-sunken); color: var(--ea-ink); display: grid; place-items: center; --mdc-icon-size: 22px;
      transition: background-color .2s ease;
    }
    button.round:disabled { opacity: .4; cursor: default; }
    button:focus-visible, select:focus-visible { outline: 2px solid var(--ea-ink); outline-offset: 2px; }

    /* keuzelijst als pil */
    .pick { position: relative; display: block; min-width: 0; }
    .pick select {
      appearance: none; -webkit-appearance: none; width: 100%; height: 44px; border: 0; border-radius: 999px;
      background: var(--ea-sunken); color: var(--ea-ink); font: inherit; font-weight: 600; font-size: 14px;
      padding: 0 36px 0 16px; cursor: pointer; text-overflow: ellipsis;
    }
    .pick select:disabled { opacity: .55; cursor: default; }
    .pick ha-icon { position: absolute; right: 12px; top: 11px; pointer-events: none; color: var(--ea-muted); --mdc-icon-size: 22px; }
    .pick.own select { background: var(--ea-on-soft); }

    /* rondes */
    .rounds { display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 12px; align-items: start; margin-top: 16px; }
    .pair { display: flex; flex-wrap: wrap; gap: 8px; min-width: 0; }
    .pair .pick { flex: 1 1 190px; }
    .pick .pre { position: absolute; left: 14px; top: 12px; right: auto; color: var(--ea-muted); pointer-events: none; --mdc-icon-size: 20px; }
    .pick.pre select { padding-left: 44px; }
    .disc {
      width: 40px; height: 40px; border-radius: 999px; background: var(--ea-sunken); display: grid; place-items: center;
      font-family: var(--ea-display); font-weight: 600; font-size: 17px; --mdc-icon-size: 22px; flex: none;
    }
    .disc.on { background: var(--ea-on); color: var(--ea-on-ink); }
    .disc.good { background: var(--ea-good); color: var(--ea-good-ink); }
    .pair .state { flex: 1 1 100%; padding-left: 16px; }
    .after { margin-top: 12px; }

    /* planning */
    .next { font-family: var(--ea-display); font-size: 22px; line-height: 28px; font-weight: 600; letter-spacing: -0.01em; text-wrap: balance; }
    .lanes { margin-top: 16px; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 10px; align-items: center; }
    .lanes .name { font-size: 13px; line-height: 18px; font-weight: 600; color: var(--ea-muted); }
    .lane { position: relative; height: 32px; border-radius: 999px; background: var(--ea-sunken); overflow: hidden; }
    .bar {
      position: absolute; top: 0; bottom: 0; border-radius: 999px; background: var(--ea-on); color: var(--ea-on-ink);
      display: grid; place-items: center; font-weight: 600; font-size: 13px; min-width: 6px; overflow: hidden;
      box-shadow: 0 0 0 2px var(--ea-sunken);
    }
    .axis { position: relative; height: 18px; font-size: 12px; line-height: 18px; color: var(--ea-muted); }
    .axis span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
    .axis span:first-child { transform: none; }
    .axis span:last-child { transform: translateX(-100%); }
    .steps { list-style: none; margin: 14px 0 0; padding: 0; }
    .steps li { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 2px 12px; padding: 10px 0; border-top: 1px solid var(--ea-line); align-items: baseline; }
    .steps .when { font-weight: 600; }
    .steps .what { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .steps .what b { font-weight: 600; }
    .total { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; padding-top: 12px; border-top: 1px solid var(--ea-line); }
    .total b { font-family: var(--ea-display); font-size: 20px; line-height: 24px; font-weight: 600; }
    .saving { color: var(--ea-good); font-weight: 600; }
    .note { margin-top: 12px; }

    /* apparaten */
    .device { display: grid; grid-template-columns: 40px minmax(0, 1fr); gap: 12px; align-items: start; }
    .device + .device { margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--ea-line); }
    .device .top { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 40px; }
    .device .top b { font-weight: 600; }
    .badge { border-radius: 999px; padding: 4px 10px; font-size: 13px; line-height: 18px; font-weight: 600; background: var(--ea-sunken); white-space: nowrap; }
    .badge.good { background: var(--ea-good-soft); color: var(--ea-ink); }
    .badge.on { background: var(--ea-on-soft); color: var(--ea-ink); }
    .device .pick { margin-top: 4px; }
    .facts { margin-top: 12px; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 0 12px; align-items: baseline; }
    .facts .day { font-size: 13px; line-height: 18px; font-weight: 600; color: var(--ea-muted); min-width: 56px; }
    .facts .t { font-family: var(--ea-display); font-weight: 600; font-size: 20px; line-height: 24px; }
    .facts .more { grid-column: 2 / -1; margin-bottom: 8px; }
    .facts .more:last-child { margin-bottom: 0; }
    @media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
  `;

  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const known = (s) => s && !["unknown", "unavailable", ""].includes(s.state);
  const euro = (v) => `€ ${Number(v).toFixed(2).replace(".", ",")}`;
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();

  class EnergieAdviseurCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this._hass = null;
      this._config = null;
      this._seen = null; // handtekening van de entiteiten waar de kaart naar kijkt
      this._dirty = false; // er is iets veranderd terwijl een keuzelijst open stond
      this._busy = null; // tijdstip van de laatste wijziging: de planning wordt opnieuw berekend
      this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
      this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
      this.shadowRoot.addEventListener("focusout", () => setTimeout(() => this._dirty && this._render(), 0));
    }

    setConfig(config) {
      if (!config || (!config.laundry && !(config.appliances || []).length)) {
        throw new Error("Geef 'laundry' en/of 'appliances' op");
      }
      this._config = config;
      this._seen = null;
      if (this._hass) this._update();
    }

    set hass(hass) {
      this._hass = hass;
      this._update();
    }

    getCardSize() {
      return 8;
    }

    getGridOptions() {
      return { columns: "full", rows: "auto" };
    }

    // ------------------------------------------------------------ entiteiten

    _laundryIds() {
      const l = this._config.laundry;
      if (!l) return null;
      const p = l.prefix;
      return {
        loads: l.loads || `number.${p}_aantal_wasjes_vandaag`,
        plan: l.plan || `sensor.${p}_wasplanning`,
        program: l.program || `select.${p}_programma_voor_advies`,
        dry: l.dry_program || `select.${p}_droogprogramma`,
      };
    }

    _applianceIds(a) {
      const p = a.prefix;
      return {
        program: a.program || `select.${p}_programma_voor_advies`,
        today: a.today || `sensor.${p}_beste_starttijd_vandaag`,
        tomorrow: a.tomorrow || `sensor.${p}_beste_starttijd_morgen`,
        best: a.best || `sensor.${p}_beste_starttijd`,
        good: a.good || `binary_sensor.${p}_nu_gunstig_om_te_starten`,
        status: a.status || `sensor.${p}_status`,
      };
    }

    _watched() {
      const ids = Object.values(this._laundryIds() || {});
      for (const a of this._config.appliances || []) ids.push(...Object.values(this._applianceIds(a)));
      return ids;
    }

    _s(id) {
      return this._hass.states[id];
    }

    _update() {
      if (!this._config || !this._hass) return;
      const seen = this._watched()
        .map((id) => {
          const s = this._s(id);
          return s ? `${s.state}|${s.last_updated}` : "-";
        })
        .join(";");
      if (seen === this._seen && !this._dirty) return;
      if (seen !== this._seen && this._seen !== null) this._busy = null;
      this._seen = seen;
      // niet opnieuw tekenen terwijl een keuzelijst open staat: die zou dichtklappen
      if (this.shadowRoot.activeElement && this.shadowRoot.activeElement.tagName === "SELECT") {
        this._dirty = true;
        return;
      }
      this._render();
    }

    // ------------------------------------------------------------ opmaak

    _clock(iso) {
      return new Date(iso).toLocaleTimeString("nl-NL", { hour: "2-digit", minute: "2-digit" });
    }

    /** Kloktijd, met 'morgen' of de weekdag ervoor als het niet vandaag is. */
    _when(iso) {
      const d = new Date(iso);
      const now = new Date();
      if (sameDay(d, now)) return this._clock(iso);
      const tomorrow = new Date(now);
      tomorrow.setDate(now.getDate() + 1);
      const day = sameDay(d, tomorrow) ? "morgen" : d.toLocaleDateString("nl-NL", { weekday: "short" });
      return `${day} ${this._clock(iso)}`;
    }

    _duration(a, b) {
      const min = Math.round((new Date(b) - new Date(a)) / 60000);
      return min >= 60 ? `${Math.floor(min / 60)}u${String(min % 60).padStart(2, "0")}` : `${min} min`;
    }

    _pick(field, options, value, { extra = "", label, own = false, disabled = false, first = null, pre = "" } = {}) {
      const list = (first ? [first] : []).concat(options.map((o) => ({ value: o, text: o })));
      return `<label class="pick ${own ? "own" : ""} ${pre ? "pre" : ""}">
        ${pre ? `<ha-icon class="pre" icon="${esc(pre)}" aria-hidden="true"></ha-icon>` : ""}<select data-field="${field}" ${extra} aria-label="${esc(label)}" ${disabled || !options.length ? "disabled" : ""}>
          ${list.map((o) => `<option value="${esc(o.value)}" ${o.value === value ? "selected" : ""}>${esc(o.text)}</option>`).join("")}
          ${options.length ? "" : `<option selected>Nog geen programma geleerd</option>`}
        </select><ha-icon icon="mdi:chevron-down"></ha-icon></label>`;
    }

    // ------------------------------------------------------------ wasdag

    _laundry() {
      const ids = this._laundryIds();
      if (!ids) return "";
      const loadsState = this._s(ids.loads);
      const plan = this._s(ids.plan);
      if (!loadsState || !plan) {
        return `<div class="label">Wasdag</div><div class="panel muted">De wasplanning van Energie Adviseur is niet gevonden.</div>`;
      }
      const a = plan.attributes;
      const loads = Math.round(Number(loadsState.state) || 0);
      const max = Number(loadsState.attributes.max ?? 6);
      const perRound = Array.isArray(a.rondes); // vanaf Energie Adviseur 0.10
      const washOptions = a.wasprogrammas || (this._s(ids.program)?.attributes.options ?? []);
      const dryOptions = a.droogprogrammas || (this._s(ids.dry)?.attributes.options ?? []);

      let rounds = "";
      if (perRound && a.rondes.length) {
        const standard = (name) => ({ value: "", text: name ? `${name} (standaard)` : "Standaard" });
        rounds = `<div class="rounds">
          ${a.rondes
            .map((r) => {
              const n = r.ronde;
              const defWash = this._s(ids.program)?.state;
              const defDry = this._s(ids.dry)?.state;
              return `<span class="disc ${r.was_draait ? "on" : ""}" aria-hidden="true">${esc(n)}</span>
                <div class="pair">${this._pick("round-wash", washOptions, r.eigen_wasprogramma ? r.wasprogramma : "", {
                  extra: `data-round="${esc(n)}"`, label: `Wasprogramma ronde ${n}`, own: r.eigen_wasprogramma, disabled: r.was_draait, pre: "mdi:washing-machine",
                  first: standard(known(this._s(ids.program)) ? defWash : r.wasprogramma),
                })}
                ${this._pick("round-dry", dryOptions, r.eigen_droogprogramma ? r.droogprogramma : "", {
                  extra: `data-round="${esc(n)}"`, label: `Droogprogramma ronde ${n}`, own: r.eigen_droogprogramma, pre: "mdi:tumble-dryer",
                  first: standard(known(this._s(ids.dry)) ? defDry : r.droogprogramma),
                })}
                ${r.was_draait ? `<span class="state small muted">Deze was draait nu.</span>` : ""}</div>`;
            })
            .join("")}
        </div>`;
      } else if (!perRound && loads > 0) {
        // oudere versie van de integratie: één was- en één droogprogramma voor alle rondes
        rounds = `<div class="rounds">
          <span class="disc" aria-hidden="true"><ha-icon icon="mdi:repeat"></ha-icon></span>
          <div class="pair">
          ${this._pick("select", washOptions, this._s(ids.program)?.state, { extra: `data-entity="${esc(ids.program)}"`, label: "Wasprogramma", pre: "mdi:washing-machine" })}
          ${this._pick("select", dryOptions, this._s(ids.dry)?.state, { extra: `data-entity="${esc(ids.dry)}"`, label: "Droogprogramma", pre: "mdi:tumble-dryer" })}
          <span class="state small muted">Een programma per ronde kiezen kan na de update van Energie Adviseur naar 0.10.</span>
          </div>
        </div>`;
      }

      const waiting = (a.droogbeurten_die_klaarliggen || []).filter(Boolean);
      const pending = Number(a.nog_te_drogen || 0);
      const after = pending
        ? `<p class="after small muted">Ligt klaar om te drogen: ${
            waiting.length ? esc(waiting.join(", ")) : `${pending} ${pending === 1 ? "was" : "wassen"}`
          }.</p>`
        : "";

      return `<div class="label">Wasdag</div>
        <div class="panel">
          <div class="count">
            <div class="what"><b>Wasjes vandaag</b>
              <span class="small muted">Elke was krijgt daarna een droogbeurt.</span></div>
            <div class="stepper">
              <button class="round" data-action="loads" data-delta="-1" aria-label="Eén wasje minder" ${loads <= 0 ? "disabled" : ""}><ha-icon icon="mdi:minus"></ha-icon></button>
              <output class="num" aria-live="polite" aria-label="Aantal wasjes">${loads}</output>
              <button class="round" data-action="loads" data-delta="1" aria-label="Eén wasje meer" ${loads >= max ? "disabled" : ""}><ha-icon icon="mdi:plus"></ha-icon></button>
            </div>
          </div>
          ${rounds}${after}
        </div>
        ${this._plan(plan, loads + pending)}`;
    }

    _plan(plan, todo) {
      const a = plan.attributes;
      const steps = (a.planning || []).slice().sort((x, y) => new Date(x.start) - new Date(y.start));
      if (!todo) return "";
      if (!steps.length) {
        return `<div class="panel"><p class="next">Geen planning mogelijk</p>
          <p class="note small ${a.toelichting ? "warn" : "muted"}">${esc(
            a.toelichting || "Er passen geen was- en droogbeurten meer binnen de stroomprijzen die nu bekend zijn."
          )}</p></div>`;
      }
      const t0 = Math.min(...steps.map((s) => +new Date(s.start)));
      const t1 = Math.max(...steps.map((s) => +new Date(s.eind)));
      const span = Math.max(1, t1 - t0);
      const pct = (t) => ((t - t0) / span) * 100;
      const isWash = (s) => (s.soort ? s.soort === "was" : s.stap.startsWith("Was"));
      const lane = (wash) =>
        steps
          .filter((s) => isWash(s) === wash)
          .map((s) => {
            const left = pct(+new Date(s.start));
            const width = pct(+new Date(s.eind)) - left;
            const n = s.ronde ?? (s.stap.match(/\d+/) || [""])[0];
            return `<span class="bar" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%" title="${esc(s.tekst)}">${esc(n)}</span>`;
          })
          .join("");
      // tijdas: begin, eind en hele uren ertussen (hooguit vier)
      const ticks = [t0];
      const hour = 3600000;
      const every = Math.max(1, Math.ceil(span / hour / 4));
      for (let t = Math.ceil(t0 / hour) * hour; t < t1; t += hour) {
        const far = (t - t0) / span > 0.12 && (t1 - t) / span > 0.12;
        if (far && new Date(t).getHours() % every === 0) ticks.push(t);
      }
      ticks.push(t1);
      const axis = ticks.map((t) => `<span class="num" style="left:${pct(t).toFixed(2)}%">${esc(this._clock(t))}</span>`).join("");

      const first = steps[0];
      const started = new Date(first.start) <= new Date();
      const list = steps
        .map(
          (s) => `<li>
            <span class="when num">${esc(this._when(s.start))}–${esc(this._clock(s.eind))}</span>
            <span class="what"><b>${esc(s.stap)}</b>${s.programma ? ` <span class="muted">· ${esc(s.programma)}</span>` : ""}</span>
            <span class="num muted">${s.kosten != null ? esc(euro(s.kosten)) : ""}</span>
          </li>`
        )
        .join("");
      const saving = Number(a.besparing);
      return `<div class="panel">
          <p class="small muted">${this._busy ? "Planning wordt bijgewerkt…" : "Goedkoopste planning"}</p>
          <p class="next">${esc(first.stap)} ${started ? "nu starten" : `starten om ${esc(this._when(first.start))}`}</p>
          <div class="lanes" role="img" aria-label="Tijdlijn van de wasmachine en de droger">
            <span class="name">Was</span><div class="lane">${lane(true)}</div>
            <span class="name">Droger</span><div class="lane">${lane(false)}</div>
            <span></span><div class="axis">${axis}</div>
          </div>
          <ul class="steps">${list}</ul>
          <div class="total">
            <span>Totaal <span class="muted small">· klaar ${esc(this._when(new Date(t1).toISOString()))}</span></span>
            <b class="num">${a.kosten != null ? esc(euro(a.kosten)) : ""}</b>
            ${saving > 0.005 ? `<span class="saving small" style="flex-basis:100%">${esc(euro(saving))} goedkoper dan alles zo vroeg mogelijk draaien</span>` : ""}
          </div>
          ${a.toelichting ? `<p class="note small warn">${esc(a.toelichting)}.</p>` : ""}
        </div>`;
    }

    // ------------------------------------------------------------ apparaten

    _device(a) {
      const ids = this._applianceIds(a);
      const program = this._s(ids.program);
      if (!program) return "";
      const today = this._s(ids.today);
      const tomorrow = this._s(ids.tomorrow);
      const best = this._s(ids.best);
      const running = this._s(ids.status)?.state === "running";
      const good = this._s(ids.good)?.state === "on";
      const rows = [];
      const row = (s, isToday) => {
        const at = s.attributes;
        const save = isToday ? Number(at.besparing_t_o_v_nu_starten ?? at.besparing) : NaN;
        const when = this._when(s.state).split(" ");
        const day = when.length > 1 ? when[0] : "vandaag";
        rows.push(`<span class="day">${esc(day[0].toUpperCase() + day.slice(1))}</span>
          <span class="t num">${esc(when[when.length - 1])}</span>
          <span class="num">${at.kosten != null ? esc(euro(at.kosten)) : ""}</span>
          <span class="more small muted">klaar ${esc(this._when(at.klaar_om))}${save > 0.005 ? ` · ${esc(euro(save))} goedkoper dan nu starten` : ""}</span>`);
      };
      if (known(today)) row(today, true);
      else if (known(best) && !known(tomorrow)) row(best, true);
      if (known(tomorrow)) row(tomorrow, false);
      const badge = running
        ? `<span class="badge on">Draait nu</span>`
        : good
          ? `<span class="badge good">Nu gunstig</span>`
          : "";
      const options = program.attributes.options || [];
      return `<div class="device">
          <span class="disc ${running ? "on" : good ? "good" : ""}" aria-hidden="true"><ha-icon icon="${esc(a.icon || "mdi:power-plug")}"></ha-icon></span>
          <div>
            <div class="top"><b>${esc(a.name)}</b>${badge}</div>
            ${this._pick("select", options, program.state, {
              extra: `data-entity="${esc(ids.program)}" ${a.also_select ? `data-also="${esc(a.also_select)}"` : ""}`,
              label: `Programma ${a.name}`,
            })}
            ${
              rows.length
                ? `<div class="facts">${rows.join("")}</div>`
                : `<p class="small muted" style="margin-top:10px">${
                    options.length ? "Nog geen advies: er zijn geen stroomprijzen bekend." : "Geef op de pagina Energie Adviseur een cyclus een naam; daarna volgt hier het advies."
                  }</p>`
            }
          </div>
        </div>`;
    }

    _render() {
      this._dirty = false;
      const devices = (this._config.appliances || []).map((a) => this._device(a)).join("");
      this.shadowRoot.innerHTML = `<style>${STYLE}</style>
        <div class="stack">
          ${this._laundry()}
          ${devices ? `<div class="label">Beste moment per apparaat</div><div class="panel">${devices}</div>` : ""}
        </div>`;
    }

    // ------------------------------------------------------------ bediening

    _call(domain, service, data) {
      this._busy = Date.now();
      // na een paar seconden is de planning hoe dan ook bijgewerkt
      setTimeout(() => {
        if (this._busy && Date.now() - this._busy >= 4000) {
          this._busy = null;
          this._seen = null;
          this._update();
        }
      }, 4100);
      return this._hass.callService(domain, service, data).catch((err) => {
        this._busy = null;
        this.dispatchEvent(
          new CustomEvent("hass-notification", { detail: { message: err?.message || String(err) }, bubbles: true, composed: true })
        );
        this._seen = null;
        this._update();
      });
    }

    _onClick(event) {
      const button = event.target.closest("button[data-action]");
      if (!button || button.disabled) return;
      if (button.dataset.action === "loads") {
        const ids = this._laundryIds();
        const state = this._s(ids.loads);
        const max = Number(state.attributes.max ?? 6);
        const value = Math.min(max, Math.max(0, Math.round(Number(state.state) || 0) + Number(button.dataset.delta)));
        this._call("number", "set_value", { entity_id: ids.loads, value });
      }
    }

    _onChange(event) {
      const select = event.target.closest("select[data-field]");
      if (!select) return;
      const field = select.dataset.field;
      if (field === "select") {
        this._call("select", "select_option", { entity_id: select.dataset.entity, option: select.value });
        // de droger: hetzelfde programma geldt als standaard voor de droogbeurten van de wasdag
        const also = select.dataset.also;
        if (also && (this._s(also)?.attributes.options || []).includes(select.value)) {
          this._call("select", "select_option", { entity_id: also, option: select.value });
        }
      } else if (field === "round-wash" || field === "round-dry") {
        const data = { appliance: this._config.laundry.appliance, round: Number(select.dataset.round) };
        data[field === "round-wash" ? "wash_program" : "dry_program"] = select.value;
        this._call("energie_adviseur", "set_round_program", data);
      }
      select.blur();
    }
  }

  customElements.define(TAG, EnergieAdviseurCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: TAG,
    name: "Energie Adviseur",
    description: "Wasdag met een programma per ronde en het beste startmoment per apparaat.",
  });
})();
