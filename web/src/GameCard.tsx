import type { GameCard as Game, Line } from "./api";

const fmtSigned = (n: number) => (n > 0 ? `+${n}` : n === 0 ? "PK" : `${n}`);
const fmtMl = (n: number | null) => (n == null ? "–" : n > 0 ? `+${n}` : `${n}`);

// A flexed game's placeholder time is midnight Eastern on the game's date, so read the date in ET.
const ET_DATE = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric" });
export const tbdDate = (iso: string) => `${ET_DATE.format(new Date(iso))} · time TBD`;

export function kickoff(iso: string, timeValid = true) {
  if (!timeValid) return tbdDate(iso);
  const d = new Date(iso);
  const day = d.toLocaleDateString(undefined, { weekday: "short" });
  const time = d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `${day} ${time}`;
}

function spreadText(line: Line, g: Game) {
  if (line.home_spread == null) return line.details ?? null;
  if (line.home_spread === 0) return "PK";
  // Show the favorite, the way books do: "SF -2.5"
  return line.home_spread < 0
    ? `${g.home.abbr} ${fmtSigned(line.home_spread)}`
    : `${g.away.abbr} ${fmtSigned(-line.home_spread)}`;
}

export function LineRow({ g }: { g: Game }) {
  const line = g.line;
  if (!line) return <div className="line muted">No line</div>;
  const spread = spreadText(line, g);
  return (
    <div className="line">
      {spread && <span>{spread}</span>}
      {line.total != null && <span>O/U {line.total}</span>}
      {(line.home_ml != null || line.away_ml != null) && (
        <span>
          ML {g.away.abbr} {fmtMl(line.away_ml)} · {g.home.abbr} {fmtMl(line.home_ml)}
        </span>
      )}
      <span className="provider">
        {line.provider ?? "Line"}
        {line.captured_after_kickoff ? " · in-game" : ""}
      </span>
    </div>
  );
}

function TeamRow({ side, g, isHome }: { side: Game["home"]; g: Game; isHome: boolean }) {
  const other = isHome ? g.away : g.home;
  const winning = g.state !== "pre" && side.score != null && other.score != null && side.score > other.score;
  const losing = g.state === "post" && side.score != null && other.score != null && side.score < other.score;
  return (
    <div className={`team ${losing ? "dim" : ""}`}>
      {side.logo ? (
        <img
          src={side.logo}
          alt=""
          width={28}
          height={28}
          loading="lazy"
          onError={(e) => (e.currentTarget.style.visibility = "hidden")}
        />
      ) : (
        <span className="logo-ph" />
      )}
      <span className="name">{side.short ?? side.name}</span>
      <span className="abbr">{side.abbr}</span>
      <span className={`score ${winning ? "lead" : ""}`}>{g.state === "pre" ? "" : side.score ?? ""}</span>
    </div>
  );
}

export function GameCardView({ g, onOpen }: { g: Game; onOpen: () => void }) {
  const status =
    g.state === "pre" ? kickoff(g.start_time, g.time_valid) : g.state === "in" ? g.status_detail ?? "Live" : g.status_detail ?? "Final";
  return (
    <button className={`card ${g.state}`} onClick={onOpen}>
      <div className="card-head">
        <span className={`status ${g.state}`}>
          {g.state === "in" && <span className="dot" />}
          {status}
        </span>
        <span className="meta">
          {g.favorite && <span className="fav">★</span>}
          {g.broadcast}
        </span>
      </div>
      <TeamRow side={g.away} g={g} isHome={false} />
      <TeamRow side={g.home} g={g} isHome={true} />
      <LineRow g={g} />
    </button>
  );
}
