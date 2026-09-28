-- C1 league ranks: the per-team season cache now holds ranks as well as the INTs leader, so
-- team_season_leaders (003) becomes team_season_stats with a general `data` column.
-- Idempotent, including when 003 runs again after this file (a re-run of 003 recreates an empty
-- team_season_leaders next to team_season_stats: drop it rather than rename onto the existing table).
DO $$
BEGIN
    IF to_regclass('team_season_leaders') IS NOT NULL THEN
        IF to_regclass('team_season_stats') IS NULL THEN
            ALTER TABLE team_season_leaders RENAME TO team_season_stats;
        ELSE
            DROP TABLE team_season_leaders;
        END IF;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name = 'team_season_stats' AND column_name = 'leaders') THEN
        ALTER TABLE team_season_stats RENAME COLUMN leaders TO data;
    END IF;
END $$;

-- data: {"interceptions": {name, last_name, position, value, tied} | null,
--        "ranks": {"<summary stat key>": {"value": 31.0, "rank": "Tied-4th"}}}
-- Rows cached before this migration have no "ranks"; clearing them makes the next C1 open refetch.
DELETE FROM team_season_stats WHERE NOT (data ? 'ranks');
