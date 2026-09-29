export type TeamSide = {
  abbr: string;
  name: string;
  short: string | null;
  logo: string | null;
  color: string | null;
  score: number | null;
  /** AP / CFP rank 1-25 (NCAAF); null = unranked */
  rank: number | null;
};

export type Line = {
  provider: string | null;
  details: string | null;
  home_spread: number | null;
  total: number | null;
  home_ml: number | null;
  away_ml: number | null;
  captured_after_kickoff: boolean;
};

export type GameCard = {
  id: number;
  league: string;
  state: "pre" | "in" | "post";
  start_time: string;
  /** false: a flexed game with no kickoff set yet; start_time is ESPN's placeholder, show the date only */
  time_valid: boolean;
  /** false: canceled or postponed (state "post" but never played) */
  completed: boolean | null;
  status_detail: string | null;
  period: number | null;
  clock: string | null;
  broadcast: string | null;
  venue: string | null;
  /** stored for NCAAF; nothing displays it yet */
  neutral_site: boolean;
  favorite: boolean;
  home: TeamSide;
  away: TeamSide;
  line: Line | null;
  screen?: "C1" | "C2" | "D";
};

export type Scoreboard = {
  league: string;
  season: number | null;
  week: number | null;
  /** 1 preseason, 2 regular season, 3 postseason */
  season_type: number | null;
  /** ESPN's season stages in order (weeks, then e.g. Bowls and CFP); Prev and Next walk it */
  calendar: { season_type: number; week: number; label: string }[];
  updated_at: string | null;
  stale: boolean;
  error: string | null;
  /** refresh worked but ESPN sent some games we couldn't read */
  warning?: string | null;
  games: GameCard[];
};

async function get<T>(url: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(url, { cache: "no-store" });
  } catch {
    throw new Error("No connection to the app. Check Wi-Fi or Tailscale, then pull to try again.");
  }
  if (!r.ok) {
    // Say what happened in words, not the raw error body.
    if (r.status === 404) throw new Error("That game or page doesn't exist.");
    if (r.status === 502) throw new Error("ESPN didn't answer and nothing is saved yet. Pull to try again.");
    throw new Error(`The server had a problem (${r.status}). Pull to try again.`);
  }
  return r.json();
}

export const getScoreboard = (league: string, week?: number, seasonType?: number, force = false) => {
  const q = new URLSearchParams();
  if (week) q.set("week", String(week));
  if (seasonType) q.set("season_type", String(seasonType));
  if (force) q.set("force", "true");
  const qs = q.toString();
  return get<Scoreboard>(`/api/scoreboard/${league}${qs ? `?${qs}` : ""}`);
};

export type SideRow<T> = {
  key: string;
  label: string;
  home: T | null;
  away: T | null;
  /** ESPN league rank for that side (C1 season stats), e.g. "Tied-4th" */
  home_rank?: string;
  away_rank?: string;
};
export type Leader = {
  name: string | null;
  last_name?: string | null;
  position?: string | null;
  value: string | null;
  /** other players on the same total (season INTs leader) */
  tied?: number;
};
export type Injury = { name: string; position: string | null; status: string | null; detail: string | null; return_date: string | null };
export type HeaderSide = { record: string | null; venue_record: string | null; linescores: (number | null)[]; possession: boolean | null };

export type Bet = {
  market: "moneyline" | "spread" | "total";
  label: string;
  status: "graded" | "so_far" | "ungraded" | "not_played";
  text: string;
  outcome?: string;
  margin?: number;
  line?: string | null;
  line_source?: "pre_game" | "espn_close" | "in_game";
  line_source_label?: string;
  provider?: string | null;
  graded_score?: string;
};

export type GameDetail = GameCard & {
  screen: "C1" | "C2" | "D";
  stale: boolean;
  error: string | null;
  summary_updated_at: string | null;
  summary_available: boolean;
  /** ESPN's summary trails the game's state; box-score blocks are withheld until it catches up */
  summary_behind?: boolean;
  header: { home: HeaderSide; away: HeaderSide };
  situation: { possession: string | null; down_distance: string | null } | null;
  team_stats: { kind: "season" | "game"; rows: SideRow<string>[] };
  leaders: { kind: "season" | "game"; rows: SideRow<Leader>[] };
  injuries: { home: Injury[]; away: Injury[] } | null;
  one_liner: string | null;
  bets: Bet[] | null;
  placeholders: string[];
};

export const getGame = (id: number) => get<GameDetail>(`/api/games/${id}`);
