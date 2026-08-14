const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
}[char]));

let nextRefreshAt = 0;
let autoRefreshTimer = null;
let countdownTimer = null;
let loading = false;
window.__challengePlayers = [];

function posClass(position) {
  return position <= 3 ? `p${position}` : "";
}

function avatar(player) {
  if (player.profileIcon) {
    return `<img class="avatar" src="${esc(player.profileIcon)}" alt="">`;
  }
  return `<span class="avatar fallback">${esc((player.riotId || "?").slice(0, 2).toUpperCase())}</span>`;
}

function champList(list) {
  if (!list?.length) {
    return `<span class="empty">Sin partidas SoloQ en agosto</span>`;
  }

  return `<div class="champs">${list.map(champion => `
    <div class="champ-wrap" title="${esc(champion.name)} · ${champion.games} partidas · ${champion.wins} victorias">
      ${champion.icon ? `<img class="champ" src="${esc(champion.icon)}" alt="${esc(champion.name)}">` : ""}
      <small>${esc(champion.name)}</small>
    </div>`).join("")}</div>`;
}

function bestChamp(champion) {
  if (!champion) {
    return `<span class="empty">—</span>`;
  }

  return `<div class="best">
    ${champion.icon ? `<img class="champ" src="${esc(champion.icon)}" alt="${esc(champion.name)}">` : ""}
    <span><b>${esc(champion.name)}</b><small>${champion.wins} victorias</small></span>
  </div>`;
}

function row(player) {
  if (player.error) {
    return `<tr>
      <td><div class="pos ${posClass(player.position)}">${player.position}</div></td>
      <td><div class="player">${avatar(player)}<div><b>${esc(player.riotId)}</b><small>LAN</small></div></div></td>
      <td colspan="7" class="row-error">${esc(player.error)}</td>
    </tr>`;
  }

  const challenge = player.challenge;
  return `<tr data-player-puuid="${esc(player.puuid || "")}" class="player-row">
    <td><div class="pos ${posClass(player.position)}">${player.position}</div></td>
    <td>
      <div class="player">
        ${avatar(player)}
        <div>
          <b>${esc(player.riotId)}</b>
          <small>Nivel ${player.summonerLevel ?? "—"} · LAN</small>
          <span class="live-badge live-off">No en vivo</span>
        </div>
      </div>
    </td>
    <td><div class="rank"><b>${esc(player.rank.label)}</b><small>Ranked Solo/Duo</small></div></td>
    <td><span class="lp">${player.rank.lp}</span></td>
    <td><span class="games">${challenge.games}</span></td>
    <td><span class="record"><b class="w">${challenge.wins} V</b> / <b class="l">${challenge.losses} D</b></span></td>
    <td><span class="wr">${challenge.winrate.toFixed(1)}%</span><div class="bar"><i style="width:${Math.min(100, challenge.winrate)}%"></i></div></td>
    <td>${champList(challenge.top3Champions)}</td>
    <td>${bestChamp(challenge.mostWinningChampion)}</td>
  </tr>`;
}

function card(player) {
  if (player.error) {
    return `<article class="card"><div class="card-head"><div class="pos ${posClass(player.position)}">${player.position}</div>${avatar(player)}
      <div class="id"><b>${esc(player.riotId)}</b><small>${esc(player.error)}</small></div></div></article>`;
  }

  const challenge = player.challenge;
  return `<article class="card">
    <div class="card-head">
      <div class="pos ${posClass(player.position)}">${player.position}</div>${avatar(player)}
      <div class="id"><b>${esc(player.riotId)}</b><small>${esc(player.rank.label)} · ${player.rank.lp} LP</small></div>
    </div>
    <div class="grid">
      <div class="cell"><label>Partidas agosto</label><strong>${challenge.games}</strong></div>
      <div class="cell"><label>Win rate</label><strong>${challenge.winrate.toFixed(1)}%</strong></div>
      <div class="cell"><label>Victorias</label><strong>${challenge.wins}</strong></div>
      <div class="cell"><label>Derrotas</label><strong>${challenge.losses}</strong></div>
      <div class="cell full"><label>Top 3 más jugados</label>${champList(challenge.top3Champions)}</div>
      <div class="cell full"><label>Campeón con más victorias</label>${bestChamp(challenge.mostWinningChampion)}</div>
    </div>
  </article>`;
}

