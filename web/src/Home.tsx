import { useCallback, useEffect, useState } from "react";
import { Navigate } from "react-router-dom";
import { getHeadlines, getWeekend, type Headlines, type WeekendColumn } from "./api";
import { recall, remember } from "./lastSeen";
import { updatedAt } from "./Scoreboard";
import { usePull } from "./usePull";

const looksLikeHeadlines = (v: unknown) => !!v && Array.isArray((v as NonNullable<Headlines>).items);
const looksLikeColumns = (v: unknown) => Array.isArray(v);
const LEAGUE_LABEL: Record<string, string> = { nfl: "NFL", ncaaf: "NCAAF" };

const leagueOf = (url: string | null) =>
  url?.includes("/nfl/") ? "NFL" : url?.includes("/college-football/") ? "CFB" : null;
// "https://www.espn.com/..." -> "ESPN"
const sourceOf = (url: string | null) => {
  try {
    return url ? new URL(url).hostname.replace(/^www\./, "").split(".")[0] : null;
  } catch {
    return null;
  }
};

// Headlines are news only (Adam, 2026-10-06): the scores live on the sport tabs. The worker still feeds finals to the
// headline writer, so a final-score line is dropped here. The writer phrases one several ways: "Chiefs defeated Raiders
// 30-27.", "Ravens lost to Titans 24-18.", "... 45-24 on Monday Night Football.", and the older "Steelers 24, Browns 27
// (final)" / "NFL final: Steelers 24, Browns 27". A news sentence that merely mentions a score ("... 24-10 behind 300
// yards from ...") is longer than a bare result, so it does not match and stays.
const TEAMS = String.raw`(.{2,40}?)`;
const SCORE = String.raw`\s+\d{1,3}\s*[-–]\s*\d{1,3}\b`;
const BEAT = new RegExp(String.raw`^${TEAMS}\s+(?:defeated|beat|beats|edged|topped|downed|outlasted|outscored|routed|blanked|stunned|survived|held off|escaped|rolled past|ran past|got past|nipped|upset|lost to|fell to|fell at|dropped one to|were beaten by|was beaten by)\s+${TEAMS}${SCORE}(.*)$`, "i");
const OLD = /^(.{2,40}?)\s+\d{1,3},\s+(.{2,40}?)\s+\d{1,3}$/;
const FINAL_SUFFIX = /\s*\(final\)\.?\s*$/i;
const FINAL_PREFIX = /^\s*(?:nfl|ncaaf|cfb)?\s*final:\s*/i;
const NAME = /^[^\d]+$/; // a team name has no digits

function isFinalScore(text: string) {
  const t = text.trim().replace(FINAL_PREFIX, "").replace(FINAL_SUFFIX, "").replace(/^\s*(?:nfl|ncaaf|cfb):\s*/i, "").replace(/\.\s*$/, "");
  const m = BEAT.exec(t);
  if (m) return NAME.test(m[1]) && NAME.test(m[2]) && m[3].replace(/^[\s,.;:-]+|[\s.]+$/g, "").replace(/^on\s+/i, "").length <= 32;
  const o = OLD.exec(t);
  return !!o && NAME.test(o[1]) && NAME.test(o[2]);
}

function Column({ c }: { c: WeekendColumn }) {
  return (
    <article className="column">
      <div className="kicker">
        <span>{LEAGUE_LABEL[c.league] ?? c.league.toUpperCase()} · The weekend</span>
      </div>
      <h2 className="column-title">{c.title}</h2>
      {c.paragraphs.map((p) => (
        <p key={p} className="column-text">
          {p}
        </p>
      ))}
      <p className="column-foot">Written by AI · {updatedAt(c.written_at)}</p>
    </article>
  );
}

/** Screen A (Phase 4, kept minimal): the weekend columns, then the latest AI headline set, news only. Before the first set exists, home is the
 * NFL board, as it was. */
export function Home() {
  const [h, setH] = useState<Headlines | undefined>(() => recall<NonNullable<Headlines>>("headlines", looksLikeHeadlines) ?? undefined);
  const [cols, setCols] = useState<WeekendColumn[]>(() => recall<WeekendColumn[]>("weekend", looksLikeColumns) ?? []);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      // The columns are a bonus: if they can't be had, the headlines still show (and the last columns stay).
      const [fresh, weekend] = await Promise.all([getHeadlines(), getWeekend().catch(() => null)]);
      if (weekend) {
        remember("weekend", weekend);
        setCols(weekend);
      }
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

  const news = (h?.items ?? []).filter((item) => !isFinalScore(item.text));

  return (
    <div className="page" {...handlers}>
      {indicator}
      <header className="top">
        <h1>Headlines</h1>
        <button className="refresh" onClick={() => load()} disabled={loading} aria-label="Refresh">
          {loading ? "…" : "↻"}
        </button>
      </header>
      {err && <div className="banner error">Couldn't load: {err}</div>}
      <main className="list feed-page">
        {h ? (
          <>
            {cols.map((c) => (
              <Column key={c.league} c={c} />
            ))}
            {cols.length > 0 && news.length > 0 && <h2 className="feed-label">News</h2>}
            {news.length > 0 ? (
              <ul className="feed">
                {news.map((item) => {
                  const kicker = [leagueOf(item.url), sourceOf(item.url)].filter(Boolean).join(" · ");
                  const body = (
                    <>
                      {(kicker || item.url) && (
                        <div className="kicker">
                          <span>{kicker}</span>
                          {item.url && <span aria-hidden="true">↗</span>}
                        </div>
                      )}
                      <div className="news-text">{item.text}</div>
                    </>
                  );
                  return (
                    <li key={item.text}>
                      {item.url ? (
                        <a className="feed-row" href={item.url} target="_blank" rel="noreferrer">
                          {body}
                        </a>
                      ) : (
                        <div className="feed-row">{body}</div>
                      )}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="muted center small">No news in the latest set.</p>
            )}
            <p className="muted center small">Updated {updatedAt(h.updated_at)}</p>
          </>
        ) : (
          <p className="muted center small">Loading headlines…</p>
        )}
      </main>
    </div>
  );
}
