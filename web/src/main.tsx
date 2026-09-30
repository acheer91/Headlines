import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { Scoreboard } from "./Scoreboard";
import { GamePage } from "./GamePage";
import { Home } from "./Home";
import { ErrorBoundary } from "./ErrorBoundary";
import "./styles.css";

// NFL (Phase 1) and NCAAF (Phase 5a); the other tabs are placeholders so the layout is final.
const TABS = [
  { id: "nfl", label: "NFL", enabled: true },
  { id: "ncaaf", label: "NCAAF", enabled: true },
  { id: "nba", label: "NBA", enabled: false },
  { id: "epl", label: "EPL", enabled: false },
  { id: "mls", label: "MLS", enabled: false },
];

function TabBar() {
  return (
    <nav className="tabs">
      {TABS.map((t) =>
        t.enabled ? (
          <NavLink key={t.id} to={`/scores/${t.id}`} className={({ isActive }) => (isActive ? "tab active" : "tab")}>
            {t.label}
          </NavLink>
        ) : (
          <span key={t.id} className="tab disabled" title="Coming in Phase 5">
            {t.label}
          </span>
        ),
      )}
    </nav>
  );
}

/** A render failure on one page shouldn't follow you to the next: a new path gets a fresh boundary. */
function RoutedErrorBoundary({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  return <ErrorBoundary key={pathname}>{children}</ErrorBoundary>;
}

function App() {
  return (
    <BrowserRouter>
      <RoutedErrorBoundary>
      <Routes>
        {/* Screen A: the AI headlines (Phase 4); the NFL board until the first set is written. */}
        <Route path="/" element={<Home />} />
        <Route path="/scores/:league" element={<Scoreboard />} />
        <Route path="/game/:id" element={<GamePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
      </RoutedErrorBoundary>
      <TabBar />
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
