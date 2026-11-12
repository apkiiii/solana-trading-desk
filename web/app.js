/* MY SOLANA TRADING DESK — interface */
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const eur = (v) => (v == null || isNaN(v)) ? "—" : Number(v).toLocaleString("fr-FR", {style: "currency", currency: "EUR"});
const num = (v, d = 0) => (v == null || isNaN(v)) ? "—" : Number(v).toLocaleString("fr-FR", {maximumFractionDigits: d, minimumFractionDigits: d});
const short = (a) => a ? `${a.slice(0, 4)}…${a.slice(-4)}` : "—";
const when = (iso) => { if (!iso) return "—"; const d = new Date(iso); return d.toLocaleString("fr-FR", {day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit"}); };
const ago = (iso) => { if (!iso) return ""; const s = (Date.now() - new Date(iso).getTime()) / 1000; if (s < 60) return "à l'instant"; if (s < 3600) return `il y a ${Math.floor(s / 60)} min`; if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`; return when(iso); };
const store = { get(k) { try { return localStorage.getItem(k); } catch { return null; } }, set(k, v) { try { localStorage.setItem(k, v); } catch { /* stockage indisponible */ } } };

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "err" ? 7000 : 4000);
}

async function api(path, body, method) {
  const opts = {method: method || (body !== undefined ? "POST" : "GET"), headers: {}};
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* réponse vide */ }
  if (!res.ok) throw new Error(data.error || `Erreur ${res.status}`);
  return data;
}

async function busy(button, label, fn) {
  const old = button.innerHTML;
  button.disabled = true;
  button.innerHTML = `<span class="spin"></span>${esc(label)}`;
  try { return await fn(); } catch (e) { toast(e.message, "err"); } finally { button.disabled = false; button.innerHTML = old; }
}

/* ================================================================ agents */
const AGENTS = {
  HUNTER: {role: "Trouve les meilleurs traders", c: "#c58bff", ini: "HU"},
  SCOUT: {role: "Surveille tes traders", c: "#5aa8ff", ini: "SC"},
  HEAD: {role: "Note les traders", c: "#9b6bff", ini: "HD"},
  WHALE: {role: "Gros mouvements", c: "#2fd0e8", ini: "WH"},
  RISK: {role: "Sécurité du token", c: "#ff7a8a", ini: "RK"},
  SNIPER: {role: "Prépare l'achat", c: "#ffb547", ini: "SN"},
  EXIT: {role: "Protège les positions", c: "#21d394", ini: "EX"},
  JUDGE: {role: "Décideur final", c: "#e9b85f", ini: "JG"},
};
const STATUS = {
  OK: ["ok", "OK"], WARN: ["warn", "Attention"], BLOCK: ["bad", "Refus"], UNAVAILABLE: ["bad", "Données manquantes"],
  IGNORED: ["", "Ignoré"], DUPLICATE: ["", "Doublon"], APPROVED: ["ok", "Approuvé"], REJECTED: ["bad", "Refusé"], WAIT: ["warn", "En attente"],
};
let agentsData = [];

function deckHTML(agent, full) {
  const meta = AGENTS[agent.name];
  const last = agent.last;
  const st = last ? (STATUS[last.status] || ["", last.status]) : ["", "En veille"];
  const conf = last && last.confidence != null ? Math.round(last.confidence * 100) : null;
  const text = last ? esc(last.summary) : (agent.name === "JUDGE" ? "Aucune décision pour l'instant." : "Aucune analyse pour l'instant.");
  const history = full && agent.history.length > 1
    ? `<div style="margin-top:10px;display:flex;flex-direction:column;gap:6px">${agent.history.slice(1, 5).map((h) => `<div class="row" style="font-size:12px;gap:8px"><span class="chip ${(STATUS[h.status] || [""])[0]}" style="padding:2px 8px">${esc((STATUS[h.status] || ["", h.status])[1])}</span><span class="muted" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(h.summary)}</span><span class="dim">${ago(h.created_at)}</span></div>`).join("")}</div>` : "";
  return `<div class="deck ${agent.name === "JUDGE" ? "judge" : ""}" data-agent="${agent.name}" style="--c:${meta.c}">
    <div class="deck-top"><div class="orb">${meta.ini}</div><div><h3>${agent.name}</h3><div class="role">${meta.role}</div></div>
      <div class="eq"><i></i><i></i><i></i><i></i></div></div>
    <div class="line"><span class="txt">${text}</span><span class="caret"></span></div>
    <div class="foot"><span class="chip ${st[0]}" style="padding:2px 8px">${esc(st[1])}</span>
      ${conf != null ? `<div class="conf" title="Confiance ${conf} %"><i style="width:${conf}%"></i></div><span class="mono">${conf}%</span>` : `<span class="spacer"></span>`}
      <span class="dim">${last ? ago(last.created_at) : ""}</span></div>${history}</div>`;
}

function renderDecks() {
  $("#decks").innerHTML = agentsData.map((a) => deckHTML(a, false)).join("");
  $("#decks-full").innerHTML = agentsData.map((a) => deckHTML(a, true)).join("");
}

async function loadAgents() {
  agentsData = (await api("/api/agents")).agents;
  renderDecks();
}

function typeInto(el, text) {
  let i = 0;
  el.textContent = "";
  const step = () => { el.textContent = text.slice(0, i); i += 2; if (i <= text.length + 1) requestAnimationFrame(step); };
  step();
}

function onAgentEvent(ev) {
  $$(`.deck[data-agent="${ev.agent}"]`).forEach((deck) => {
    deck.classList.add("working");
    const txt = $(".txt", deck);
    if (txt) typeInto(txt, ev.summary || "");
    clearTimeout(deck._t);
    deck._t = setTimeout(() => deck.classList.remove("working"), 6000);
  });
  const chip = $("#live-chip");
  chip.className = "chip ok";
  chip.innerHTML = `<i></i>${esc(ev.agent)} vient d'analyser`;
  clearTimeout(chip._t);
  chip._t = setTimeout(() => { chip.className = "chip"; chip.innerHTML = "<i></i>en veille"; }, 8000);
  clearTimeout(window._agentsReload);
  window._agentsReload = setTimeout(loadAgents, 2500);
}

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onmessage = (m) => { try { const ev = JSON.parse(m.data); if (ev.type === "agent") onAgentEvent(ev); } catch { /* ignoré */ } };
  source.onerror = () => { source.close(); setTimeout(connectStream, 4000); };
}

/* ================================================================ état */
let state = null;

function renderState(s) {
  state = s;
  $("#ver").textContent = `v${s.version}`;
  $("#data-root").textContent = s.data_root;
  $$("#account-seg button").forEach((b) => b.classList.toggle("on", b.dataset.account === s.account));
  const bot = $("#chip-bot");
  if (s.emergency) { bot.className = "chip bad"; bot.innerHTML = "<i></i>ARRÊT D'URGENCE"; }
  else if (s.bot_state === "RUNNING") { bot.className = "chip ok"; bot.innerHTML = "<i></i>Bot en marche"; }
  else { bot.className = "chip"; bot.innerHTML = "<i></i>Bot arrêté"; }
  const sc = $("#chip-schedule");
  const next = s.schedule.next_change ? new Date(s.schedule.next_change).toLocaleTimeString("fr-FR", {hour: "2-digit", minute: "2-digit"}) : "";
  if (!s.schedule.enabled) { sc.className = "chip"; sc.innerHTML = "<i></i>Horaires libres"; }
  else if (s.schedule.open) { sc.className = "chip ok"; sc.innerHTML = `<i></i>Achats autorisés jusqu'à ${next}`; }
  else { sc.className = "chip warn"; sc.innerHTML = `<i></i>Hors horaires — reprise à ${next}`; }
  $("#chip-venue").innerHTML = s.account === "real"
    ? `<i></i>Réel · ${s.wallet ? short(s.wallet) : "aucun wallet"}` : "<i></i>Démo · devis Jupiter réels";
  $("#chip-venue").className = `chip ${s.account === "real" ? "bad" : "gold"}`;
  $("#btn-start").disabled = s.emergency || s.bot_state === "RUNNING";
  $("#btn-stop").disabled = s.bot_state !== "RUNNING";
  const urgent = $("#btn-urgent");
  urgent.classList.toggle("on", s.emergency);
  urgent.textContent = s.emergency ? "⚠ Urgence active — lever" : "⚠ Arrêt d'urgence";
  $("#upd-dot").hidden = !s.update.available;

  const c = s.capital;
  const demo = s.account === "demo";
  $("#cap-label").textContent = demo ? "Solde du compte démo (argent fictif)" : "Capital du compte réel";
  $("#cap-big").textContent = eur(demo ? c.demo_balance : c.total);
  $("#cap-sub").innerHTML = demo ? "Marché réel, aucun risque : idéal pour vérifier le bot avant le réel."
    : `Réserve intouchable ${eur(c.reserve)} · réserve de frais ${eur(c.fee_reserve)}`;
  $("#m-invested").textContent = eur(c.invested);
  const pnl = $("#m-pnl");
  pnl.textContent = eur(c.realized_pnl);
  pnl.className = `mono ${c.realized_pnl > 0 ? "pos" : c.realized_pnl < 0 ? "neg" : ""}`;
  $("#m-pos").textContent = `${s.counts.open_positions} / ${c.max_open}`;
  const used = c.budget ? Math.min(100, (c.invested / c.budget) * 100) : 0;
  $("#m-budget").style.width = `${used}%`;
  $("#m-budget-txt").textContent = `${eur(c.invested)} / ${eur(c.budget)}`;
  $("#k-approved").textContent = s.counts.approved;
  $("#k-rejected").textContent = s.counts.rejected;
  $("#k-traders").textContent = `${s.counts.traders} / ${s.counts.max_traders}`;
  $("#k-credits").textContent = num(s.credits.used);
  $("#home-banner b").textContent = `Version ${s.version} — ${s.release_name}`;
  const mon = s.engine && s.engine.monitor;
  $("#home-banner-text").textContent = s.emergency ? "ARRÊT D'URGENCE actif : aucune transaction possible."
    : s.bot_state === "RUNNING"
      ? `Le bot copie tes traders en ${s.account === "real" ? "COMPTE RÉEL" : "compte démo"}${mon ? (mon.connected ? " — connexion temps réel active" : " — connexion en cours…") : ""}. Tu peux fermer la fenêtre : il continue en arrière-plan.`
      : s.engine_running ? "Bot stoppé : aucun nouvel achat, les positions ouvertes restent surveillées."
        : "Appuie sur DÉMARRER pour lancer le copy trading automatique.";
  renderActivity(s.alerts || []);
  if (s.first_run && !store.get("wizard-done") && !$("#wizard").classList.contains("on")) openWizard();
}

let lastAlertId = null;
function renderActivity(alerts) {
  const icon = {info: "ok", warning: "warn", error: "bad"};
  $("#activity").innerHTML = alerts.length ? alerts.map((a) => `<div class="row" style="gap:10px;padding:6px 0;border-bottom:1px solid var(--line)"><span class="chip ${icon[a.level] || ""}" style="padding:2px 8px">${a.level === "info" ? "Info" : a.level === "warning" ? "Attention" : "Erreur"}</span><span style="flex:1">${esc(a.message)}</span><span class="dim">${ago(a.at)}</span></div>`).join("")
    : `<div class="empty">Rien pour l'instant. Les achats, ventes et messages du bot s'afficheront ici.</div>`;
  const newest = alerts.length ? alerts[0].id : 0;
  if (lastAlertId !== null && newest > lastAlertId) alerts.filter((a) => a.id > lastAlertId).reverse().forEach((a) => toast(a.message, a.level === "info" ? "ok" : "err"));
  lastAlertId = newest;
}

async function refreshState() {
  try { renderState(await api("/api/state")); } catch { /* serveur indisponible : nouvelle tentative */ }
}

/* ================================================================ opportunités */
async function loadOpps() {
  const items = (await api("/api/opportunities")).items;
  $("#opps").innerHTML = items.length ? items.map((o, i) => `<div class="opp"><span class="rank">#${i + 1}</span>
      <div class="mint">${short(o.mint)}</div>
      <div class="score mono">${num(o.score)}<span class="muted" style="font-size:13px"> pts</span></div>
      <div class="row" style="gap:6px"><span class="chip ${(STATUS[o.risk] || [""])[0]}">RISK ${esc(o.risk || "—")}</span><span class="chip ${(STATUS[o.whale] || [""])[0]}">WHALE ${esc(o.whale || "—")}</span></div>
      <div class="muted" style="font-size:12px;margin-top:8px">Acheté par ${short(o.wallet)} (note ${num(o.trader_score)}) · ${o.buyers} achat(s)</div></div>`).join("")
    : `<div class="empty" style="grid-column:1/-1">Aucune opportunité pour l'instant.<br><span class="dim">Elles apparaîtront quand tes traders achèteront et que les agents auront analysé les tokens.</span></div>`;
}

/* ================================================================ JUDGE */
async function loadDecisions() {
  const items = (await api("/api/decisions")).items;
  $("#decisions").innerHTML = items.length ? items.map((d) => {
    const st = STATUS[d.decision] || ["", d.decision];
    const checks = (d.checks || []).map((c) => `<div class="check"><span class="ic ${c.passed === true ? "y" : c.passed === false ? "n" : "u"}">${c.passed === true ? "✓" : c.passed === false ? "✕" : "?"}</span><div><b>${c.n}. ${esc(c.name)}</b><div class="muted">${esc(c.detail)}</div></div></div>`).join("");
    return `<details class="decision"><summary><span class="chip ${st[0]}">${esc(st[1])}</span>
      <div><b>${d.side === "BUY" ? "Achat" : "Vente"} ${short(d.token_mint)}</b><div class="muted" style="font-size:12.5px">${esc((d.reasons || []).slice(0, 2).join(" · "))}</div></div>
      <span class="dim">${when(d.created_at)}</span></summary><div class="checks">${checks || '<span class="muted">Pas de détail</span>'}</div></details>`;
  }).join("") : `<div class="empty">Aucune décision pour l'instant.<br><span class="dim">Chaque proposition d'achat passera ici devant les 14 contrôles du JUDGE.</span></div>`;
}

/* ================================================================ traders */
async function loadTraders() {
  const items = (await api("/api/traders")).items;
  if (!items.length) { $("#traders").innerHTML = `<div class="empty">Aucun trader suivi. Ajoute l'adresse publique d'un wallet repéré sur GMGN (« smart money »).</div>`; return; }
  $("#traders").innerHTML = `<table><thead><tr><th>Note</th><th>Trader</th><th>Réussite</th><th>Gain réalisé</th><th>Drawdown</th><th>Historique</th><th>Statut</th><th></th></tr></thead><tbody>${items.map((t) => {
    const s = t.stats || {};
    const score = t.score;
    const color = score == null ? "#5d6886" : score >= 60 ? "var(--green)" : score >= 40 ? "var(--amber)" : "var(--red)";
    const notes = (s.notes || []).map(esc).join("<br>");
    return `<tr><td><div class="ring" style="--p:${score || 0};--rc:${color}"><span>${score == null ? "—" : Math.round(score)}</span></div></td>
      <td><b>${esc(t.label || "Sans nom")}</b> ${t.source === "auto" ? '<span class="chip gold" style="padding:1px 7px;font-size:10.5px">auto</span>' : '<span class="chip" style="padding:1px 7px;font-size:10.5px">manuel</span>'}<div class="addr muted">${esc(t.address)}</div>${notes ? `<div style="font-size:11.5px;color:var(--amber);margin-top:4px">${notes}</div>` : ""}</td>
      <td class="mono">${s.win_rate == null ? "—" : num(s.win_rate * 100) + " %"}<div class="dim" style="font-size:11px">${s.closed_trades ?? 0} trades</div></td>
      <td class="mono ${s.realized_pnl_sol > 0 ? "pos" : s.realized_pnl_sol < 0 ? "neg" : ""}">${s.realized_pnl_sol == null ? "—" : num(s.realized_pnl_sol, 3) + " SOL"}</td>
      <td class="mono">${s.max_drawdown_pct == null ? "—" : num(s.max_drawdown_pct) + " %"}</td>
      <td class="mono">${s.history_days == null ? "—" : num(s.history_days) + " j"}</td>
      <td><span class="chip ${t.active ? "ok" : ""}">${t.active ? "Actif" : "Désactivé"}</span></td>
      <td><div class="row" style="justify-content:flex-end"><button class="btn gold sm" data-eval="${esc(t.address)}">Évaluer</button><button class="btn sm" data-toggle="${esc(t.address)}">${t.active ? "Désactiver" : "Activer"}</button></div></td></tr>`;
  }).join("")}</tbody></table><p class="dim" style="font-size:12px;margin:12px 0 0">L'évaluation lit jusqu'à 300 transactions du wallet (1 à 3 minutes).</p>`;
  $$("[data-eval]").forEach((b) => b.onclick = () => busy(b, "Analyse…", async () => {
    const r = await api(`/api/traders/${b.dataset.eval}/evaluate`, {});
    toast(r.summary, r.status === "UNAVAILABLE" ? "err" : "ok");
    loadTraders();
  }));
  $$("[data-toggle]").forEach((b) => b.onclick = () => busy(b, "…", async () => { await api(`/api/traders/${b.dataset.toggle}/toggle`, {}); loadTraders(); }));
}

$("#btn-add-trader").onclick = (e) => busy(e.currentTarget, "Ajout…", async () => {
  await api("/api/traders", {address: $("#t-addr").value.trim(), label: $("#t-label").value.trim()});
  $("#t-addr").value = ""; $("#t-label").value = "";
  toast("Trader ajouté. Clique sur « Évaluer » pour le noter.", "ok");
  loadTraders();
});

/* ================================================================ analyse de token */
$("#btn-analyze").onclick = (e) => busy(e.currentTarget, "Analyse…", async () => {
  $("#analysis").innerHTML = `<div class="muted">Devis d'achat et de revente, liquidité, RugCheck, flux du pool…</div>`;
  const r = await api("/api/analyze", {mint: $("#mint").value.trim()});
  if (r.eur_per_sol == null) { $("#analysis").innerHTML = `<div class="empty">Analyse impossible : ${esc(r.eur_reason)}</div>`; return; }
  $("#analysis").innerHTML = `<p class="muted">Montant testé : ${eur(r.amount_eur)} (1 SOL = ${eur(r.eur_per_sol)})</p>` + r.reports.map((rep) => {
    const st = STATUS[rep.status] || ["", rep.status];
    const checks = rep.checks.length ? rep.checks.map((c) => `<div class="check"><span class="ic ${c.passed === true ? "y" : c.passed === false ? "n" : "u"}">${c.passed === true ? "✓" : c.passed === false ? "✕" : "?"}</span><div><b>${esc(c.name)}</b><div class="muted">${esc(c.detail)}</div></div></div>`).join("")
      : rep.reasons.map((x) => `<div class="check"><span class="ic u">•</span><div>${esc(x)}</div></div>`).join("");
    return `<div class="decision" style="padding-bottom:4px"><div class="row" style="padding:12px 16px"><b>${rep.agent}</b><span class="chip ${st[0]}">${esc(st[1])}</span><span class="muted">${esc(rep.summary)}</span></div><div class="checks">${checks}</div></div>`;
  }).join("");
  loadAgents();
});

/* ================================================================ journal */
async function loadJournal() {
  const j = await api("/api/journal");
  $("#positions").innerHTML = j.positions.length ? `<table><thead><tr><th>Token</th><th>Ouverte</th><th>Investi</th><th>Frais</th><th>Résultat</th><th>Compte</th><th>État</th></tr></thead><tbody>${j.positions.map((p) => `<tr><td class="addr">${short(p.token_mint)}</td><td>${when(p.opened_at)}</td><td class="mono">${eur(p.cost_eur)}</td><td class="mono">${eur(p.fees_eur)}</td><td class="mono ${p.realized_pnl_eur > 0 ? "pos" : p.realized_pnl_eur < 0 ? "neg" : ""}">${eur(p.realized_pnl_eur)}</td><td>${p.mode === "LIVE" ? "Réel" : "Démo"}</td><td><span class="chip ${p.status === "OPEN" ? "ok" : ""}">${p.status === "OPEN" ? "Ouverte" : "Fermée"}</span></td></tr>`).join("")}</tbody></table>`
    : `<div class="empty">Aucune position pour l'instant.</div>`;
  $("#events").innerHTML = j.events.length ? `<table><thead><tr><th>Date</th><th>Trader</th><th>Type</th><th>Token</th><th>Montant</th></tr></thead><tbody>${j.events.map((e) => {
    const amount = e.quote_amount_raw && e.quote_decimals != null ? `${num(Number(e.quote_amount_raw) / 10 ** e.quote_decimals, 4)} ${e.quote_decimals === 9 ? "SOL" : "USDC"}` : "—";
    return `<tr><td>${when(e.detected_at)}</td><td class="addr">${short(e.wallet)}</td><td><span class="chip ${e.side === "BUY" ? "ok" : e.side === "SELL" ? "warn" : ""}">${e.side === "BUY" ? "Achat" : e.side === "SELL" ? "Vente" : "Autre"}</span></td><td class="addr">${short(e.token_mint)}</td><td class="mono">${amount}</td></tr>`;
  }).join("")}</tbody></table>` : `<div class="empty">Aucun swap détecté pour l'instant.</div>`;
}

/* ================================================================ réglages */
const LIMITS = [
  ["capital", "max_position_eur", "Montant maximal par achat", "€", "Le bot n'investit jamais plus que ça sur un token."],
  ["capital", "max_open_positions", "Positions en même temps", "", "Nombre maximum de tokens détenus simultanément."],
  ["capital", "max_daily_loss_eur", "Perte maximale par jour", "€", "Une fois atteinte : plus aucun achat jusqu'au lendemain (les ventes continuent)."],
  ["capital", "trading_budget_eur", "Budget de trading", "€", "Somme maximale investie en même temps."],
  ["exit", "stop_loss_pct", "Stop-loss", "%", "Vente automatique si une position perd ce pourcentage."],
  ["exit", "take_profit_pct", "Prise de profit", "%", "Vente automatique si une position gagne ce pourcentage."],
  ["exit", "follow_trader_sells", "Vendre quand le trader vend", "bool", "Suivre la sortie du trader copié."],
  ["exit", "max_hold_hours", "Durée maximale d'une position", "h", "Vente automatique au-delà."],
  ["judge", "min_trader_score", "Note minimale d'un trader", "/100", "En dessous, le JUDGE refuse de le copier."],
  ["judge", "min_liquidity_usd", "Liquidité minimale d'un token", "$", "Évite les tokens qu'on ne pourrait pas revendre."],
  ["judge", "max_total_fees_pct", "Frais maximum par trade", "%", "Refuse les trades trop chers en frais."],
  ["execution", "slippage_bps", "Tolérance de prix (slippage)", "pb", "En points de base : 150 = 1,5 %."],
];
const ADVANCED = [
  ["capital", "total_eur", "Capital total", "€"], ["capital", "reserve_eur", "Réserve intouchable", "€"],
  ["capital", "fee_reserve_eur", "Réserve de frais", "€"], ["capital", "max_traders", "Traders suivis au maximum", ""],
  ["judge", "min_trader_closed_trades", "Trades clôturés minimum d'un trader", ""], ["judge", "min_trader_history_days", "Historique minimum d'un trader", "jours"],
  ["judge", "max_source_age_seconds", "Âge maximum de l'achat copié", "s"], ["judge", "max_top10_holders_pct", "Concentration maximale (top 10)", "%"],
  ["judge", "min_token_age_minutes", "Âge minimum d'un token", "min"], ["judge", "max_buy_price_impact_pct", "Impact prix maximum à l'achat", "%"],
  ["judge", "max_sell_price_impact_pct", "Impact prix maximum à la revente", "%"], ["exit", "min_liquidity_usd", "Liquidité minimale avant sortie", "$"],
  ["execution", "max_priority_fee_lamports", "Frais de priorité maximum", "lamports"],
];
let settingsData = null;

function settingInput(section, key, unit) {
  const value = settingsData.sections[section][key];
  if (unit === "bool") return `<select data-s="${section}.${key}"><option value="true" ${value ? "selected" : ""}>Oui</option><option value="false" ${!value ? "selected" : ""}>Non</option></select>`;
  return `<div class="row" style="gap:6px;flex-wrap:nowrap"><input class="input mono" type="number" step="any" data-s="${section}.${key}" value="${esc(value)}"><span class="muted" style="min-width:34px">${esc(unit)}</span></div>`;
}

async function loadSettings() {
  settingsData = await api("/api/settings");
  const sd = settingsData;
  $("#profiles").innerHTML = Object.entries(sd.profiles).map(([k, p]) => `<div class="pcard ${sd.profile === k ? "on" : ""}" data-profile="${k}"><h3>${esc(p.title)}${k === "equilibre" ? ' <span class="chip gold" style="padding:1px 8px;font-size:10.5px">conseillé</span>' : ""}</h3><p>${esc(p.description)}</p></div>`).join("")
    + (sd.profile ? "" : `<p class="muted" style="grid-column:1/-1;margin:0">Réglages personnalisés (aucun profil ne correspond exactement).</p>`);
  $("#limits").innerHTML = LIMITS.map(([s, k, label, unit, help]) => `<div class="setting"><div><b>${esc(label)}</b><span>${esc(help)}</span></div>${settingInput(s, k, unit)}</div>`).join("")
    + `<details class="adv"><summary>Réglages avancés</summary>${ADVANCED.map(([s, k, label, unit]) => `<div class="setting"><div><b>${esc(label)}</b></div>${settingInput(s, k, unit)}</div>`).join("")}</details>`;
  $$("[data-s]").forEach((el) => {
    const [section, key] = el.dataset.s.split(".");
    const value = sd.sections[section][key];
    if (el.tagName === "SELECT") el.value = String(value);
    else if (el.type === "time" || el.type === "number") el.value = value;
    el.onchange = () => saveSetting(section, key, el);
  });
  const DAY = {mon: "Lu", tue: "Ma", wed: "Me", thu: "Je", fri: "Ve", sat: "Sa", sun: "Di"};
  const days = sd.sections.schedule.days;
  $("#days").innerHTML = Object.entries(DAY).map(([k, l]) => `<button class="${days.includes(k) ? "on" : ""}" data-day="${k}">${l}</button>`).join("");
  $$("[data-day]").forEach((b) => b.onclick = async () => {
    const set = new Set(settingsData.sections.schedule.days);
    set.has(b.dataset.day) ? set.delete(b.dataset.day) : set.add(b.dataset.day);
    try { settingsData = await api("/api/settings", {section: "schedule", key: "days", value: Object.keys(DAY).filter((d) => set.has(d))}); loadSettings(); refreshState(); }
    catch (e) { toast(e.message, "err"); }
  });
  $$("[data-profile]").forEach((c) => c.onclick = async () => {
    try { await api("/api/profile", {name: c.dataset.profile}); toast(`Profil « ${c.querySelector("h3").firstChild.textContent.trim()} » appliqué.`, "ok"); loadSettings(); refreshState(); }
    catch (e) { toast(e.message, "err"); }
  });
}

async function saveSetting(section, key, el) {
  const old = settingsData.sections[section][key];
  let value = el.value;
  if (typeof old === "boolean") value = value === "true";
  else if (typeof old === "number") value = Number(value);
  if (typeof old === "number" && isNaN(value)) { toast("Valeur invalide.", "err"); el.value = old; return; }
  try {
    settingsData = await api("/api/settings", {section, key, value});
    toast("Réglage enregistré.", "ok");
    loadSettings(); refreshState();
  } catch (e) { toast(e.message, "err"); el.value = old; }
}

/* ================================================================ clés + diagnostic */
const KEYS = [
  ["HELIUS_API_KEY", "Helius", "Conseillée (gratuite) — détection plus rapide des trades. Sans elle, le bot utilise le RPC public de Solana, gratuit mais plus lent", "https://dashboard.helius.dev"],
  ["GMGN_API_KEY", "GMGN", "Facultative — accélère la recherche des traders (sinon : blockchain, sans clé). Voir « Clé GMGN » plus bas", "https://gmgn.ai/ai"],
  ["JUPITER_API_KEY", "Jupiter", "Optionnelle (gratuite) — devis plus rapides : conseillée pour le compte réel", "https://portal.jup.ag"],
  ["RUGCHECK_API_KEY", "RugCheck", "Optionnelle — rapports de risque plus frais", "https://rugcheck.xyz"],
];

async function loadKeys(target = "#keys") {
  const k = (await api("/api/keys")).keys;
  $(target).innerHTML = KEYS.map(([name, title, help, url]) => `<div class="setting" style="grid-template-columns:1fr 1.2fr">
    <div><b>${title} <span class="chip ${k[name].set ? "ok" : ""}" style="padding:1px 8px;font-size:10.5px">${k[name].set ? "configurée" : "absente"}</span></b><span>${esc(help)} — <a href="${url}" target="_blank" rel="noopener">obtenir une clé</a></span></div>
    <input class="input mono" type="password" autocomplete="off" data-key="${name}" placeholder="${k[name].set ? esc(k[name].masked) : "Coller la clé"}"></div>`).join("")
    + `<div class="row" style="margin-top:12px"><span class="spacer"></span><button class="btn gold" data-savekeys>Enregistrer les clés</button></div>`;
  $(`${target} [data-savekeys]`).onclick = (e) => busy(e.currentTarget, "Enregistrement…", async () => {
    const values = {};
    $$(`${target} [data-key]`).forEach((i) => { if (i.value.trim()) values[i.dataset.key] = i.value.trim(); });
    await api("/api/keys", values);
    toast("Clés enregistrées sur cet ordinateur.", "ok");
    loadKeys(target); refreshState();
  });
}

async function loadWallet() {
  const w = await api("/api/wallet");
  const chip = $("#w-chip");
  chip.className = `chip ${w.exists ? "ok" : "warn"}`;
  chip.innerHTML = `<i></i>${w.exists ? "connecté" : "à ajouter"}`;
  const steps = `<ol style="margin:12px 0 0;padding-left:18px;line-height:1.8">
      <li>Dans l'extension <b>Phantom</b> : clique sur ton avatar (en haut à gauche) → <b>Ajouter un compte</b> → <b>Créer un nouveau compte</b>. Nomme-le « Bot ».</li>
      <li>Envoie sur ce compte <b>seulement</b> le budget du bot (ex. 17,50 € + 5 € de frais, en SOL). Jamais tes économies.</li>
      <li>Toujours dans Phantom : avatar → <b>Paramètres</b> (roue dentée) → <b>Gérer les comptes</b> → compte « Bot » → <b>Afficher la clé privée</b> → réseau <b>Solana</b>.</li>
      <li>Copie cette clé privée et colle-la ci-dessous, <b>sur ce PC uniquement</b> (jamais dans un chat, un mail ou un site).</li></ol>
      <p class="dim" style="font-size:12px;margin:10px 0 0">Ce n'est pas la phrase de récupération (12 mots) : elle sera toujours refusée. La clé est rangée dans le Gestionnaire d'identifiants de Windows et ne s'affiche plus jamais.</p>`;
  $("#wallet").innerHTML = w.exists
    ? `<p class="muted" style="margin-top:0">Le bot signe lui-même ses ordres sur ce PC, uniquement quand le JUDGE les approuve et que le <b>compte réel</b> est activé. Tous ses trades s'affichent dans Phantom, sur le compte « Bot ».</p>
       <div class="setting"><div><b>Adresse du compte bot</b><span>À vérifier : c'est la même que dans Phantom</span></div><div class="addr mono" style="word-break:break-all">${esc(w.address)}</div></div>
       <div class="setting"><div><b>Solde</b><span>Lu sur la blockchain</span></div><div class="mono">${w.balance ? num(w.balance.sol, 4) + " SOL" : "illisible (clé Helius ?)"}</div></div>
       <div class="row" style="margin-top:10px"><a class="btn sm" href="https://solscan.io/account/${esc(w.address)}" target="_blank" rel="noopener">Voir sur Solscan</a><span class="spacer"></span><button class="btn ghost sm" id="w-delete">Retirer ce wallet</button></div>`
    : `<p class="muted" style="margin-top:0">Pour le <b>compte réel</b>, le bot a besoin d'un compte Phantom <b>dédié</b>, séparé de ton compte principal. Le compte démo n'en a pas besoin.</p>
       <div class="row"><input class="input mono" type="password" autocomplete="off" id="w-key" placeholder="Clé privée du compte « Bot »" style="flex:1"><button class="btn gold" id="w-import">Enregistrer</button></div>${steps}`;
  if ($("#w-import")) $("#w-import").onclick = (e) => busy(e.currentTarget, "Vérification…", async () => {
    const input = $("#w-key");
    const r = await api("/api/wallet", {private_key: input.value});
    input.value = "";
    toast(`Compte bot enregistré : ${short(r.address)}. Vérifie que c'est la même adresse dans Phantom.`, "ok");
    loadWallet(); refreshState();
  });
  if ($("#w-delete")) $("#w-delete").onclick = (e) => {
    if (!confirm("Retirer ce wallet du logiciel ? Les fonds restent dans Phantom ; le compte réel sera désactivé.")) return;
    busy(e.currentTarget, "…", async () => { await api("/api/wallet/delete", {}); toast("Wallet retiré du logiciel.", "ok"); loadWallet(); refreshState(); });
  };
}

async function loadHunter() {
  const h = await api("/api/hunter");
  const c = h.settings;
  const chip = $("#h-chip");
  const srcName = h.source === "gmgn" ? "GMGN" : h.source === "onchain" ? "blockchain, sans clé" : "aucune source";
  if (!h.source) { chip.className = "chip warn"; chip.innerHTML = "<i></i>aucune source"; }
  else if (!c.enabled) { chip.className = "chip"; chip.innerHTML = "<i></i>désactivée"; }
  else if (h.running) { chip.className = "chip ok"; chip.innerHTML = `<i></i>active (${srcName}) — toutes les ${num(c.interval_minutes)} min`; }
  else { chip.className = "chip"; chip.innerHTML = `<i></i>${srcName} — démarre avec le bot`; }
  const last = h.last;
  const cands = h.candidates;
  const statusText = {ok: ["ok", "retenu"], excluded: ["bad", "exclu"], dropped: ["warn", "écarté"], new: ["", "à vérifier"]};
  $("#hunter").innerHTML = `
    ${h.source === "onchain" ? `<p style="margin-top:0"><b>Recherche sur la blockchain.</b> HUNTER repère les <b>gros détenteurs des tokens qui ont le plus monté en 24 h</b> (souvent entrés tôt) et les acheteurs récents des tokens actifs, puis vérifie <b>l'historique complet</b> de chacun (au moins 30 trades sur 14 jours, réussite, gains, pertes) avant de le copier. Avec ta clé Helius, la recherche est rapide (plafond de ${num(c.helius_daily_credits)} crédits par jour, ensuite PublicNode gratuit).</p>`
      : `<p style="margin-top:0"><b>Recherche via GMGN.</b> HUNTER parcourt les listes « smart money » et « KOL » et note chaque trader (réussite, rendement, gains réels de ses dernières ventes, pertes lourdes).</p>`}
    <p class="muted" style="margin-top:0">Il écarte les robots, les créateurs de tokens et ceux qui revendent trop vite pour être copiés. Toutes les ${num(c.interval_minutes)} min, il <b>retire les traders non concluants</b> (note en baisse, inactifs depuis ${num(c.max_idle_minutes)} min, ou copies perdantes de ${eur(c.max_copied_loss_eur)}) et les remplace par les meilleurs candidats. Il ne fait qu'<b>choisir qui suivre</b> : chaque achat passe toujours par le JUDGE.</p>
    <div class="grid g4" style="margin:12px 0">
      <div class="setting" style="grid-template-columns:1fr"><div><b>Recherche</b></div><select data-h="enabled"><option value="true" ${c.enabled ? "selected" : ""}>Activée</option><option value="false" ${!c.enabled ? "selected" : ""}>Désactivée</option></select></div>
      <div class="setting" style="grid-template-columns:1fr"><div><b>Traders copiés</b><span>maximum ${h.max_traders}</span></div><input class="input mono" type="number" min="1" max="${h.max_traders}" data-h="target_traders" value="${c.target_traders}"></div>
      <div class="setting" style="grid-template-columns:1fr"><div><b>Vérification toutes les</b><span>minutes</span></div><input class="input mono" type="number" min="1" step="1" data-h="interval_minutes" value="${c.interval_minutes}"></div>
      <div class="setting" style="grid-template-columns:1fr"><div><b>Période analysée</b></div><select data-h="period"><option value="7d" ${c.period === "7d" ? "selected" : ""}>7 jours</option><option value="30d" ${c.period === "30d" ? "selected" : ""}>30 jours</option></select></div>
    </div>
    <div class="row"><span class="muted">${h.followed} trader(s) suivi(s) dont ${h.auto} trouvé(s) par HUNTER${last ? ` · dernier passage ${ago(last.created_at)} : ${esc(last.summary)}` : ""}</span><span class="spacer"></span><button class="btn gold" id="h-run" ${h.source ? "" : "disabled"}>Chercher maintenant</button></div>
    ${cands.length ? `<table style="margin-top:12px"><thead><tr><th>Note</th><th>Trader</th><th>Réussite</th><th>Rendement</th><th>Gain moyen</th><th>Trades</th><th>Statut</th></tr></thead><tbody>${cands.slice(0, 15).map((x) => {
      const st = x.stats || {};
      const s2 = x.followed ? ["ok", "copié"] : x.status === "ok" ? ["", "en réserve"] : (statusText[x.status] || ["", x.status]);
      return `<tr><td class="mono"><b>${x.score == null ? "—" : num(x.score)}</b></td><td><div class="addr">${short(x.address)}${st.name ? ` <span class="muted">@${esc(st.name)}</span>` : ""}</div>${st.holder_of && st.holder_of.length ? `<div class="dim" style="font-size:11px">gros détenteur de ${st.holder_of.slice(0, 2).map((h) => `${esc(h.symbol || "?")} (+${num(h.gain_24h)} %)`).join(", ")}</div>` : st.seen_tokens && x.status === "new" ? `<div class="dim" style="font-size:11px">a acheté ${st.seen_tokens.length} token(s) actif(s)</div>` : ""}</td>
        <td class="mono">${st.win_rate == null ? "—" : num(st.win_rate * 100) + " %"}</td><td class="mono ${st.roi_pct > 0 ? "pos" : st.roi_pct < 0 ? "neg" : ""}">${st.roi_pct == null ? "—" : num(st.roi_pct) + " %"}</td>
        <td class="mono">${st.avg_win_pct == null ? "—" : "+" + num(st.avg_win_pct) + " %"}</td><td class="mono">${st.token_num ?? st.closed_trades ?? "—"}</td>
        <td><span class="chip ${s2[0]}" style="padding:2px 8px">${s2[1]}</span>${x.reason ? `<div class="dim" style="font-size:11px;margin-top:3px">${esc(x.reason)}</div>` : ""}</td></tr>`;
    }).join("")}</tbody></table>` : `<div class="empty" style="margin-top:12px">Aucun candidat évalué pour l'instant.</div>`}`;
  $$("[data-h]").forEach((el) => el.onchange = async () => {
    const key = el.dataset.h;
    let value = el.value;
    if (key === "enabled") value = value === "true";
    else if (key !== "period") value = Number(value);
    try { await api("/api/settings", {section: "discovery", key, value}); toast("Réglage enregistré.", "ok"); loadHunter(); }
    catch (e) { toast(e.message, "err"); loadHunter(); }
  });
  if ($("#h-run")) $("#h-run").onclick = (e) => busy(e.currentTarget, "Recherche…", async () => {
    const r = await api("/api/hunter/run", {});
    toast(r.summary, r.status === "UNAVAILABLE" ? "err" : "ok");
    loadHunter(); loadTraders(); refreshState();
  });
}

async function loadGmgnKey() {
  const k = await api("/api/gmgn/key");
  const chip = $("#gk-chip");
  chip.className = `chip ${k.exists ? "ok" : "warn"}`;
  chip.innerHTML = `<i></i>${k.exists ? "créée" : "à créer"}`;
  const steps = `<ol style="margin:12px 0 0;padding-left:18px;line-height:1.8">
      <li>Clique sur <b>Copier la clé publique</b>.</li>
      <li>Sur <a href="https://gmgn.ai/ai" target="_blank" rel="noopener">gmgn.ai/ai</a> (connecté à ton compte GMGN), colle-la dans <b>Upload Public Key</b>.</li>
      <li>Laisse seulement <b>Enable Reading</b> (la lecture suffit, le trading passe par Jupiter et ton compte Phantom « Bot »), puis <b>Next</b>.</li>
      <li>GMGN t'affiche ta <b>clé API</b> : colle-la dans « Clés API » → <b>GMGN</b>, puis <b>Enregistrer les clés</b>.</li></ol>`;
  $("#gmgn-key").innerHTML = k.exists
    ? `<p class="muted" style="margin-top:0">Elle permet à HUNTER de lire les classements de GMGN. Elle ne donne <b>aucun accès</b> à ton wallet ; sa partie secrète reste dans le coffre-fort de Windows.</p>
       <textarea class="input mono" rows="4" readonly id="gk-pem" style="font-size:12px">${esc(k.public_pem)}</textarea>
       <div class="row" style="margin-top:10px"><button class="btn gold" id="gk-copy">Copier la clé publique</button><span class="spacer"></span><button class="btn ghost sm" id="gk-replace">Remplacer la clé</button></div>${steps}`
    : `<p class="muted" style="margin-top:0"><b>Facultatif</b> : sans clé GMGN, HUNTER cherche directement sur la blockchain. GMGN demande une clé publique pour te donner une clé API gratuite. Le logiciel la crée sur ce PC ; elle ne donne <b>aucun accès</b> à ton wallet.</p>
       <button class="btn gold" id="gk-create">Créer ma clé GMGN</button>${steps}`;
  const create = async (replace) => { await api("/api/gmgn/key", {replace}); toast("Clé créée. Copie la clé publique sur gmgn.ai/ai.", "ok"); loadGmgnKey(); };
  if ($("#gk-create")) $("#gk-create").onclick = (e) => busy(e.currentTarget, "Création…", () => create(false));
  if ($("#gk-replace")) $("#gk-replace").onclick = (e) => {
    if (!confirm("Remplacer la clé ? Ta clé API GMGN actuelle ne fonctionnera plus : il faudra en recréer une sur gmgn.ai/ai.")) return;
    busy(e.currentTarget, "…", () => create(true));
  };
  if ($("#gk-copy")) $("#gk-copy").onclick = async () => {
    const pem = $("#gk-pem").value;
    try { await navigator.clipboard.writeText(pem); } catch { $("#gk-pem").select(); document.execCommand("copy"); }
    toast("Clé publique copiée. Colle-la sur gmgn.ai/ai.", "ok");
  };
}

$("#btn-diag").onclick = (e) => busy(e.currentTarget, "Test en cours…", async () => {
  $("#diag-out").textContent = "Test en cours (10 à 60 secondes)…";
  const r = await api("/api/diagnose", {wallet: $("#diag-wallet").value.trim() || null});
  $("#diag-out").textContent = r.output || "Aucun résultat.";
});

/* ================================================================ mises à jour */
async function loadUpdate(check) {
  const u = check ? await api("/api/update/check", {}) : await api("/api/update");
  $("#u-current").textContent = u.current;
  $("#btn-u-install").disabled = !u.available;
  $("#u-status").textContent = u.error ? `Vérification impossible : ${u.error}` : u.available
    ? `Nouvelle version disponible : ${u.available.version}` : `Le logiciel est à jour${u.checked_at ? ` (vérifié ${ago(u.checked_at)})` : ""}.`;
  $("#u-notes").innerHTML = u.available ? `<h3>${esc(u.available.title)}</h3>${mdLite(u.available.notes)}` : "";
}
function mdLite(text) {
  return esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").split(/\n/).map((l) => l.startsWith("- ") ? `<li>${l.slice(2)}</li>` : l ? `<p>${l}</p>` : "").join("").replace(/(<li>.*?<\/li>)+/g, (m) => `<ul>${m}</ul>`);
}
$("#btn-u-check").onclick = (e) => busy(e.currentTarget, "Vérification…", () => loadUpdate(true));
$("#btn-u-install").onclick = (e) => {
  if (!confirm("Installer la nouvelle version ? Le logiciel va se fermer quelques secondes puis redémarrer. Tes données sont conservées.")) return;
  busy(e.currentTarget, "Téléchargement…", async () => { const r = await api("/api/update/install", {}); toast(r.message, "ok"); });
};

/* ================================================================ commandes */
$$("#account-seg button").forEach((b) => b.onclick = async () => {
  const real = b.dataset.account === "real";
  if (real && state && state.account === "real") return;
  if (real) {
    if (!state || !state.wallet) { toast("Ajoute d'abord le compte Phantom dédié au bot (menu Connexions).", "err"); go("connections"); return; }
    const c = state.capital;
    const ok = confirm(`Activer le COMPTE RÉEL ?\n\nLe bot pourra acheter et vendre avec les vrais fonds du compte Phantom ${short(state.wallet)}.\n\nLimites : ${eur(c.max_position)} max par achat, ${c.max_open} positions max, perte max ${eur(c.max_daily_loss)} par jour. Chaque ordre doit être approuvé par le JUDGE.\n\nLe compte réel se désactive à la fermeture du logiciel. Tu peux perdre l'argent investi.`);
    if (!ok) return;
  }
  try {
    const r = await api("/api/account", {account: b.dataset.account, confirm: real ? true : undefined});
    toast(real ? `Compte RÉEL activé (${r.balance ? num(r.balance.sol, 4) + " SOL" : "solde illisible"}).` : "Compte démo activé : argent fictif, marché réel.", real ? "err" : "ok");
    refreshState();
  } catch (e) { toast(e.message, "err"); }
});
$("#btn-start").onclick = (e) => busy(e.currentTarget, "Démarrage…", async () => {
  await api("/api/bot", {action: "start"});
  toast(state && state.account === "real" ? "Bot démarré en COMPTE RÉEL." : "Bot démarré en compte démo.", "ok");
  refreshState();
});
$("#btn-stop").onclick = (e) => busy(e.currentTarget, "Arrêt…", async () => {
  await api("/api/bot", {action: "stop"});
  toast("Bot stoppé : plus de nouvel achat. Les positions ouvertes restent protégées.", "ok");
  refreshState();
});
$("#btn-urgent").onclick = async () => {
  const activate = !(state && state.emergency);
  if (!activate && !confirm("Lever l'arrêt d'urgence ? Les transactions pourront de nouveau être autorisées par le JUDGE.")) return;
  try { await api("/api/emergency", {active: activate}); toast(activate ? "ARRÊT D'URGENCE activé : plus aucune transaction." : "Arrêt d'urgence levé.", activate ? "err" : "ok"); refreshState(); }
  catch (e) { toast(e.message, "err"); }
};
$("#btn-quit").onclick = async () => {
  if (!confirm("Fermer Solana Trading Desk ?")) return;
  try { await api("/api/quit", {}); } catch { /* déjà fermé */ }
  document.body.innerHTML = `<div style="display:grid;place-items:center;height:100vh;color:#8a96b3;font-family:sans-serif">Solana Trading Desk est fermé. Tu peux fermer cette fenêtre.</div>`;
};

/* ================================================================ assistant */
let wzStep = 0;
const WZ = [
  () => `<h2>Bienvenue dans ton Trading Desk</h2><p>En 3 étapes, le logiciel sera prêt. Tu commences en <b>compte démo</b> : argent fictif, marché réel, aucun risque.</p>
    <div class="grid g3" style="margin-top:16px"><div class="panel panel-b"><b>Les agents</b><p class="muted" style="margin:6px 0 0">surveillent tes traders et vérifient chaque token.</p></div><div class="panel panel-b"><b>Le JUDGE</b><p class="muted" style="margin:6px 0 0">autorise ou refuse chaque achat selon des règles fixes.</p></div><div class="panel panel-b"><b>Toi</b><p class="muted" style="margin:6px 0 0">choisis un profil, et c'est tout.</p></div></div>`,
  () => `<h2>1. Tes clés de connexion</h2><p>Aucune clé n'est obligatoire : sans clé, le bot lit la blockchain par le RPC public de Solana (gratuit, plus lent). Une clé Helius gratuite rend la détection des trades plus rapide. Les clés restent sur ce PC.</p><div id="wz-keys"></div>`,
  () => `<h2>2. Ton profil de risque</h2><p>Tu pourras le changer à tout moment dans Réglages.</p><div class="profiles" id="wz-profiles" style="grid-template-columns:1fr"></div>`,
  () => `<h2>3. Les traders à copier</h2><p>L'agent HUNTER trouve lui-même les 5 meilleurs traders (sans clé) et remplace ceux qui ne sont pas concluants. Tu peux aussi en ajouter un à la main :</p>
    <div class="row" style="margin-top:12px"><input class="input mono" id="wz-addr" placeholder="Adresse publique (facultatif)" style="flex:1"><button class="btn" id="wz-add">Ajouter</button></div>
    <p class="dim" style="font-size:12.5px">Ensuite : <b>▶ Démarrer</b> (compte démo) — HUNTER choisit les traders, le JUDGE vérifie chaque achat. Le compte réel se branche plus tard, avec un compte Phantom dédié au bot (menu Connexions).</p>`,
];
function openWizard() { wzStep = 0; $("#wizard").classList.add("on"); renderWizard(); }
function closeWizard() { $("#wizard").classList.remove("on"); store.set("wizard-done", "1"); }
async function renderWizard() {
  $$("#wz-steps i").forEach((i, n) => i.classList.toggle("on", n <= wzStep));
  $("#wz-body").innerHTML = WZ[wzStep]();
  $("#wz-prev").style.visibility = wzStep ? "visible" : "hidden";
  $("#wz-next").textContent = wzStep === WZ.length - 1 ? "Terminer" : "Suivant";
  if (wzStep === 1) loadKeys("#wz-keys");
  if (wzStep === 2) {
    const sd = await api("/api/settings");
    $("#wz-profiles").innerHTML = Object.entries(sd.profiles).map(([k, p]) => `<div class="pcard ${sd.profile === k ? "on" : ""}" data-wzp="${k}"><h3>${esc(p.title)}</h3><p>${esc(p.description)}</p></div>`).join("");
    $$("[data-wzp]").forEach((c) => c.onclick = async () => { try { await api("/api/profile", {name: c.dataset.wzp}); renderWizard(); } catch (e) { toast(e.message, "err"); } });
  }
  if (wzStep === 3) $("#wz-add").onclick = (e) => busy(e.currentTarget, "Ajout…", async () => { await api("/api/traders", {address: $("#wz-addr").value.trim()}); toast("Trader ajouté.", "ok"); $("#wz-addr").value = ""; });
}
$("#wz-next").onclick = () => { if (wzStep < WZ.length - 1) { wzStep++; renderWizard(); } else { closeWizard(); refreshState(); go("traders"); } };
$("#wz-prev").onclick = () => { if (wzStep) { wzStep--; renderWizard(); } };
$("#wz-skip").onclick = closeWizard;

/* ================================================================ navigation */
const LOADERS = {
  home: () => Promise.all([loadAgents(), loadOpps()]), agents: loadAgents, judge: loadDecisions, traders: () => Promise.all([loadHunter(), loadTraders()]),
  journal: loadJournal, settings: loadSettings, connections: () => Promise.all([loadWallet(), loadKeys(), loadGmgnKey()]), updates: () => loadUpdate(false), vps: async () => {},
};
function go(view) {
  if (!LOADERS[view]) view = "home";
  if (location.hash !== `#${view}`) history.replaceState(null, "", `#${view}`);
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === view));
  LOADERS[view]().catch((e) => toast(e.message, "err"));
}
window.addEventListener("hashchange", () => go(location.hash.slice(1)));

refreshState().then(() => go(location.hash.slice(1) || "home"));
connectStream();
setInterval(refreshState, 4000);
setInterval(() => { const v = location.hash.slice(1) || "home"; if (v === "home") loadOpps().catch(() => {}); }, 15000);
