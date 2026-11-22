import { useCallback, useEffect, useState } from "react";
import { api, useRunEvents } from "./api.js";
import StartPage from "./pages/StartPage.jsx";
import RunPage from "./pages/RunPage.jsx";
import ReviewPage from "./pages/ReviewPage.jsx";
import ResultsPage from "./pages/ResultsPage.jsx";

// Tiny state-based router (no react-router). The page and run id are mirrored in the URL hash
// so a browser refresh comes back to the same run, whose event stream then replays.
const PAGES = ["start", "run", "review", "results"];
const STEPS = [
  { page: "start", label: "Start" },
  { page: "run", label: "Analyze" },
  { page: "review", label: "Review" },
  { page: "results", label: "Results" },
];

function parseHash() {
  const [page, runId] = window.location.hash.replace(/^#\/?/, "").split("/");
  const valid = /^[0-9a-f]{12}$/.test(runId || "");
  if (!PAGES.includes(page) || page === "start" || !valid) return { page: "start", runId: null };
  return { page, runId };
}

export default function App() {
  const [route, setRoute] = useState(parseHash);
  const [health, setHealth] = useState(null);
  const [healthError, setHealthError] = useState(null);
  const run = useRunEvents(route.runId);

  const navigate = useCallback((page, runId) => {
    const next = page === "start" ? { page, runId: null } : { page, runId };
    const hash = page === "start" ? "#/start" : `#/${page}/${runId}`;
    if (window.location.hash !== hash) window.history.pushState(null, "", hash);
    setRoute(next);
    window.scrollTo(0, 0);
  }, []);

  useEffect(() => {
    const onPop = () => setRoute(parseHash());
    window.addEventListener("popstate", onPop);
    window.addEventListener("hashchange", onPop);
    return () => {
      window.removeEventListener("popstate", onPop);
      window.removeEventListener("hashchange", onPop);
    };
  }, []);

  const refreshHealth = useCallback(() => {
    api
      .health()
      .then((h) => {
        setHealth(h);
        setHealthError(null);
      })
      .catch((e) => setHealthError(e.message));
  }, []);

  // Refresh on first load and whenever the Start page is shown again.
  const onStartPage = route.page === "start";
  useEffect(() => {
    if (onStartPage || !health) refreshHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onStartPage, refreshHealth]);

  const reached = {
    start: true,
    run: !!route.runId,
    review: !!route.runId && (run.status === "AWAITING_REVIEW" || !!run.report || run.status === "COMPLETED"),
    results: !!route.runId && (run.status === "COMPLETED" || !!run.report),
  };

  const props = { runId: route.runId, run, health, navigate };
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand" onClick={() => navigate("start")} role="link" tabIndex={0}>
          <span className="logo" aria-hidden="true" />
          <div>
            <div className="brand-name">Leakage Analyzer</div>
            <div className="brand-sub">Spend &amp; contract leakage · accounts payable</div>
          </div>
        </div>
        <nav className="stepper" aria-label="Progress">
          {STEPS.map((s, i) => (
            <button
              key={s.page}
              className={`step ${route.page === s.page ? "current" : ""}`}
              disabled={!reached[s.page]}
              onClick={() => navigate(s.page, route.runId)}
            >
              <span className="step-no">{i + 1}</span>
              {s.label}
            </button>
          ))}
        </nav>
        <div className="status-badges">
          <span className={`badge ${health?.mcp === "up" ? "badge-ok" : "badge-bad"}`} title={health?.mcp_error || ""}>
            <span className="dot" />
            MCP {health?.mcp === "up" ? `· stdio · pid ${health.pid}` : "down"}
          </span>
          <span
            className={`badge ${health?.llm_provider ? "badge-llm" : "badge-off"}`}
            title={health?.llm_provider ? health.llm_model : "No outbound traffic"}
          >
            LLM {health?.llm_provider ? `· ${health.llm_provider}` : "off · offline"}
          </span>
        </div>
      </header>

      <main className="page">
        {route.page === "start" && (
          <StartPage health={health} healthError={healthError} onRetry={refreshHealth} navigate={navigate} />
        )}
        {route.page === "run" && <RunPage {...props} />}
        {route.page === "review" && <ReviewPage {...props} />}
        {route.page === "results" && <ResultsPage {...props} />}
      </main>
    </div>
  );
}
