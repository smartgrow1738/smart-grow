"use strict";
// Smart-Grow dashboard. Polls /api/v1/status; all server data is written with
// textContent (never innerHTML), so nothing a node sends can inject markup.
(() => {
  const POLL_MS = 3000;
  const RETRY_MS = 8000;
  const TIMEOUT_MS = 5000;
  const WINDOW_S = 600;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);

  let timer = null;

  async function fetchJSON(url, options = {}) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const res = await fetch(url, { ...options, cache: "no-store", signal: ctrl.signal });
      const body = await res.json().catch(() => null);
      return { ok: res.ok, status: res.status, body };
    } finally {
      clearTimeout(t);
    }
  }

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }

  function svg(tag, attrs) {
    const e = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    return e;
  }

  function ago(s) {
    if (s === null || s === undefined) return "never";
    if (s < 60) return `${Math.round(s)}s ago`;
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} days ago`;
  }

  function duration(s) {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return h ? `${h}h ${m}m` : m ? `${m}m` : `${Math.round(s)}s`;
  }

  const fmt = (v, digits, unit) => (v === null || v === undefined ? "--" : `${v.toFixed(digits)}${unit}`);

  // ---- overall + checks -------------------------------------------------
  const OVERALL = {
    ok: ["Ready", "All checks passed. The system is sensing, deciding and logging."],
    warn: ["Running, with warnings", "It works, but look at the amber items below."],
    fail: ["Not ready", "Fix the red items below before relying on the system."],
  };

  function renderOverall(state, checks) {
    const box = $("overall");
    box.className = `overall state-${state}`;
    const [title, detail] = OVERALL[state] || ["Unknown", ""];
    const bad = checks.filter((c) => c.state !== "ok").map((c) => c.label);
    $("overall-title").textContent = title;
    $("overall-detail").textContent = bad.length ? `${detail} (${bad.join(", ")})` : detail;
  }

  function renderChecks(checks) {
    const items = checks.map((c) => {
      const li = el("li", `state-${c.state}`);
      li.append(el("span", "dot"));
      const body = el("div");
      body.append(el("strong", null, c.label), el("p", null, c.detail));
      if (c.fix) body.append(el("p", "fix", `Fix: ${c.fix}`));
      li.append(body);
      return li;
    });
    $("checks").replaceChildren(...items);
  }

  // ---- live reading -----------------------------------------------------
  function renderLatest(latest) {
    if (!latest) return;
    $("moisture").textContent = String(latest.moisture);
    const on = latest.decision === "PUMP_ON";
    const pump = $("pump");
    pump.className = `pump ${on ? "pump-on" : "pump-off"}`;
    pump.textContent = on ? "PUMP ON" : "PUMP OFF";
    $("reason").textContent = `AI: ${latest.reason}`;
    $("rate").textContent = fmt(latest.predicted_drying_rate, 2, " %/hr");
    $("eta").textContent = fmt(latest.hours_to_critical, 1, " h");
    $("temp").textContent = latest.temperature === null ? "not measured" : fmt(latest.temperature, 1, " C");
    $("age").textContent = ago(latest.age_s);
  }

  // ---- chart ------------------------------------------------------------
  function renderChart(history, critical) {
    const W = 640, H = 220, L = 36, R = 10, T = 10, B = 24;
    const x = (age) => L + (1 - age / WINDOW_S) * (W - L - R);
    const y = (m) => T + (1 - m / 100) * (H - T - B);
    const nodes = [];

    for (const v of [0, 25, 50, 75, 100]) {
      nodes.push(svg("line", { class: "grid-line", x1: L, x2: W - R, y1: y(v), y2: y(v) }));
      const label = svg("text", { class: "axis-label", x: L - 6, y: y(v) + 4, "text-anchor": "end" });
      label.textContent = `${v}%`;
      nodes.push(label);
    }
    for (const [age, text] of [[WINDOW_S, "-10 min"], [WINDOW_S / 2, "-5 min"], [0, "now"]]) {
      const label = svg("text", { class: "axis-label", x: x(age), y: H - 6,
        "text-anchor": age === 0 ? "end" : age === WINDOW_S ? "start" : "middle" });
      label.textContent = text;
      nodes.push(label);
    }
    nodes.push(svg("line", { class: "crit", x1: L, x2: W - R, y1: y(critical), y2: y(critical) }));

    const pts = history.filter((p) => p.age_s <= WINDOW_S);
    if (!pts.length) {
      const msg = svg("text", { class: "empty", x: (W + L) / 2, y: H / 2, "text-anchor": "middle" });
      msg.textContent = "No readings in the last 10 minutes";
      nodes.push(msg);
    }
    // Break the line where the node went quiet, instead of drawing across the gap.
    let run = [];
    const flush = () => {
      if (run.length > 1) nodes.push(svg("polyline", { class: "series", points: run.join(" ") }));
      run = [];
    };
    pts.forEach((p, i) => {
      if (i && pts[i - 1].age_s - p.age_s > 30) flush();
      run.push(`${x(p.age_s).toFixed(1)},${y(p.moisture).toFixed(1)}`);
    });
    flush();
    for (const p of pts) {
      if (p.decision === "PUMP_ON") nodes.push(svg("circle", { class: "on", cx: x(p.age_s), cy: y(p.moisture), r: 3.5 }));
    }
    $("chart").replaceChildren(...nodes);

    const onCount = pts.filter((p) => p.decision === "PUMP_ON").length;
    $("window-stats").textContent = pts.length
      ? `${pts.length} readings, ${onCount} pump-on decisions in this window.`
      : "";
  }

  // ---- table ------------------------------------------------------------
  function renderRows(history) {
    const recent = history.slice(-8).reverse();
    if (!recent.length) return;
    $("rows").replaceChildren(...recent.map((p) => {
      const tr = el("tr");
      const on = p.decision === "PUMP_ON";
      tr.append(
        el("td", null, ago(p.age_s)),
        el("td", null, `${p.moisture}%`),
        el("td", null, fmt(p.rate, 2, " %/hr")),
        el("td", on ? "tag-on" : "tag-off", on ? "PUMP ON" : "PUMP OFF"),
      );
      return tr;
    }));
  }

  // ---- polling loop -----------------------------------------------------
  async function poll() {
    clearTimeout(timer);
    let next = POLL_MS;
    try {
      const { ok, body } = await fetchJSON("/api/v1/status");
      if (!ok || !body) throw new Error("bad status response");
      $("offline").hidden = true;
      renderOverall(body.overall, body.checks);
      renderChecks(body.checks);
      renderLatest(body.latest);
      renderChart(body.history, body.config.critical_moisture);
      renderRows(body.history);
      $("crit-label").textContent = `critical ${body.config.critical_moisture}%`;
      $("uptime").textContent = `Server up ${duration(body.uptime_s)}`;
      $("updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      $("offline").hidden = false;
      $("overall").className = "overall state-fail";
      $("overall-title").textContent = "Backend unreachable";
      $("overall-detail").textContent = "The dashboard cannot reach the server, so nothing below is current.";
      next = RETRY_MS;
    }
    if (!document.hidden) timer = setTimeout(poll, next);
  }

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) poll();
    else clearTimeout(timer);
  });

  // ---- what-if ----------------------------------------------------------
  function setupWhatIf() {
    const m = $("wi-m"), t = $("wi-t"), out = $("wi-m-out"), result = $("wi-result");
    const button = $("whatif").querySelector("button");
    m.addEventListener("input", () => { out.textContent = m.value; });

    $("whatif").addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const payload = { moisture: Number(m.value) };
      if (t.value.trim() !== "") payload.temperature = Number(t.value);
      button.disabled = true;
      try {
        const { ok, body } = await fetchJSON("/api/v1/predict", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        result.hidden = false;
        if (!ok || !body) {
          const msg = body && Array.isArray(body.detail) && body.detail[0] ? body.detail[0].msg : "Request failed";
          result.className = "wi-result state-fail";
          result.replaceChildren(el("p", null, `Not accepted: ${msg}`));
          return;
        }
        const a = body.ai_prediction;
        const on = a.decision === "PUMP_ON";
        result.className = `wi-result ${on ? "state-fail" : "state-ok"}`;
        result.replaceChildren(
          el("p", null, `${on ? "PUMP ON" : "PUMP OFF"}: ${a.reason}`),
          el("p", "muted", `Drying rate ${fmt(a.predicted_drying_rate, 2, " %/hr")}, ` +
            `time to critical ${fmt(a.hours_to_critical, 1, " h")}` +
            (a.temperature_assumed ? " (room temperature assumed)" : "")),
        );
      } catch (err) {
        result.hidden = false;
        result.className = "wi-result state-fail";
        result.replaceChildren(el("p", null, "Could not reach the backend."));
      } finally {
        button.disabled = false;
      }
    });
  }

  setupWhatIf();
  poll();
})();
