const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;"
}[char]));

const API_BASE = (() => {
  const { protocol, hostname, port } = window.location;
  const isLocalHost = hostname === "127.0.0.1" || hostname === "localhost";

  // FastAPI sirve frontend y API juntos en :8000. Si la página se abre con
  // VS Code Live Server (por ejemplo :5500), las consultas deben ir al backend.
  if (isLocalHost && port && port !== "8000") {
    return `${protocol}//${hostname}:8000`;
  }
  return "";
})();

const apiUrl = path => `${API_BASE}${path}`;

async function apiFetch(path, options = {}, timeoutMs = 30000) {
  const { signal: externalSignal, ...fetchOptions } = options;
  const controller = new AbortController();
  let timedOut = false;
  const cancelFromCaller = () => controller.abort();
  if (externalSignal?.aborted) {
    cancelFromCaller();
  } else {
    externalSignal?.addEventListener("abort", cancelFromCaller, { once: true });
  }
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fetch(apiUrl(path), { ...fetchOptions, signal: controller.signal });
  } catch (error) {
    if (error?.name === "AbortError") {
      if (externalSignal?.aborted && !timedOut) {
        const canceled = new Error("La consulta fue cancelada porque cambió el jugador o el apartado.");
        canceled.name = "RequestCanceledError";
        throw canceled;
      }
      const timeoutError = new Error(`La consulta ${path} tardó más de ${Math.round(timeoutMs / 1000)} segundos.`);
      timeoutError.code = "REQUEST_TIMEOUT";
      throw timeoutError;
    }
    throw error;
  } finally {
    clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", cancelFromCaller);
  }
}

function apiConnectionMessage(error) {
  if (error?.status) {
    return error.message || `El backend respondió HTTP ${error.status}.`;
  }
  if (error?.code === "REQUEST_TIMEOUT") {
    return `${error.message} El backend continúa activo; Riot Games está tardando o limitando temporalmente las consultas.`;
  }
  if (API_BASE) {
    return `No se pudo conectar con el backend en ${API_BASE}. Ejecuta 3_INICIAR_WEB.bat y confirma que siga abierta la ventana del servidor. ${error?.message || ""}`.trim();
  }
  return error?.message || "No se pudo consultar el backend.";
}

