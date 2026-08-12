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
  return `<tr>
    <td><div class="pos ${posClass(player.position)}">${player.position}</div></td>
    <td><div class="player">${avatar(player)}<div><b>${esc(player.riotId)}</b><small>Nivel ${player.summonerLevel ?? "—"} · LAN</small></div></div></td>
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

    $("#participants").textContent = data.summary.participants;
    $("#most-games").textContent = data.summary.mostGames || "Sin partidas";
    $("#best-wr").textContent = data.summary.bestWinrate || "Sin partidas";
    $("#tbody").innerHTML = data.players.map(row).join("");
    $("#cards").innerHTML = data.players.map(card).join("");

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

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && Date.now() / 1000 >= nextRefreshAt && !loading) {
    load();
  }
});

load();
