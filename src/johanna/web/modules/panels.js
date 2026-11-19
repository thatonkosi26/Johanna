import { state } from "./state.js";
import { emptyState, esc, freshnessLabel, services, stamp, stat, stateLabel, tz } from "./format.js";
import { calendarStatus } from "./status.js";

// ---------------------------------------------------------------- other panels
export function calendarMarkup(limit = 4) {
  const calendar = state.data?.calendar ?? { events: [] };
  if (calendar.error) return emptyState("Calendar unavailable", calendar.error, "error-state");
  const events = (calendar.events ?? []).slice(0, limit);
  if (!events.length) {
    const status = calendarStatus();
    const previous = status.latest_snapshot_week && status.latest_snapshot_week !== status.week_start
      ? ` The most recent saved snapshot is for the week of ${status.latest_snapshot_week}.`
      : "";
    const refresh = status.enabled ? "Calendar refresh is enabled but has not saved this week yet." : "Calendar refresh is disabled.";
    return emptyState("No cached events for this week", `${refresh}${previous}`);
  }
  return events.map((event) => `<div class="event-row"><div class="event-time">${esc(event.scheduled_at_utc ? new Intl.DateTimeFormat(undefined, { timeZone: tz(), hour: "2-digit", minute: "2-digit" }).format(new Date(event.scheduled_at_utc)) : "TBD")}</div><div><div class="event-name"><i class="impact ${esc(String(event.impact || "").toLowerCase())}"></i>${esc(event.event_name)}</div><div class="event-detail">${esc(event.currency || "—")} · ${esc(event.freshness || "cached")} · ${esc(event.source || "")}</div></div></div>`).join("");
}

export function newsMarkup(limit = 3) {
  const news = state.data?.news ?? [];
  if (!news.length) return emptyState("News feed not connected", "No news provider is configured. Headlines will appear here when an authorized source is connected.");
  return news.slice(0, limit).map((entry) => {
    const item = Array.isArray(entry.payload) ? entry.payload[0] : entry.payload;
    const title = item?.title || item?.headline;
    if (!title) return "";
    return `<div class="news-row"><div class="event-time">${esc(entry.provider || "SOURCE")}</div><div><div class="event-name">${esc(title)}</div><div class="event-detail">${esc(entry.updated_at_utc || "Cached item")}</div></div></div>`;
  }).join("") || emptyState("No headlines available", "Saved news cache has no displayable headlines.");
}

export function healthMarkup() {
  const all = Object.values(services());
  if (!all.length) return emptyState("Service status unavailable", "Backend has not provided service health.");
  const real = all.filter((service) => service.implemented !== false);
  const planned = all.filter((service) => service.implemented === false);
  return `<div class="health-list">${real.map((service) => {
    const stateName = service.state || "unknown";
    const [label] = stateLabel(stateName);
    const title = service.message || service.error || service.monitor?.error || service.refresh?.error || "";
    return `<div class="health-row" title="${esc(title)}"><span>${esc(service.name.replaceAll("_", " "))}</span><span class="health-state ${esc(stateName)}">${esc(label)}</span></div>`;
  }).join("")}</div>${planned.length ? `<div class="settings-note">Planned, not yet implemented: ${esc(planned.map((service) => service.name.replaceAll("_", " ")).join(", "))}.</div>` : ""}`;
}

export function statusStrip(items) {
  return `<div class="stats-strip status-strip">${items.map(([label, value, empty]) => stat(label, value, empty)).join("")}</div>`;
}

export function calendarStatusPanel() {
  const calendar = calendarStatus();
  return statusStrip([
    ["Freshness", freshnessLabel(calendar.freshness)[0], calendar.freshness !== "fresh"],
    ["Refresh", stateLabel(calendar.state)[0], calendar.state !== "running"],
    ["Snapshot retrieved", calendar.snapshot_retrieved_at_utc ? stamp(calendar.snapshot_retrieved_at_utc) : "—", !calendar.snapshot_retrieved_at_utc],
    ["Week starting", calendar.week_start || "—", !calendar.week_start],
    ["Source", calendar.snapshot_source || "—", !calendar.snapshot_source],
  ]);
}
