import { useEffect, useRef, useState } from "react";

const PULL_THRESHOLD = 70;

/** Pull model: refresh on pull-down and when the app comes back to the foreground, never on a timer. */
export function usePull(onRefresh: (force: boolean) => void, busy: boolean) {
  const [pull, setPull] = useState(0);
  const startY = useRef<number | null>(null);

  useEffect(() => {
    const onVis = () => document.visibilityState === "visible" && onRefresh(false);
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, [onRefresh]);

  const handlers = {
    onTouchStart: (e: React.TouchEvent) => {
      startY.current = window.scrollY <= 0 ? e.touches[0].clientY : null;
    },
    onTouchMove: (e: React.TouchEvent) => {
      if (startY.current == null) return;
      setPull(Math.max(0, Math.min(120, e.touches[0].clientY - startY.current)));
    },
    onTouchEnd: () => {
      if (pull >= PULL_THRESHOLD && !busy) onRefresh(true);
      setPull(0);
      startY.current = null;
    },
  };

  const indicator = (
    <div className="pull" style={{ height: pull }}>
      {pull > 0 && (pull >= PULL_THRESHOLD ? "Release to refresh" : "Pull to refresh")}
    </div>
  );
  return { handlers, indicator };
}