let nextRefreshAt = 0;
let autoRefreshTimer = null;
let countdownTimer = null;
let loading = false;
let liveClockTimer = null;
let liveRefreshTimer = null;
let liveStatusTimer = null;
let liveStatusLoading = false;
let activePlayerRequestId = 0;
let lastRankingUpdatedAt = 0;
let activeModalPlayer = null;
let activeModalTab = "overview";
let livePanelLoadedRequestId = 0;
let detailsPanelLoadedRequestId = 0;
let activeHistory = [];
let activeHistoryPage = 1;
let activeHistoryRenderer = null;
const modalRequestControllers = new Map();
const HISTORY_PAGE_SIZE = 3;
window.__challengePlayers = [];
window.__liveStatuses = {};

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
  return `<article class="card player-card" data-player-puuid="${esc(player.puuid || "")}">
    <div class="card-head">
      <div class="pos ${posClass(player.position)}">${player.position}</div>${avatar(player)}
      <div class="id"><b>${esc(player.riotId)}</b><small>${esc(player.rank.label)} · ${player.rank.lp} LP</small><span class="live-badge live-off">No en vivo</span></div>
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

  const scheduledDelay = nextRefreshAt * 1000 - Date.now();
  const remainingMs = scheduledDelay > 0 ? scheduledDelay : 60000;
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
    const error = new Error(data?.detail || `Error HTTP ${response.status}.`);
    error.status = response.status;
    throw error;
  }

  return data;
}

function renderLiveStatus(player, payload) {
  const containers = document.querySelectorAll(`[data-player-puuid="${CSS.escape(player.puuid || "")}"]`);
  containers.forEach(container => {
    const badge = container.querySelector(".live-badge");
    if (!badge) return;
    if (payload?.state === "unknown" || payload?.in_game == null) {
      badge.textContent = "Estado pendiente";
      badge.classList.remove("live-on", "live-off");
      badge.classList.add("live-unknown");
    } else if (payload && payload.in_game) {
      badge.textContent = "🔴 EN JUEGO";
      badge.classList.remove("live-off", "live-unknown");
      badge.classList.add("live-on");
    } else {
      badge.textContent = "No en vivo";
      badge.classList.remove("live-on", "live-unknown");
      badge.classList.add("live-off");
    }
  });

  if (activeModalPlayer?.puuid === player.puuid) {
    updateLiveTabIndicator(payload);
  }
}

function updateLiveTabIndicator(payload) {
  const indicator = $("#live-tab-indicator");
  if (!indicator) return;
  indicator.className = "tab-indicator";
  if (payload?.in_game) {
    indicator.classList.add("is-live");
    indicator.title = "Jugador en partida";
  } else if (payload?.state === "unknown" || payload?.in_game == null) {
    indicator.classList.add("is-unknown");
    indicator.title = "Estado pendiente";
  } else {
    indicator.title = "Jugador fuera de partida";
  }
}

function renderLiveForm(form) {
  if (!Array.isArray(form) || !form.length) {
    return `<span class="live-muted">Sin muestra reciente</span>`;
  }
  return `<div class="live-form">${form.map(result => `
    <span class="live-form-dot ${result === "W" ? "win" : "loss"}" title="${result === "W" ? "Victoria" : "Derrota"}">${result === "W" ? "V" : "D"}</span>
  `).join("")}</div>`;
}

function renderLiveInsights(insights) {
  if (!Array.isArray(insights) || !insights.length) return "";
  const tones = new Set(["positive", "danger", "warning", "neutral"]);
  return `<div class="live-insights">${insights.map(insight => {
    const tone = tones.has(insight?.tone) ? insight.tone : "neutral";
    return `<span class="insight ${tone}">${esc(insight?.label || "Señal")}</span>`;
  }).join("")}</div>`;
}

function renderVersusMember(member, requestedPuuid, side) {
  if (!member) {
    return `<article class="versus-player ${side}-member is-empty"><span>Sin datos</span></article>`;
  }
  const rank = member?.rank || {};
  const recent = member?.recent || {};
  const avg = recent?.avg || {};
  const role = member?.mainRole || recent?.mainRole || {};
  const isTarget = Boolean(member?.puuid && member.puuid === requestedPuuid);
  const spells = Array.isArray(member?.spells) ? member.spells : [];
  const rune = Array.isArray(member?.runes) ? member.runes.find(item => item?.icon) : null;
  const seasonLine = rank.games
    ? `${Number(rank.winrate || 0).toFixed(1)}% · ${rank.wins}V/${rank.losses}D`
    : "Sin partidas clasificatorias";

  return `
    <article class="versus-player ${side}-member ${isTarget ? "is-target" : ""}">
      <div class="versus-identity">
        <div class="live-portrait-wrap">
          ${member?.championIcon ? `<img class="live-champion" src="${esc(member.championIcon)}" alt="${esc(member.championName || "Campeón")}">` : `<span class="live-champion fallback">?</span>`}
          ${rune ? `<img class="live-rune" src="${esc(rune.icon)}" alt="${esc(rune.name || "Runa")}" title="${esc(rune.name || "Runa")}">` : ""}
        </div>
        <div class="live-spells">${spells.map(spell => spell?.icon
          ? `<img src="${esc(spell.icon)}" alt="${esc(spell.name || "Hechizo")}" title="${esc(spell.name || "Hechizo")}">`
          : `<span aria-hidden="true"></span>`).join("")}</div>
        <div class="versus-copy">
          <div class="versus-name"><strong title="${esc(member?.summonerName || "Jugador")}">${esc(member?.summonerName || "Jugador")}</strong>${isTarget ? `<span class="you-pill">TÚ</span>` : ""}</div>
          <b>${esc(member?.championName || "Campeón")}</b>
          <strong class="live-kda"><i>${avg.kills ?? 0}</i> / <i class="deaths">${avg.deaths ?? 0}</i> / <i>${avg.assists ?? 0}</i></strong>
          <small>${seasonLine}</small>
          <small>${avg.csPerMinute ?? 0} CS/min · ${avg.damagePerMinute ?? 0} DPM</small>
          ${renderLiveForm(recent.recentForm)}
        </div>
      </div>
      <div class="versus-rank">
        <b>${esc(rank.label || "Sin clasificación")}${rank.games ? ` · ${rank.lp || 0} LP` : ""}</b>
        <small>${esc(role.label || "Rol flexible")} · ${recent.games || 0} recientes</small>
        ${renderLiveInsights((member?.insights || []).slice(0, 2))}
      </div>
    </article>
  `;
}

function renderVersusBoard(blueTeam, redTeam, requestedPuuid) {
  const roles = [
    ["TOP", "Superior"],
    ["JUNGLE", "Jungla"],
    ["MIDDLE", "Central"],
    ["BOTTOM", "Tirador"],
    ["UTILITY", "Soporte"],
  ];
  const blueUsed = new Set();
  const redUsed = new Set();
  const pick = (team, used, roleKey, fallbackIndex) => {
    let index = team.findIndex((member, idx) => !used.has(idx) && (member?.mainRole?.key || member?.recent?.mainRole?.key) === roleKey);
    if (index < 0 && team[fallbackIndex] && !used.has(fallbackIndex)) index = fallbackIndex;
    if (index < 0) index = team.findIndex((_, idx) => !used.has(idx));
    if (index >= 0) used.add(index);
    return index >= 0 ? team[index] : null;
  };

  return roles.map(([key, label], index) => {
    const blue = pick(blueTeam, blueUsed, key, index);
    const red = pick(redTeam, redUsed, key, index);
    return `
      <div class="versus-row">
        ${renderVersusMember(blue, requestedPuuid, "blue")}
        <div class="versus-role"><span>${esc(label)}</span><b>VS</b></div>
        ${renderVersusMember(red, requestedPuuid, "red")}
      </div>
    `;
  }).join("");
}

function renderTeamSummary(summary, side) {
  const avg = summary?.averages || {};
  const kda = avg?.kda || {};
  return `
    <article class="team-summary-card ${side}-summary">
      <header><span></span>${side === "blue" ? "Equipo azul" : "Equipo rojo"}<small>${summary?.sampledPlayers || 0}/5 con muestra</small></header>
      <div class="team-metric-grid">
        <div><small>Victoria</small><b>${Number(avg.winrate || 0).toFixed(1)}%</b></div>
        <div><small>KDA promedio</small><b><i>${kda.kills ?? 0}</i> / <em>${kda.deaths ?? 0}</em> / <i>${kda.assists ?? 0}</i></b></div>
        <div><small>Oro / min</small><b>${avg.goldPerMinute || 0}</b></div>
        <div><small>Daño / min</small><b>${avg.damagePerMinute || 0}</b></div>
        <div><small>Centinelas / min</small><b>${Number(avg.visionWardsPerMinute || 0).toFixed(2)}</b></div>
      </div>
      ${renderLiveInsights(summary?.insights)}
    </article>
  `;
}

function renderBuildItems(items) {
  if (!Array.isArray(items) || !items.length) return `<span class="build-empty">Sin objetos disponibles para este parche.</span>`;
  return `<div class="build-items">${items.map(item => `
    <div class="build-item" title="${esc(item?.reason || "")}">
      <div class="build-icon-wrap">
        <img src="${esc(item?.icon || "")}" alt="${esc(item?.name || "Objeto")}">
        ${Number(item?.quantity || 1) > 1 ? `<b>x${Number(item.quantity)}</b>` : ""}
      </div>
      <span>${esc(item?.name || "Objeto")}<small>${esc(item?.reason || "")}</small></span>
    </div>
  `).join("")}</div>`;
}

function itemizationPhase(itemization, seconds) {
  const phases = Array.isArray(itemization?.phasePlan) ? itemization.phasePlan : [];
  return phases.find(phase => seconds >= Number(phase.minSeconds || 0) && (phase.maxSeconds == null || seconds < Number(phase.maxSeconds))) || phases[phases.length - 1] || null;
}

function updateItemizationPhase(itemization, seconds) {
  const active = itemizationPhase(itemization, seconds);
  if (!active) return;
  document.querySelectorAll("[data-build-phase]").forEach(card => {
    card.classList.toggle("active", card.getAttribute("data-build-phase") === active.key);
  });
  const badge = $("#build-current-phase");
  if (badge) badge.textContent = `AHORA · ${active.label}`;
}

function renderLiveItemization(itemization, initialLength) {
  const panel = $("#live-itemization");
  const content = $("#itemization-content");
  if (!panel || !content) return;
  panel.classList.remove("hidden");
  if (!itemization) {
    content.innerHTML = `<div class="build-empty">El catálogo de objetos no estuvo disponible en esta consulta.</div>`;
    return;
  }
  const phases = Array.isArray(itemization.phasePlan) ? itemization.phasePlan : [];
  content.innerHTML = `
    <div class="itemization-heading">
      <div class="build-champion">
        ${itemization.championIcon ? `<img src="${esc(itemization.championIcon)}" alt="${esc(itemization.championName)}">` : ""}
        <div><span>ITEMIZACIÓN ADAPTATIVA</span><h5>${esc(itemization.championName || "Campeón")} · ${esc(itemization.matchup?.role || "Rol flexible")}</h5></div>
      </div>
      <div class="build-source"><span id="build-current-phase"></span><a href="${esc(itemization.source?.url || "#")}" target="_blank" rel="noopener noreferrer">↗ ${esc(itemization.source?.label || "Armados globales")}</a></div>
    </div>
    <p class="build-method">${esc(itemization.method || "")} ${esc(itemization.matchup?.tip || "")}</p>
    <div class="build-quick-grid">
      <section><h6>Inicio de partida</h6>${renderBuildItems(itemization.starter)}</section>
      <section><h6>Primera vuelta</h6>${renderBuildItems(itemization.firstRecall)}</section>
      <section><h6>Núcleo 5v5</h6>${renderBuildItems((itemization.core || []).slice(0, 5))}</section>
    </div>
    <div class="phase-plan">${phases.map(phase => `
      <article data-build-phase="${esc(phase.key)}" class="item-phase-card">
        <header><span>${esc(phase.label)}</span><small>${phase.maxSeconds == null ? `+${Math.round(Number(phase.minSeconds || 0) / 60)} min` : `${Math.round(Number(phase.minSeconds || 0) / 60)}–${Math.round(Number(phase.maxSeconds) / 60)} min`}</small></header>
        <p>${esc(phase.focus || "")}</p>
        ${renderBuildItems(phase.items)}
      </article>
    `).join("")}</div>
    <div class="build-adaptations">
      <h6>Ajustes por composición</h6>
      ${(itemization.adaptations || []).map(entry => `
        <article>
          ${entry?.item?.icon ? `<img src="${esc(entry.item.icon)}" alt="${esc(entry.item.name || "Objeto")}">` : `<span class="adaptation-marker">!</span>`}
          <div><b>${esc(entry?.label || "Ajuste")}</b><small>${esc(entry?.reason || "")}${entry?.item?.name ? ` · ${esc(entry.item.name)}` : ""}</small></div>
        </article>
      `).join("")}
    </div>
    <p class="build-live-note">La fase cambia con el reloj. Las compras reales no están disponibles en Spectator; confirma el orden según el oro y quién vaya por delante.</p>
  `;
  updateItemizationPhase(itemization, initialLength);
}

function renderLiveMatch(liveData) {
  const board = $("#live-versus-board");
  const teamStats = $("#team-stats-comparison");
  const meta = $("#live-match-meta");
  if (!board || !teamStats || !meta) return;

  const requestedPuuid = liveData?.requestedPuuid || "";
  const blueTeam = Array.isArray(liveData?.blue_team) ? liveData.blue_team : [];
  const redTeam = Array.isArray(liveData?.red_team) ? liveData.red_team : [];
  board.innerHTML = renderVersusBoard(blueTeam, redTeam, requestedPuuid);
  teamStats.innerHTML = `
    ${renderTeamSummary(liveData?.teamStats?.blue, "blue")}
    <div class="team-summary-vs"><span>VS</span></div>
    ${renderTeamSummary(liveData?.teamStats?.red, "red")}
  `;

  if (liveClockTimer) clearInterval(liveClockTimer);
  const initialLength = Math.max(0, Number(liveData?.gameLength) || 0);
  const fetchedAt = (Number(liveData?.fetchedAt) || Date.now() / 1000) * 1000;
  renderLiveItemization(liveData?.itemization, initialLength);
  meta.innerHTML = `
    <span class="live-now"><i></i> EN VIVO</span>
    <span>${esc(matchTypeLabel({ queueId: liveData?.queueId, gameMode: liveData?.gameMode }))}</span>
    <span id="live-match-time">${formatDuration(initialLength)}</span>
    <span>${esc(liveData?.platformId || "LAN")}</span>
  `;
  const updateClock = () => {
    const clock = $("#live-match-time");
    if (!clock) return;
    const elapsed = Math.max(0, Math.floor((Date.now() - fetchedAt) / 1000));
    const total = initialLength + elapsed;
    clock.textContent = formatDuration(total);
    updateItemizationPhase(liveData?.itemization, total);
  };
  updateClock();
  liveClockTimer = setInterval(updateClock, 1000);
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

function renderRankingSummary(player) {
  const challenge = player?.challenge || {};
  const rank = player?.rank || {};
  const best = challenge.mostWinningChampion;
  return `
    <article class="metric-card">
      <label>Partidas en agosto</label>
      <strong>${Number(challenge.games || 0)}</strong>
    </article>
    <article class="metric-card">
      <label>Récord de agosto</label>
      <strong>${Number(challenge.wins || 0)}V / ${Number(challenge.losses || 0)}D</strong>
    </article>
    <article class="metric-card">
      <label>Win rate</label>
      <strong>${Number(challenge.winrate || 0).toFixed(1)}%</strong>
    </article>
    <article class="metric-card">
      <label>Rango actual</label>
      <strong>${esc(rank.label || "Sin clasificación")}</strong>
    </article>
    <article class="metric-card">
      <label>LP actuales</label>
      <strong>${Number(rank.lp || 0)} LP</strong>
    </article>
    <article class="metric-card">
      <label>Más victorias</label>
      <strong>${esc(best?.name || "Sin muestra")}</strong>
      ${best ? `<small>${Number(best.wins || 0)} victorias</small>` : ""}
    </article>
  `;
}

function cancelModalRequest(channel) {
  const controller = modalRequestControllers.get(channel);
  if (controller) controller.abort();
  modalRequestControllers.delete(channel);
}

function cancelAllModalRequests() {
  for (const controller of modalRequestControllers.values()) controller.abort();
  modalRequestControllers.clear();
}

function beginModalRequest(channel) {
  cancelModalRequest(channel);
  const controller = new AbortController();
  modalRequestControllers.set(channel, controller);
  return controller;
}

function finishModalRequest(channel, controller) {
  if (modalRequestControllers.get(channel) === controller) {
    modalRequestControllers.delete(channel);
  }
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

async function fetchAllLiveStatuses(players) {
  if (liveStatusLoading) return;
  const validPlayers = (players || []).filter(player => player?.puuid);
  if (!validPlayers.length) return;
  liveStatusLoading = true;
  try {
    const puuids = validPlayers.map(player => encodeURIComponent(player.puuid)).join(",");
    const response = await apiFetch(`/api/live-statuses?puuids=${puuids}`, { cache: "no-store" }, 20000);
    const data = await parseApiResponse(response);
    const statuses = Object.fromEntries((data?.statuses || []).map(status => [status.puuid, status]));
    window.__liveStatuses = { ...window.__liveStatuses, ...statuses };
    validPlayers.forEach(player => {
      const status = statuses[player.puuid];
      if (status) renderLiveStatus(player, status);
    });
  } catch (err) {
    console.warn("Live status batch failed", err);
  } finally {
    liveStatusLoading = false;
  }
}

function scheduleLiveStatusRefresh() {
  if (liveStatusTimer) clearInterval(liveStatusTimer);
  liveStatusTimer = setInterval(() => {
    if (!document.hidden) fetchAllLiveStatuses(window.__challengePlayers || []);
  }, 30000);
}

async function loadPlayerLivePanel(player, requestId) {
  const board = $("#live-versus-board");
  const teamStats = $("#team-stats-comparison");
  const itemization = $("#live-itemization");
  const liveMeta = $("#live-match-meta");
  const liveBox = $("#live-match-box");
  const requestController = beginModalRequest("live");

  try {
    const response = await apiFetch(
      `/api/live/${player.puuid}`,
      { cache: "no-store", signal: requestController.signal },
      35000
    );
    const liveData = await parseApiResponse(response);
    if (requestId !== activePlayerRequestId) return;
    livePanelLoadedRequestId = requestId;

    if (liveData?.in_game) {
      liveBox.classList.remove("hidden");
      window.__liveStatuses[player.puuid] = { state: "in_game", in_game: true, gameId: liveData.gameId };
      updateLiveTabIndicator(window.__liveStatuses[player.puuid]);
      renderLiveMatch(liveData);
      return;
    }

    if (liveClockTimer) clearInterval(liveClockTimer);
    liveBox.classList.remove("hidden");
    liveMeta.innerHTML = "<span>Fuera de partida</span>";
    board.innerHTML = `<div class="live-empty-state"><b>No hay una partida activa.</b><small>El estado se volverá a comprobar automáticamente.</small></div>`;
    teamStats.innerHTML = "";
    itemization.classList.add("hidden");
    window.__liveStatuses[player.puuid] = { state: "idle", in_game: false };
    updateLiveTabIndicator(window.__liveStatuses[player.puuid]);
  } catch (error) {
    if (error?.name === "RequestCanceledError") return;
    if (requestId !== activePlayerRequestId) return;
    livePanelLoadedRequestId = requestId;
    if (liveClockTimer) clearInterval(liveClockTimer);
    liveBox.classList.remove("hidden");
    liveMeta.innerHTML = "<span>No disponible</span>";
    board.innerHTML = `<div class="live-loading live-load-error"><b>No se pudo consultar la partida activa.</b><small>${esc(apiConnectionMessage(error))}</small><button type="button" class="retry-load" data-retry-live>Reintentar</button></div>`;
    teamStats.innerHTML = "";
    itemization.classList.add("hidden");
    updateLiveTabIndicator({ state: "unknown", in_game: null });
  } finally {
    finishModalRequest("live", requestController);
  }
}

function renderSimpleHistory(history) {
  if (!Array.isArray(history) || !history.length) {
    return "<div class='history-empty'>Sin partidas recientes.</div>";
  }
  return history.map(game => `
    <div class="history-item ${game.win ? "win" : "loss"}">
      <div>
        <strong>${esc(game.champion || "?")}</strong>
        <small>${esc(game.gameMode || "Clasificatoria")}</small>
      </div>
      <div class="kda">${game.kills}/${game.deaths}/${game.assists}</div>
      <span class="result">${game.win ? "Victoria" : "Derrota"}</span>
    </div>
  `).join("");
}

function renderHistoryPage(page = 1) {
  const historyList = $("#history-list");
  if (!historyList) return;
  const totalPages = Math.max(1, Math.ceil(activeHistory.length / HISTORY_PAGE_SIZE));
  activeHistoryPage = Math.max(1, Math.min(Number(page) || 1, totalPages));
  const start = (activeHistoryPage - 1) * HISTORY_PAGE_SIZE;
  const pageItems = activeHistory.slice(start, start + HISTORY_PAGE_SIZE);
  const renderer = activeHistoryRenderer || renderDetailedHistory;
  const pages = Array.from({ length: totalPages }, (_, index) => index + 1);
  const pagination = totalPages > 1 ? `
    <nav class="history-pagination" aria-label="Páginas del historial">
      <button type="button" data-history-page="${activeHistoryPage - 1}" ${activeHistoryPage === 1 ? "disabled" : ""} aria-label="Página anterior">‹</button>
      ${pages.map(number => `<button type="button" data-history-page="${number}" class="${number === activeHistoryPage ? "active" : ""}" ${number === activeHistoryPage ? `aria-current="page"` : ""}>${number}</button>`).join("")}
      <button type="button" data-history-page="${activeHistoryPage + 1}" ${activeHistoryPage === totalPages ? "disabled" : ""} aria-label="Página siguiente">›</button>
      <span>${start + 1}–${Math.min(start + HISTORY_PAGE_SIZE, activeHistory.length)} de ${activeHistory.length}</span>
    </nav>
  ` : "";
  historyList.innerHTML = renderer(pageItems) + pagination;
}

function setHistoryData(history, renderer = renderDetailedHistory) {
  activeHistory = Array.isArray(history) ? history : [];
  activeHistoryRenderer = renderer;
  activeHistoryPage = 1;
  renderHistoryPage(1);
}

async function loadPlayerDetailsPanel(player, requestId) {
  const historyList = $("#history-list");
  const summaryMetrics = $("#player-summary-metrics");
  const formStrip = $("#player-form-strip");
  const requestController = beginModalRequest("history");

  historyList.innerHTML = "<div class='history-empty loading-message'>Consultando hasta 10 partidas recientes…</div>";

  try {
    const response = await apiFetch(
      `/api/player/${player.puuid}/details?count=10`,
      { cache: "no-store", signal: requestController.signal },
      60000
    );
    const details = await parseApiResponse(response);
    if (requestId !== activePlayerRequestId) return;

    const summary = details?.summary || null;
    const history = details?.history || [];
    summaryMetrics.innerHTML = renderSummaryMetrics(summary);
    formStrip.innerHTML = renderRecentForm(summary?.recentForm || []);
    setHistoryData(history, renderDetailedHistory);
    detailsPanelLoadedRequestId = requestId;
  } catch (error) {
    if (error?.name === "RequestCanceledError") return;
    if (requestId !== activePlayerRequestId) return;
    const message = esc(apiConnectionMessage(error));
    detailsPanelLoadedRequestId = requestId;
    historyList.innerHTML = `<div class="history-empty load-error"><b>No se pudo cargar el historial.</b><small>${message}</small><button type="button" class="retry-load" data-retry-history>Reintentar</button></div>`;
  } finally {
    finishModalRequest("history", requestController);
  }
}

async function loadPlayerLpEvolutionPanel(player, requestId) {
  const lpMetrics = $("#lp-metrics");
  const lpTrend = $("#lp-trend");
  const requestController = beginModalRequest("overview");

  try {
    const response = await apiFetch(
      `/api/player/${player.puuid}/lp-history?limit=24`,
      { cache: "no-store", signal: requestController.signal },
      10000
    );
    const lpEvolution = await parseApiResponse(response);
    if (requestId !== activePlayerRequestId) return;
    lpMetrics.innerHTML = renderLpMetrics(lpEvolution);
    lpTrend.innerHTML = renderLpTrend(lpEvolution);
  } catch (error) {
    if (error?.name === "RequestCanceledError") return;
    if (requestId !== activePlayerRequestId) return;
    lpMetrics.innerHTML = `<div class="history-empty load-error"><b>No se pudo cargar la evolución de LP.</b><small>${esc(apiConnectionMessage(error))}</small></div>`;
    lpTrend.innerHTML = "";
  } finally {
    finishModalRequest("overview", requestController);
  }
}

function startLivePanelRefresh(player, requestId) {
  if (liveRefreshTimer) clearInterval(liveRefreshTimer);
  liveRefreshTimer = setInterval(() => {
    const modal = $("#player-modal");
    if (
      activeModalTab === "live" &&
      requestId === activePlayerRequestId &&
      modal &&
      !modal.classList.contains("hidden")
    ) {
      loadPlayerLivePanel(player, requestId);
    }
  }, 60000);
}

function activateModalTab(tabName) {
  const validTabs = new Set(["overview", "live", "history"]);
  const nextTab = validTabs.has(tabName) ? tabName : "overview";
  activeModalTab = nextTab;

  if (nextTab !== "live") {
    cancelModalRequest("live");
    if (liveRefreshTimer) clearInterval(liveRefreshTimer);
    liveRefreshTimer = null;
  }
  if (nextTab !== "history") cancelModalRequest("history");

  document.querySelectorAll("[data-modal-tab]").forEach(button => {
    const active = button.dataset.modalTab === nextTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", active ? "true" : "false");
    button.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll("[data-modal-page]").forEach(page => {
    const active = page.dataset.modalPage === nextTab;
    page.classList.toggle("active", active);
    page.hidden = !active;
  });

  if (nextTab === "live" && activeModalPlayer?.puuid) {
    if (livePanelLoadedRequestId !== activePlayerRequestId) {
      const board = $("#live-versus-board");
      const meta = $("#live-match-meta");
      if (meta) meta.innerHTML = "<span>Buscando partida activa…</span>";
      if (board) board.innerHTML = "<div class='live-loading'>Consultando Spectator API…</div>";
      loadPlayerLivePanel(activeModalPlayer, activePlayerRequestId);
    }
    startLivePanelRefresh(activeModalPlayer, activePlayerRequestId);
  } else if (nextTab === "history" && activeModalPlayer?.puuid) {
    if (detailsPanelLoadedRequestId !== activePlayerRequestId) {
      loadPlayerDetailsPanel(activeModalPlayer, activePlayerRequestId);
    }
  }
}

function bindModalTabs() {
  const tabs = document.querySelector(".modal-tabs");
  if (!tabs) return;
  tabs.addEventListener("click", event => {
    const button = event.target.closest("[data-modal-tab]");
    if (button) activateModalTab(button.dataset.modalTab);
  });
  tabs.addEventListener("keydown", event => {
    if (!new Set(["ArrowLeft", "ArrowRight"]).has(event.key)) return;
    const buttons = Array.from(tabs.querySelectorAll("[data-modal-tab]"));
    const current = buttons.findIndex(button => button.classList.contains("active"));
    const direction = event.key === "ArrowRight" ? 1 : -1;
    const next = buttons[(current + direction + buttons.length) % buttons.length];
    event.preventDefault();
    next.focus();
    activateModalTab(next.dataset.modalTab);
  });
}

function openPlayerDetails(player) {
  if (!player?.puuid) return;
  cancelAllModalRequests();
  const requestId = ++activePlayerRequestId;
  const modal = $("#player-modal");
  const title = $("#modal-player-name");
  const historyList = $("#history-list");
  const summaryMetrics = $("#player-summary-metrics");
  const formStrip = $("#player-form-strip");
  const lpMetrics = $("#lp-metrics");
  const lpTrend = $("#lp-trend");
  const board = $("#live-versus-board");
  const teamStats = $("#team-stats-comparison");
  const itemization = $("#live-itemization");
  const liveMeta = $("#live-match-meta");
  const liveBox = $("#live-match-box");

  activeModalPlayer = player;
  livePanelLoadedRequestId = 0;
  detailsPanelLoadedRequestId = 0;
  activeHistory = [];
  activeHistoryPage = 1;
  activeHistoryRenderer = renderDetailedHistory;

  title.textContent = `${player.riotId} · detalle`;
  historyList.innerHTML = "<div class='history-empty'>Abre este apartado para consultar las partidas recientes.</div>";
  summaryMetrics.innerHTML = renderRankingSummary(player);
  formStrip.innerHTML = "<div class='form-line'><label>Resumen disponible al instante · el historial reciente se consulta en su apartado</label></div>";
  lpMetrics.innerHTML = "<div class='history-empty loading-message'>Cargando evolución de LP…</div>";
  lpTrend.innerHTML = "";
  liveBox.classList.remove("hidden");
  liveMeta.innerHTML = "<span>Consulta bajo demanda</span>";
  board.innerHTML = "<div class='live-loading'>Abre esta pestaña para consultar la partida.</div>";
  teamStats.innerHTML = "";
  itemization.classList.add("hidden");
  if (liveClockTimer) clearInterval(liveClockTimer);
  if (liveRefreshTimer) clearInterval(liveRefreshTimer);

  modal.classList.remove("hidden");
  modal.setAttribute("aria-hidden", "false");
  updateLiveTabIndicator(window.__liveStatuses[player.puuid]);
  activateModalTab("overview");
  loadPlayerLpEvolutionPanel(player, requestId);
}

function bindHistoryToggles() {
  const historyList = $("#history-list");
  if (!historyList) return;

  historyList.addEventListener("click", (event) => {
    const retryHistory = event.target.closest("[data-retry-history]");
    if (retryHistory && activeModalPlayer?.puuid) {
      detailsPanelLoadedRequestId = 0;
      loadPlayerDetailsPanel(activeModalPlayer, activePlayerRequestId);
      return;
    }

    const pageButton = event.target.closest("[data-history-page]");
    if (pageButton && !pageButton.disabled) {
      renderHistoryPage(Number(pageButton.dataset.historyPage));
      return;
    }

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
  const cards = document.querySelector("#cards");
  const openFromEvent = event => {
    const playerEl = event.target.closest("[data-player-puuid]");
    if (!playerEl) return;
    const puuid = playerEl.dataset.playerPuuid;
    const player = (window.__challengePlayers || []).find(item => item.puuid === puuid);
    if (player) {
      openPlayerDetails(player);
    }
  };

  if (tbody) tbody.addEventListener("click", openFromEvent);
  if (cards) cards.addEventListener("click", openFromEvent);
}

async function load() {
  if (loading) return;
  loading = true;
  updateRefreshUi();

  const status = $("#status");
  status.className = "status";
  status.textContent = "Consultando el ranking…";

  try {
    const response = await apiFetch("/api/ranking", { cache: "no-store" }, 30000);
    const data = await parseApiResponse(response);

    window.__challengePlayers = data.players || [];

    $("#participants").textContent = data.summary.participants;
    $("#most-games").textContent = data.summary.mostGames || "Sin partidas";
    $("#best-wr").textContent = data.summary.bestWinrate || "Sin partidas";
    const updatedAt = Number(data.updatedAt || 0);
    const rankingChanged = updatedAt !== lastRankingUpdatedAt || !$("#tbody").children.length;
    if (rankingChanged) {
      $("#tbody").innerHTML = data.players.map(row).join("");
      $("#cards").innerHTML = data.players.map(card).join("");
      lastRankingUpdatedAt = updatedAt;
    }

    fetchAllLiveStatuses(data.players || []);
    scheduleLiveStatusRefresh();

    nextRefreshAt = Number(data.cache?.nextRefreshAt || (data.updatedAt + 300));
    setStatusFromData(data);
    scheduleAutoRefresh();
  } catch (error) {
    status.className = "status error";
    status.innerHTML = `<b>No se pudieron cargar las estadísticas de Riot.</b> ${esc(apiConnectionMessage(error))}`;
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
function closePlayerModal() {
  activePlayerRequestId += 1;
  cancelAllModalRequests();
  activeModalPlayer = null;
  livePanelLoadedRequestId = 0;
  detailsPanelLoadedRequestId = 0;
  if (liveClockTimer) clearInterval(liveClockTimer);
  if (liveRefreshTimer) clearInterval(liveRefreshTimer);
  liveClockTimer = null;
  liveRefreshTimer = null;
  modal.classList.add("hidden");
  modal.setAttribute("aria-hidden", "true");
}

if (closeBtn) {
  closeBtn.addEventListener("click", closePlayerModal);
}
if (modal) {
  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      closePlayerModal();
    }
  });
}

function bindLiveRetry() {
  const board = $("#live-versus-board");
  if (!board) return;
  board.addEventListener("click", event => {
    if (!event.target.closest("[data-retry-live]") || !activeModalPlayer?.puuid) return;
    livePanelLoadedRequestId = 0;
    board.innerHTML = "<div class='live-loading'>Consultando Spectator API…</div>";
    loadPlayerLivePanel(activeModalPlayer, activePlayerRequestId);
  });
}

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && modal && !modal.classList.contains("hidden")) {
    closePlayerModal();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    fetchAllLiveStatuses(window.__challengePlayers || []);
    if (Date.now() / 1000 >= nextRefreshAt && !loading) load();
  }
});

bindPlayerRowClicks();
bindHistoryToggles();
bindModalTabs();
bindLiveRetry();

load();