function formatCountdown(seconds) {
  const safe = Math.max(0, Math.ceil(seconds));
  const minutes = Math.floor(safe / 60);
  const rest = safe % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function updateRefreshUi() {
  const button = $("#refresh");
  const countdown = $("#refresh-countdown");
  if (!button || !countdown) return;

  const remaining = Math.max(0, nextRefreshAt - Date.now() / 1000);

  if (loading) {
    button.disabled = true;
    button.textContent = "↻ Actualizando…";
    countdown.textContent = "Consultando Riot Games";
    return;
  }

  if (remaining > 0) {
    button.disabled = true;
    button.textContent = "✓ Actualizado";
    countdown.textContent = `Próxima consulta disponible en ${formatCountdown(remaining)}`;
  } else {
    button.disabled = false;
    button.textContent = "↻ Actualizar";
    countdown.textContent = "Ya puedes solicitar una actualización";
  }
}

function scheduleAutoRefresh() {
  if (autoRefreshTimer) clearTimeout(autoRefreshTimer);

  const remainingMs = Math.max(1000, nextRefreshAt * 1000 - Date.now());
  // Small random delay prevents all five browsers from hitting the backend at the exact same millisecond.
  const jitterMs = Math.floor(Math.random() * 8000);
  autoRefreshTimer = setTimeout(() => load(), remainingMs + jitterMs);
}

function setStatusFromData(data) {
  const status = $("#status");
  const updated = new Date(data.updatedAt * 1000);
  const cache = data.cache || {};
  const base = `Datos de Riot · Solo/Duo (cola 420) · Agosto 2026 · Actualizado ${updated.toLocaleString("es-EC")}`;

  if (cache.stale) {
    status.className = "status warning";
    status.textContent = `${base} · ${cache.warning || "Mostrando la última información disponible."}`;
  } else {
    status.className = "status";
    status.textContent = `${base} · Actualización máxima cada 5 minutos.`;
  }
}

async function parseApiResponse(response) {
  const raw = await response.text();
  let data = null;

  try {
    data = raw ? JSON.parse(raw) : null;
  } catch {
    throw new Error(`El servidor respondió HTTP ${response.status} con un formato inesperado.`);
  }

  if (!response.ok) {
    throw new Error(data?.detail || `Error HTTP ${response.status}.`);
  }

  return data;
}

function renderLiveStatus(player, payload) {
  const row = document.querySelector(`tr[data-player-puuid="${CSS.escape(player.puuid || "")}"]`);
  if (!row) return;
  const badge = row.querySelector(".live-badge");
  if (!badge) return;

  if (payload && payload.in_game) {
    badge.textContent = "🔴 EN JUEGO";
    badge.classList.remove("live-off");
    badge.classList.add("live-on");
  } else {
    badge.textContent = "No en vivo";
    badge.classList.remove("live-on");
    badge.classList.add("live-off");
  }
}

function renderSummaryMetrics(summary) {
  if (!summary || !summary.games) {
    return "<div class='history-empty'>Aun no hay partidas recientes para este jugador.</div>";
  }

  return `
    <article class="metric-card">
      <label>Partidas recientes</label>
      <strong>${summary.games}</strong>
    </article>
    <article class="metric-card">
      <label>Record</label>
      <strong>${summary.wins}V / ${summary.losses}D</strong>
    </article>
    <article class="metric-card">
      <label>Win rate</label>
      <strong>${summary.winrate.toFixed(1)}%</strong>
    </article>
    <article class="metric-card ${summary.currentStreak?.type === "win" ? "streak-win" : summary.currentStreak?.type === "loss" ? "streak-loss" : ""}">
      <label>Racha actual</label>
      <strong>${esc(summary.currentStreak?.label || "Sin racha")}</strong>
    </article>
    <article class="metric-card">
      <label>KDA promedio</label>
      <strong>${summary.avg?.kills ?? 0}/${summary.avg?.deaths ?? 0}/${summary.avg?.assists ?? 0}</strong>
      <small>${summary.avg?.kda ?? 0} ratio</small>
    </article>
    <article class="metric-card">
      <label>Campeon mas jugado</label>
      <strong>${esc(summary.mostPlayedChampion || "-")}</strong>
    </article>
  `;
}

function renderRecentForm(form) {
  if (!Array.isArray(form) || !form.length) {
    return "";
  }

  const chips = form.map(result => {
    const win = result === "W";
    return `<span class="form-chip ${win ? "win" : "loss"}">${win ? "V" : "D"}</span>`;
  }).join("");

  return `<div class="form-line"><label>Ultimas ${form.length}</label>${chips}</div>`;
}

function formatLpDelta(delta) {
  if (typeof delta !== "number" || Number.isNaN(delta)) {
    return "Sin dato";
  }
  if (delta > 0) return `+${delta} LP`;
  if (delta < 0) return `${delta} LP`;
  return "0 LP";
}

function renderLpMetrics(lpEvolution) {
  if (!lpEvolution || !lpEvolution.history?.length) {
    return "<div class='history-empty'>Aun no hay snapshots de LP para este jugador.</div>";
  }

  const last = lpEvolution.history[lpEvolution.history.length - 1] || {};
  const previous = lpEvolution.history[lpEvolution.history.length - 2] || null;
  const lastDelta = typeof lpEvolution.lastDelta === "number" ? lpEvolution.lastDelta : null;

  return `
    <article class="lp-card ${lastDelta > 0 ? "up" : lastDelta < 0 ? "down" : "flat"}">
      <label>Delta actual</label>
      <strong>${formatLpDelta(lastDelta)}</strong>
      <small>${esc(last.label || `${last.tier || "Sin rango"} ${last.division || ""}`.trim())}</small>
    </article>
    <article class="lp-card up">
      <label>LP ganado</label>
      <strong>+${lpEvolution.gained || 0}</strong>
      <small>Desde que hay historial</small>
    </article>
    <article class="lp-card down">
      <label>LP perdido</label>
      <strong>-${lpEvolution.lost || 0}</strong>
      <small>Desde que hay historial</small>
    </article>
    <article class="lp-card flat">
      <label>Snapshots</label>
      <strong>${lpEvolution.history.length}</strong>
      <small>${previous ? "Con variaciones registradas" : "Esperando siguiente refresh"}</small>
    </article>
  `;
}

function renderLpTrend(lpEvolution) {
  const trend = Array.isArray(lpEvolution?.trend) ? lpEvolution.trend : [];
  if (!trend.length) {
    return "<div class='history-empty'>La tendencia aparecera cuando haya al menos dos snapshots de LP.</div>";
  }

  const maxAbs = Math.max(1, ...trend.map(value => Math.abs(Number(value) || 0)));
  const bars = trend.map(value => {
    const numeric = Number(value) || 0;
    const height = Math.max(14, Math.round((Math.abs(numeric) / maxAbs) * 42));
    const cls = numeric > 0 ? "up" : numeric < 0 ? "down" : "flat";
    const label = numeric > 0 ? `+${numeric}` : `${numeric}`;
    return `<span class="lp-bar ${cls}" style="height:${height}px" title="${label} LP"></span>`;
  }).join("");

  return `<div class="lp-bars">${bars}</div>`;
}

function formatDuration(seconds) {
  const safe = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(safe / 60);
  const rest = safe % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatDamage(value) {
  const number = Number(value) || 0;
  return number.toLocaleString("es-EC");
}

function matchTypeLabel(game) {
  const queueId = Number(game?.queueId || 0);
  if (queueId === 420) return "SoloQ";
  if (queueId === 440) return "Flex";
  if (queueId === 450) return "ARAM";
  if (queueId === 400 || queueId === 430 || queueId === 490) return "Normal";

  const mode = String(game?.gameMode || "").toUpperCase();
  if (mode === "ARAM") return "ARAM";
  if (mode === "CLASSIC") return "Normal";
  return game?.gameMode || "Partida";
}

function renderItems(items) {
  const normalized = Array.isArray(items) ? items.slice(0, 7) : [];
  while (normalized.length < 7) {
    normalized.push({ filled: false, icon: null, name: null });
  }

  return `<div class="items-row">${normalized.map(item => {
    if (!item?.filled || !item?.icon) {
      return `<span class="item-slot empty" aria-hidden="true"></span>`;
    }
    return `<img class="item-icon" src="${esc(item.icon)}" alt="${esc(item.name || "item")}" title="${esc(item.name || "item")}">`;
  }).join("")}</div>`;
}

function renderTeamParticipants(team) {
  if (!Array.isArray(team) || !team.length) {
    return "<div class='history-empty'>Sin datos de composicion.</div>";
  }

  return team.map(member => `
    <div class="comp-row ${member.isPlayer ? "is-player" : ""}">
      <div class="comp-main">
        ${member.championIcon ? `<img class="champ-icon" src="${esc(member.championIcon)}" alt="${esc(member.championName || "?")}">` : ""}
        <div>
          <strong>${esc((member.summonerName && member.summonerName !== "?") ? member.summonerName : "Jugador")}</strong>
          <small>${esc(member.championName || "?")} · ${member.kills}/${member.deaths}/${member.assists}</small>
        </div>
      </div>
      <div class="comp-stats">
        <span>${formatDamage(member.damage)} dmg</span>
        <span>${member.cs ?? 0} CS</span>
      </div>
      ${renderItems(member.items)}
    </div>
  `).join("");
}

function renderDetailedHistory(history) {
  if (!Array.isArray(history) || !history.length) {
    return "<div class='history-empty'>Sin partidas recientes.</div>";
  }

  return history.map((game, index) => {
    const matchId = game.matchId || `match-${index}`;
    const resultLabel = game.win ? "Victoria" : "Derrota";
    return `
      <article class="history-card ${game.win ? "win" : "loss"}">
        <button class="history-head" type="button" data-match-toggle="${esc(matchId)}" aria-expanded="false">
          <div class="head-left">
            <span class="result-pill">${resultLabel}</span>
            ${game.championIcon ? `<img class="champ-icon" src="${esc(game.championIcon)}" alt="${esc(game.champion || "?")}">` : ""}
            <div>
              <strong>${esc(game.champion || "?")}</strong>
              <small>${esc(matchTypeLabel(game))} · ${formatDuration(game.gameDuration)}</small>
            </div>
          </div>
          <div class="head-right">
            <span class="kda">${game.kills}/${game.deaths}/${game.assists}</span>
            <span>${game.killParticipation ?? 0}% KP</span>
            <span>${formatDamage(game.damage)} dmg</span>
            <span>${game.cs ?? 0} CS</span>
          </div>
        </button>
        <div class="composition hidden" data-match-panel="${esc(matchId)}">
          <div class="comp-columns">
            <section>
              <h5>Blue Side</h5>
              ${renderTeamParticipants(game?.composition?.blue || [])}
            </section>
            <section>
              <h5>Red Side</h5>
              ${renderTeamParticipants(game?.composition?.red || [])}
            </section>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

async function fetchLiveStatus(player) {
  if (!player?.puuid) return;
  try {
    const response = await fetch(`/api/live/${player.puuid}`, { cache: "no-store" });
    if (!response.ok) return;
    const data = await response.json();
    renderLiveStatus(player, data);
  } catch (err) {
    console.warn("Live status fetch failed", err);
  }
}

async function openPlayerDetails(player) {
  if (!player?.puuid) return;
  const modal = $("#player-modal");
  const title = $("#modal-player-name");
  const historyList = $("#history-list");
  const summaryMetrics = $("#player-summary-metrics");
  const formStrip = $("#player-form-strip");
  const lpMetrics = $("#lp-metrics");
  const lpTrend = $("#lp-trend");
  const blueList = $("#blue-team-list");
  const redList = $("#red-team-list");
  const liveBox = $("#live-match-box");

  title.textContent = `${player.riotId} · detalle`;
  historyList.innerHTML = "<div class='history-empty'>Cargando historial…</div>";
  summaryMetrics.innerHTML = "<div class='history-empty'>Cargando resumen…</div>";
  formStrip.innerHTML = "";
  lpMetrics.innerHTML = "<div class='history-empty'>Cargando evolucion de LP…</div>";
  lpTrend.innerHTML = "";

  try {
    const [liveResponse, detailsResponse] = await Promise.all([
      fetch(`/api/live/${player.puuid}`, { cache: "no-store" }),
      fetch(`/api/player/${player.puuid}/details?count=10`, { cache: "no-store" })
    ]);

    if (liveResponse.ok) {
      const liveData = await liveResponse.json();
      if (liveData && liveData.in_game) {
        liveBox.classList.remove("hidden");
        blueList.innerHTML = (liveData.blue_team || []).map(member => `<li><span>${esc(member.championName || "?")}</span> · ${esc(member.summonerName || "?")}</li>`).join("");
        redList.innerHTML = (liveData.red_team || []).map(member => `<li><span>${esc(member.championName || "?")}</span> · ${esc(member.summonerName || "?")}</li>`).join("");
      } else {
        liveBox.classList.add("hidden");
        blueList.innerHTML = "";
        redList.innerHTML = "";
      }
    }

    if (detailsResponse.ok) {
      const details = await detailsResponse.json();
      const summary = details?.summary || null;
      const lpEvolution = details?.lpEvolution || null;
      const history = details?.history || [];

      summaryMetrics.innerHTML = renderSummaryMetrics(summary);
      formStrip.innerHTML = renderRecentForm(summary?.recentForm || []);
      lpMetrics.innerHTML = renderLpMetrics(lpEvolution);
      lpTrend.innerHTML = renderLpTrend(lpEvolution);

      historyList.innerHTML = renderDetailedHistory(history);
    } else {
      summaryMetrics.innerHTML = "<div class='history-empty'>No se pudo cargar el resumen del jugador.</div>";
      lpMetrics.innerHTML = "<div class='history-empty'>No se pudo cargar la evolucion de LP.</div>";
      lpTrend.innerHTML = "";
      const historyFallbackResponse = await fetch(`/api/history/${player.puuid}?count=5`, { cache: "no-store" });
      if (historyFallbackResponse.ok) {
        const history = await historyFallbackResponse.json();
        historyList.innerHTML = history.length
          ? history.map(game => `
              <div class="history-item ${game.win ? 'win' : 'loss'}">
                <div>
                  <strong>${esc(game.champion || "?")}</strong>
                  <small>${esc(game.gameMode || "Clasificatoria")}</small>
                </div>
                <div class="kda">${game.kills}/${game.deaths}/${game.assists}</div>
                <span class="result">${game.win ? 'Victoria' : 'Derrota'}</span>
              </div>
            `).join("")
          : "<div class='history-empty'>Sin partidas recientes.</div>";
      } else {
        historyList.innerHTML = "<div class='history-empty'>No se pudo cargar el historial.</div>";
      }
    }
  } catch (error) {
    summaryMetrics.innerHTML = "<div class='history-empty'>No se pudo cargar el resumen del jugador.</div>";
    lpMetrics.innerHTML = "<div class='history-empty'>No se pudo cargar la evolucion de LP.</div>";
    lpTrend.innerHTML = "";
    historyList.innerHTML = "<div class='history-empty'>No se pudo cargar el historial.</div>";
  }

  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
}

function bindHistoryToggles() {
  const historyList = $("#history-list");
  if (!historyList) return;

  historyList.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-match-toggle]");
    if (!trigger) return;

    const matchId = trigger.getAttribute("data-match-toggle");
    const panel = historyList.querySelector(`[data-match-panel="${CSS.escape(matchId || "")}"]`);
    if (!panel) return;

    const isHidden = panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !isHidden);
    trigger.setAttribute("aria-expanded", isHidden ? "true" : "false");
  });
}

function bindPlayerRowClicks() {
  const tbody = document.querySelector("#tbody");
  if (!tbody) return;

  tbody.addEventListener("click", (event) => {
    const rowEl = event.target.closest(".player-row");
    if (!rowEl) return;

    const puuid = rowEl.dataset.playerPuuid;
    const player = (window.__challengePlayers || []).find(item => item.puuid === puuid);
    if (player) {
      openPlayerDetails(player);
    }
  });
}

async function load() {
  if (loading) return;
  loading = true;
  updateRefreshUi();

  const status = $("#status");
  status.className = "status";
  status.textContent = "Consultando el ranking…";

  try {
    const response = await fetch("/api/ranking", { cache: "no-store" });
    const data = await parseApiResponse(response);

    window.__challengePlayers = data.players || [];

    $("#participants").textContent = data.summary.participants;
    $("#most-games").textContent = data.summary.mostGames || "Sin partidas";
    $("#best-wr").textContent = data.summary.bestWinrate || "Sin partidas";
    $("#tbody").innerHTML = data.players.map(row).join("");
    $("#cards").innerHTML = data.players.map(card).join("");

    Promise.all((data.players || []).map(player => fetchLiveStatus(player)));

    nextRefreshAt = Number(data.cache?.nextRefreshAt || (data.updatedAt + 300));
    setStatusFromData(data);
    scheduleAutoRefresh();
  } catch (error) {
    status.className = "status error";
    status.innerHTML = `<b>No se mostraron estadísticas inventadas.</b> ${esc(error.message)}`;
    nextRefreshAt = Math.floor(Date.now() / 1000) + 30;
    scheduleAutoRefresh();
  } finally {
    loading = false;
    updateRefreshUi();
  }
}

$("#refresh").addEventListener("click", () => {
  if (Date.now() / 1000 >= nextRefreshAt) load();
});

countdownTimer = setInterval(updateRefreshUi, 1000);

const modal = $("#player-modal");
const closeBtn = document.querySelector(".close-btn");
if (closeBtn) {
  closeBtn.addEventListener("click", () => {
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  });
}
if (modal) {
  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
    }
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && modal && !modal.classList.contains("hidden")) {
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() / 1000 >= nextRefreshAt && !loading) {
    load();
  }
});

bindPlayerRowClicks();
bindHistoryToggles();

load();
