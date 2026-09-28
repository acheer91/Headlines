# Scores App

NFL scoreboard you install on your phone's home screen. Pull to refresh, no alerts.

## Run it (your laptop)

Needs Docker Desktop.

```bash
cp .env.example .env               # set POSTGRES_PASSWORD
docker compose up -d --build
./scripts/validate_phase1.sh       # automated checks
```

Open http://localhost:8000 on the laptop to check it.

## Put it on your phone (Tailscale)

Android only offers "Install app" for pages served over HTTPS. Tailscale gives your laptop a private HTTPS name.

1. Install Tailscale on the laptop and the phone, same account.
2. In the Tailscale admin console, under DNS: turn on MagicDNS and HTTPS certificates.
3. On the laptop: `tailscale serve --bg 8000`
4. It prints `https://<laptop-name>.<tailnet>.ts.net`. Open that in Chrome on the phone.
5. Chrome menu → **Install app** (or **Add to home screen**).

Nothing is exposed to the public internet; only devices on your tailnet can reach it.

## Game screens and bets (Phase 2)

Tap a game: before kickoff you get the pre-game page (records, line, season stats and leaders, who's out),
during the game the live page (score by quarter, team stats, where each bet stands "so far"), after the
final the post-game page (bets graded: moneyline, spread, over/under). Pull down to refresh any of them.

```bash
./scripts/validate_phase2.sh                                   # automated checks
docker compose exec api python -m app.grade_week --week 2      # grade a week, print a table to hand-check
```

Bets are graded against the last line saved before kickoff; if the app never saw one, ESPN's closing line
(labelled on screen). Results are reported, never advice.

## Favorites

Edit `config/favorites.json`, e.g. `{"nfl": ["SEA", "KC"]}`. No rebuild needed; next pull picks it up.

## What's here

See `CLAUDE.md` for the working brief, layout, and rules.
