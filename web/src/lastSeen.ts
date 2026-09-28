/**
 * The last response we showed, kept in this browser so the next open paints instantly while the
 * pull fetches fresh data. Per-device convenience only: storage can be blocked or cleared, so
 * every read and write is guarded and the app works the same without it (just without the instant view).
 */
// Bump VERSION whenever an API response changes shape: copies saved by an older build are then ignored
// instead of being rendered by code that doesn't expect them.
const VERSION = 2;
const PREFIX = `scores:last:v${VERSION}:`;
const MAX_GAMES = 30;

// Clear copies saved by older versions (they're never read again).
try {
  for (const k of Object.keys(localStorage)) if (k.startsWith("scores:last:") && !k.startsWith(PREFIX)) localStorage.removeItem(k);
} catch {
  /* storage blocked */
}

/** The saved copy, or null if missing, unreadable, or not shaped like `looksRight` expects. */
export function recall<T>(key: string, looksRight: (v: unknown) => boolean = () => true): T | null {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    const v = raw ? JSON.parse(raw) : null;
    return v && looksRight(v) ? (v as T) : null;
  } catch {
    return null;
  }
}

/** Drop every saved copy (all versions): used when a screen fails to render. */
export function forgetAll() {
  try {
    for (const k of Object.keys(localStorage)) if (k.startsWith("scores:last:")) localStorage.removeItem(k);
  } catch {
    /* ignore */
  }
}

export function remember(key: string, value: unknown): void {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
    if (key.startsWith("game:")) prune();
  } catch {
    /* storage full or blocked: skip */
  }
}

/** Keep only the most recent game pages. */
function prune() {
  try {
    const order: string[] = JSON.parse(localStorage.getItem(PREFIX + "game-order") ?? "[]");
    const keys = Object.keys(localStorage).filter((k) => k.startsWith(PREFIX + "game:"));
    const ranked = [...new Set([...order.filter((k) => keys.includes(k)), ...keys])];
    for (const k of ranked.slice(0, Math.max(0, ranked.length - MAX_GAMES))) localStorage.removeItem(k);
    localStorage.setItem(PREFIX + "game-order", JSON.stringify(ranked.slice(-MAX_GAMES)));
  } catch {
    /* ignore */
  }
}

export function touchGame(id: number) {
  try {
    const key = PREFIX + `game:${id}`;
    const order: string[] = JSON.parse(localStorage.getItem(PREFIX + "game-order") ?? "[]");
    localStorage.setItem(PREFIX + "game-order", JSON.stringify([...order.filter((k) => k !== key), key]));
  } catch {
    /* ignore */
  }
}
