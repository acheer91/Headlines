import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { getAi, getGame, type AiText, type Bet, type GameCard, type GameDetail, type Injury, type Leader, type SideRow } from "./api";
import { GameCardView, LineRow, tbdDate } from "./GameCard";
import { recall, remember, touchGame } from "./lastSeen";
import { updatedAt } from "./Scoreboard";
import { usePull } from "./usePull";

const SCREEN_NAME = { C1: "Pre-game", C2: "Live", D: "Post-game" } as const;

// Kickoff is shown in Pacific time (PRD: C1 header "kickoff in PT").
const PT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  weekday: "short",
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});
const kickoffPT = (iso: string) => `${PT.format(new Date(iso))} PT`;

const looksLikeGame = (v: unknown) => {
  const g = v as GameDetail;
  return !!g && typeof g.screen === "string" && !!g.header && !!g.team_stats && !!g.leaders && !!g.home && !!g.away;
};

/** C1 / C2 / D, picked by the game's state. Opening or pulling the page refreshes it. */
export function GamePage() {
  const { id } = useParams();
  const nav = useNavigate();
  // The card that was tapped, so the page has something to show before the first byte arrives.
  const seed = (useLocation().state as { card?: GameCard } | null)?.card ?? null;
  const [g, setG] = useState<GameDetail | null>(() => recall<GameDetail>(`game:${id}`, looksLikeGame));
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // AI text loads after the game (stats show at once); null while it's being fetched or written.
  const [ai, setAi] = useState<AiText | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const shownId = useRef(id);
  shownId.current = id;
  const loadingId = useRef<string | undefined | null>(null);

  const load = useCallback(async () => {
    if (loadingId.current === id) return; // mount + foreground can ask twice
    loadingId.current = id;
    setLoading(true);
    setErr(null);
    try {
      const fresh = await getGame(Number(id));
      remember(`game:${id}`, fresh);
      touchGame(Number(id));
      if (shownId.current === id) setG(fresh);
      // Once per open or pull (never on a timer): the server returns stored text or writes it now.
      setAiLoading(true);
      getAi(Number(id))
        .then((a) => shownId.current === id && setAi(a))
        .catch(() => shownId.current === id && setAi(null))
        .finally(() => shownId.current === id && setAiLoading(false));
    } catch (e) {
      if (shownId.current === id) setErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (loadingId.current === id) {
        loadingId.current = null;
        setLoading(false);
      }
    }
  }, [id]);

  // Last-seen version of this game (if any) is already on screen; pull fresh data over it.
  useEffect(() => {
    setG(recall<GameDetail>(`game:${id}`, looksLikeGame));
    setAi(null);
    load();
  }, [load, id]);

  const { handlers, indicator } = usePull(load, loading);

  return (
    <div className="page" {...handlers}>
      {indicator}
      <header className="top">
        <button
          className="back"
          // Opened directly (shared link, restored app): there's no in-app page to go back to.
          onClick={() => ((window.history.state?.idx ?? 0) > 0 ? nav(-1) : nav(`/scores/${g?.league ?? seed?.league ?? "nfl"}`))}
          aria-label="Back"
        >
          ‹
        </button>
        <h1>{g ? SCREEN_NAME[g.screen] : "Game"}</h1>
        <button className="refresh" onClick={() => load()} disabled={loading} aria-label="Refresh">
          {loading ? "…" : "↻"}
        </button>
      </header>
      {g?.stale && (
        <div className="banner">
          {g.summary_updated_at
            ? `Showing saved data. ESPN didn't answer; updated ${updatedAt(g.summary_updated_at)}.`
            : "ESPN didn't answer and nothing is saved for this game yet. Pull to try again."}
        </div>
      )}
      {err && <div className="banner error">Couldn't load: {err}</div>}
      {g?.summary_behind && (
        <div className="banner">ESPN's box score hasn't caught up with this game yet. Pull again in a few seconds.</div>
      )}
      {g ? (
        <>
          <Detail g={g} ai={ai} aiLoading={aiLoading} />
          {g.summary_updated_at && (
            <p className="muted center small">
              {loading ? "Updating… " : ""}Game details updated {updatedAt(g.summary_updated_at)}
            </p>
          )}
        </>
      ) : (
        seed && (
          <main className="list">
            <GameCardView g={seed} onOpen={() => {}} />
            <p className="muted center small">Loading details…</p>
          </main>
        )
      )}
    </div>
  );
}

function Detail({ g, ai, aiLoading }: { g: GameDetail; ai: AiText | null; aiLoading: boolean }) {
  const noData = g.summary_available ? "ESPN didn't send this" : "Not available yet";
  // Live: the AI one-liner when it's ready, else ESPN's own line (the PRD's template fallback).
  const aiLine = ai?.kind === "one_liner" && ai.status === "ready" && ai.body && "line" in ai.body ? ai.body.line : null;
  const oneLiner = aiLine ?? g.one_liner;
  return (
    <main className="list">
      <Matchup g={g} />
      {g.screen === "C2" && oneLiner && <p className="oneliner">{oneLiner}</p>}
      {g.screen !== "C1" && <Linescore g={g} />}

      {g.screen === "C1" && (
        <Section title="Line">
          {g.line ? <LineRow g={g} /> : <p className="muted empty">No line yet</p>}
        </Section>
      )}
      {g.screen === "C2" && <Bets title="Bet status · so far" bets={g.bets} />}
      {g.screen === "D" && <Bets title="Bets" bets={g.bets} />}

      <Section title={g.team_stats.kind === "season" ? "Team stats · per game this season" : "Team stats"}>
        {g.team_stats.rows.length ? <StatsTable g={g} rows={g.team_stats.rows} /> : <p className="muted empty">{noData}</p>}
      </Section>
      <Section title={g.leaders.kind === "season" ? "Season leaders" : "Game leaders"}>
        {g.leaders.rows.length ? <Leaders g={g} rows={g.leaders.rows} /> : <p className="muted empty">{noData}</p>}
      </Section>
      {g.screen === "C1" && (
        <Section title="Who's out">
          {g.injuries ? <Injuries g={g} /> : <p className="muted empty">{noData}</p>}
        </Section>
      )}

      {g.screen === "C1" && <PreviewSections g={g} ai={ai} loading={aiLoading} />}
      {g.screen === "D" && g.completed !== false && <RecapSections g={g} ai={ai} loading={aiLoading} />}
    </main>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="section">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function Matchup({ g }: { g: GameDetail }) {
  const status =
    g.screen === "C1"
      ? g.time_valid
        ? kickoffPT(g.start_time)
        : tbdDate(g.start_time)
      : g.screen === "C2"
        ? g.status_detail ?? "Live"
        : g.status_detail ?? "Final";
  const meta = [g.screen === "C1" ? g.venue : null, g.broadcast].filter(Boolean).join(" · ");
  return (
    <section className={`section matchup ${g.state}`}>
      <div className="card-head">
        <span className={`status ${g.state}`}>
          {g.state === "in" && <span className="dot" />}
          {status}
        </span>
        <span className="meta">{meta}</span>
      </div>
      {(["away", "home"] as const).map((side) => {
        const t = g[side];
        const h = g.header[side];
        const other = g[side === "home" ? "away" : "home"];
        const winning = g.state !== "pre" && t.score != null && other.score != null && t.score > other.score;
        return (
          <div key={side} className={`team ${g.state === "post" && !winning ? "dim" : ""}`}>
            {t.logo ? <img src={t.logo} alt="" width={28} height={28} /> : <span className="logo-ph" />}
            <span className="name">
              {t.short ?? t.name}
              {g.state === "in" && h.possession && <span className="ball" title="Has the ball"> ●</span>}
            </span>
            <span className="abbr">
              {h.record ?? ""}
              {h.venue_record ? ` · ${h.venue_record} ${side === "home" ? "home" : "away"}` : ""}
            </span>
            <span className={`score ${winning ? "lead" : ""}`}>{g.state === "pre" ? "" : t.score ?? ""}</span>
          </div>
        );
      })}
      {g.screen === "C2" && g.situation?.down_distance && (
        <div className="situation">
          {g.situation.possession ? `${g.situation.possession} ball · ` : ""}
          {g.situation.down_distance}
        </div>
      )}
    </section>
  );
}

function periodLabel(i: number) {
  return i < 4 ? `${i + 1}` : i === 4 ? "OT" : `OT${i - 3}`;
}

function Linescore({ g }: { g: GameDetail }) {
  const n = Math.max(g.header.home.linescores.length, g.header.away.linescores.length);
  if (!n) {
    return (
      <Section title="Score by quarter">
        <p className="muted empty">{g.summary_available ? "ESPN didn't send this" : "Not available yet"}</p>
      </Section>
    );
  }
  const cols = Array.from({ length: Math.max(n, 4) }, (_, i) => i);
  return (
    <Section title="Score by quarter">
      <table className="grid linescore">
        <thead>
          <tr>
            <th />
            {cols.map((i) => (
              <th key={i}>{periodLabel(i)}</th>
            ))}
            <th>T</th>
          </tr>
        </thead>
        <tbody>
          {(["away", "home"] as const).map((side) => (
            <tr key={side}>
              <th>{g[side].abbr}</th>
              {cols.map((i) => (
                <td key={i}>{g.header[side].linescores[i] ?? "–"}</td>
              ))}
              <td className="total">{g[side].score ?? "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function Bets({ title, bets }: { title: string; bets: Bet[] | null }) {
  return (
    <Section title={title}>
      {(bets ?? []).map((b) => (
        <div key={b.market} className={`bet ${b.status}`}>
          <span className="bet-label">{b.label}</span>
          <span className="bet-text">{b.text}</span>
          {b.line && (
            <span className="bet-line muted">
              {b.line}
              {/* ESPN's closing line is the normal case; say so only when a game was graded on something else */}
              {b.line_source && b.line_source !== "espn_close" ? ` · ${b.line_source_label}` : ""}
            </span>
          )}
        </div>
      ))}
      {bets?.some((b) => b.provider) && (
        <p className="muted small">Line: {bets.find((b) => b.provider)?.provider}. Reported, not advice.</p>
      )}
    </Section>
  );
}

// ESPN's "Tied-4th" -> "T-4th" so it fits under the number on a phone.
const shortRank = (r: string) => r.replace(/^Tied-/, "T-");

function StatsTable({ g, rows }: { g: GameDetail; rows: SideRow<string>[] }) {
  return (
    <table className="grid stats">
      <thead>
        <tr>
          <th>{g.away.abbr}</th>
          <th />
          <th>{g.home.abbr}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <td>
              {r.away ?? "–"}
              {r.away_rank && <span className="rank">{shortRank(r.away_rank)}</span>}
            </td>
            <th>{r.label}</th>
            <td>
              {r.home ?? "–"}
              {r.home_rank && <span className="rank">{shortRank(r.home_rank)}</span>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LeaderCell({ l }: { l: Leader | null }) {
  if (!l) return <div className="leader muted">–</div>;
  if (!l.name) return <div className="leader muted small">{l.value}</div>;
  return (
    <div className="leader">
      <div className="leader-name">
        {l.name}
        {l.tied ? <span className="muted small"> +{l.tied} tied</span> : null}
      </div>
      <div className="muted small">{l.value}</div>
    </div>
  );
}

function Leaders({ g, rows }: { g: GameDetail; rows: SideRow<Leader>[] }) {
  return (
    <div className="leaders">
      {rows.map((r) => (
        <div key={r.key} className="leader-row">
          <div className="leader-label muted small">{r.label}</div>
          <div className="leader-pair">
            <LeaderCell l={r.away} />
            <LeaderCell l={r.home} />
          </div>
        </div>
      ))}
      <div className="leader-pair muted small">
        <div>{g.away.abbr}</div>
        <div>{g.home.abbr}</div>
      </div>
    </div>
  );
}

// ESPN sends a plain date ("2026-10-04"); show it as "Oct 4" without shifting it by time zone.
const returnDate = (d: string) => {
  const [y, m, day] = d.split("-").map(Number);
  return new Date(y, m - 1, day).toLocaleDateString("en-US", { month: "short", day: "numeric" });
};

function InjuryList({ items }: { items: Injury[] }) {
  if (!items.length) return <p className="muted empty">None reported</p>;
  return (
    <ul className="injuries">
      {items.map((i) => (
        <li key={`${i.name}-${i.status}`}>
          <span>
            {i.name}
            {i.position && <span className="muted small"> {i.position}</span>}
          </span>
          <span className={`inj-status ${(i.status ?? "").toLowerCase().replace(/\s+/g, "-")}`}>
            {i.status ?? "–"}
            {i.detail && <span className="muted small"> · {i.detail}</span>}
            {i.return_date && <span className="muted small inj-return">est. return {returnDate(i.return_date)}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Injuries({ g }: { g: GameDetail }) {
  return (
    <div className="inj-teams">
      {(["away", "home"] as const).map((side) => (
        <div key={side}>
          <h3>{g[side].name}</h3>
          <InjuryList items={g.injuries?.[side] ?? []} />
        </div>
      ))}
    </div>
  );
}

// ---------- Phase 4: AI text ----------

const readyBody = <K extends string>(ai: AiText | null, kind: AiText["kind"], key: K) =>
  ai && ai.kind === kind && ai.status === "ready" && ai.body && key in ai.body ? (ai.body as Record<K, unknown>) : null;

const shortDate = (iso: string) => new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric" });

/** C1: the preview, edges per team with their sources, and writers' picks, attributed and linked. */
function PreviewSections({ g, ai, loading }: { g: GameDetail; ai: AiText | null; loading: boolean }) {
  const body = readyBody(ai, "preview", "preview") as
    | { preview: string; edges: { home: { text: string; url: string; outlet: string }[]; away: { text: string; url: string; outlet: string }[] }; picks: { writer: string; outlet: string; pick: string; url: string }[] }
    | null;
  const waiting = loading && !body;
  const fallback = ai?.status === "no_sources" ? "No fresh previews" : "Preview unavailable right now.";
  return (
    <>
      <Section title="Preview">
        {body ? <p className="ai-text">{body.preview}</p> : <p className="muted empty">{waiting ? "Writing the preview…" : fallback}</p>}
        {body && ai?.sources && ai.sources.length > 0 && (
          <p className="muted small sources">
            From{" "}
            {ai.sources.map((s, i) => (
              <span key={s.url}>
                {i > 0 && ", "}
                <a href={s.url} target="_blank" rel="noreferrer">{s.outlet}</a> ({shortDate(s.published)})
              </span>
            ))}
          </p>
        )}
      </Section>
      {body && (body.edges.away.length > 0 || body.edges.home.length > 0) && (
        <Section title="Edges">
          {(["away", "home"] as const).map((side) =>
            body.edges[side].length ? (
              <div key={side} className="edges">
                <h3>{g[side].short ?? g[side].name}</h3>
                <ul>
                  {body.edges[side].map((e) => (
                    <li key={e.text}>
                      {e.text} <a href={e.url} target="_blank" rel="noreferrer" className="muted small">{e.outlet}</a>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null,
          )}
        </Section>
      )}
      {body && (
        <Section title="Writers' picks">
          {body.picks.length ? (
            <ul className="picks">
              {body.picks.map((p) => (
                <li key={p.url + p.writer}>
                  {p.outlet}'s {p.writer} picks {p.pick}{" "}
                  <a href={p.url} target="_blank" rel="noreferrer" className="muted small">link</a>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted empty">None in these articles</p>
          )}
        </Section>
      )}
    </>
  );
}

/** D: the recap and a short summary per team; the stats-only text when there's no AI text. */
function RecapSections({ g, ai, loading }: { g: GameDetail; ai: AiText | null; loading: boolean }) {
  const body = readyBody(ai, "recap", "recap") as { recap: string; bets: string; home: string; away: string } | null;
  const bets = (g.bets ?? []).filter((b) => b.status === "graded").map((b) => `${b.label}: ${b.text}.`).join(" ");
  const fallback = `Final: ${g.home.short ?? g.home.name} ${g.home.score ?? ""}, ${g.away.short ?? g.away.name} ${g.away.score ?? ""}.`;
  return (
    <>
      <Section title="Recap">
        {body ? (
          <>
            <p className="ai-text">{body.recap}</p>
            {body.bets && <p className="muted small">{body.bets}</p>}
          </>
        ) : loading ? (
          <p className="muted empty">Writing the recap…</p>
        ) : (
          <p className="empty">{fallback} {bets}</p>
        )}
      </Section>
      {body && (
        <Section title="Team summaries">
          {(["away", "home"] as const).map((side) => (
            <p key={side} className="ai-text">
              <strong>{g[side].short ?? g[side].name}:</strong> {body[side]}
            </p>
          ))}
        </Section>
      )}
    </>
  );
}
