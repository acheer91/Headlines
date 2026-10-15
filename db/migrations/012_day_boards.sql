-- Phase 5b: NBA scores. ESPN's NBA feed has no weeks, so its board is one day. A fetch is cached per requested
-- day (NULL = "today", as ESPN sees it), and the day ESPN answered for is kept so the "today" board can be read back.
ALTER TABLE fetch_log ADD COLUMN IF NOT EXISTS requested_date DATE;
ALTER TABLE fetch_log ADD COLUMN IF NOT EXISTS board_date DATE;
