import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getScoreboard, type Scoreboard as SB } from "./api";
import { GameCardView } from "./GameCard";
import { recall, remember } from "./lastSeen";
import { usePull } from "./usePull";

/** "6:10:25 PM" today; "Sun 6:10 PM" for older saved data, so a days-old board doesn't read as today. */
export function updatedAt(iso: string) {
  const d = new Date(iso);
  if (d.toDateString() === new Date().toDateString()) {
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit", second: "2-digit" });
  }
  return d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
}

export function Scoreboard() {
  const { league = "nfl" } = useParams();
  const nav = useNavigate();
  const [data, setData] = useState<SB | null>(null);
  // The browsed stage lives in the URL (?st=3&week=1), so opening a game and coming back keeps it.
  // Preseason, regular season and postseason each start at week 1, so the season type is part of it.
  const [params, setParams] = useSearchParams();
  const week = Number(params.get("week")) || undefined;
  const st = Number(params.get("st")) || undefined;
  const go = (s: { season_type: number; week: number }) =>
    setParams({ st: String(s.season_type), week: String(s.week) }, { replace: true });
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const key = `sb:${league}:${st ?? "cur"}:${week ?? "current"}`;
  // Which board is on screen right now, and which one is being fetched. Opening the app can ask
  // twice for the same board (mount + foreground): skip the repeat. Switching weeks mid-load must
  // still fetch the new week, and a late answer for a week you've left must not overwrite the screen.
  const shownKey = useRef(key);
  shownKey.current = key;
  const loadingKey = useRef<string | null>(null);

  const load = useCallback(
    async (force = false) => {
      if (loadingKey.current === key && !force) return;
      loadingKey.current = key;
      setLoading(true);
      setErr(null);
      try {
        const fresh = await getScoreboard(league, week, st, force);
        remember(key, fresh);
        if (shownKey.current === key) setData(fresh);
      } catch (e) {
        if (shownKey.current === key) setErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (loadingKey.current === key) {
          loadingKey.current = null;
          setLoading(false);
        }
      }
    },
    [league, week, st, key],
  );

  // Paint the last scoreboard we showed right away, then pull fresh data over it.
  useEffect(() => {
    setData(recall<SB>(key, (v) => Array.isArray((v as SB).games)));
    load();
  }, [load, key]);

  const { handlers, indicator } = usePull(load, loading);

  // Prev and Next walk ESPN's season calendar (weeks, then e.g. Bowls and CFP), across season types.
  const cal = data?.calendar ?? [];
  const i = cal.findIndex((s) => s.season_type === (st ?? data?.season_type) && s.week === (week ?? data?.week));
  const label = i >= 0 ? cal[i].label : `Week ${week ?? data?.week ?? "–"}`;
  const updated = data?.updated_at ? updatedAt(data.updated_at) : null;

  return (
    <div className="page" {...handlers}>
      {indicator}
      <header className="top">
        <h1>{league.toUpperCase()}</h1>
        <div className="week">
          <button aria-label="Previous week" disabled={i <= 0} onClick={() => go(cal[i - 1])}>
            ‹
          </button>
          <span>{label}</span>
          <button aria-label="Next week" disabled={i < 0 || i >= cal.length - 1} onClick={() => go(cal[i + 1])}>
            ›
          </button>
        </div>
        <button className="refresh" onClick={() => load(true)} disabled={loading} aria-label="Refresh">
          {loading ? "…" : "↻"}
        </button>
      </header>

      {data?.stale && <div className="banner">Showing saved scores: couldn't get fresh data from ESPN. Updated {updated}.</div>}
      {data?.warning && !data.stale && <div className="banner">{data.warning}</div>}
      {err && <div className="banner error">Couldn't load: {err}</div>}

      <main className="list">
        {data?.games.map((g) => (
          <GameCardView key={g.id} g={g} onOpen={() => nav(`/game/${g.id}`, { state: { card: g } })} />
        ))}
        {data && data.games.length === 0 && <p className="muted center">No games this week.</p>}
      </main>
      {updated && !data?.stale && <p className="muted center small">Updated {updated}</p>}
    </div>
  );
}
