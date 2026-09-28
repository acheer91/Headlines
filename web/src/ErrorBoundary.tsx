import { Component, type ReactNode } from "react";
import { forgetAll } from "./lastSeen";

/**
 * One screen that fails to render must not blank the whole app. Most likely cause: a saved last-seen
 * response from an older version of the app. So: drop the saved copies and offer a reload.
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error("screen failed to render", error);
    forgetAll();
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="page">
        <div className="banner error" style={{ marginTop: 24 }}>
          Something went wrong showing this screen.{" "}
          <button className="linkish" onClick={() => window.location.assign("/scores/nfl")}>
            Reload scores
          </button>
        </div>
      </div>
    );
  }
}
