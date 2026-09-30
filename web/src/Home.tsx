import { useCallback, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { getHeadlines, type Headlines } from "./api";
import { recall, remember } from "./lastSeen";
import { updatedAt } from "./Scoreboard";
import { usePull } from "./usePull";

const looksLikeHeadlines = (v: unknown) => !!v && Array.isArray((v as NonNullable<Headlines>).items);

/** Screen A (Phase 4, kept minimal): the latest AI headline set, 5-8 lines, written twice a day by the worker.
 * Before the first set exists, home is the NFL board, as it was. */
export function Home() {
  const [h, setH] = useState<Headlines | undefined>(() => recall<NonNullable<Headlines>>("headlines", looksLikeHeadlines) ?? undefined);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const fresh = await getHeadlines();
      if (fresh) remember("headlines", fresh);
      setH(fresh);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const { handlers, indicator } = usePull(load, loading);

  if (h === null) return <Navigate to="/scores/nfl" replace />;
  return (
    <div className="page" {...handlers}>
      {indicator}
      <header className="top">
        <span />
        <h1>Headlines</h1>
        <button className="refresh" onClick={() => load()} disabled={loading} aria-label="Refresh">
          {loading ? "…" : "↻"}
        </button>
      </header>
      {err && <div className="banner error">Couldn't load: {err}</div>}
      <main className="list">
        {h ? (
          <>
            <ul className="headlines">
              {h.items.map((item) => (
                <li key={item.text}>
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noreferrer">{item.text}</a>
                  ) : (
                    item.text
                  )}
                </li>
              ))}
            </ul>
            <p className="muted center small">Updated {updatedAt(h.updated_at)}</p>
          </>
        ) : (
          <p className="muted center small">Loading headlines…</p>
        )}
      </main>
    </div>
  );
}
