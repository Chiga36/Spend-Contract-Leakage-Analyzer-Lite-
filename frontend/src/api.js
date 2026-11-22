import { useEffect, useReducer } from "react";

// ---------------------------------------------------------------------------- fetch helpers
function describe(detail) {
  if (!detail) return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  return JSON.stringify(detail);
}

async function request(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = null;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* not JSON */
    }
    const err = new Error(describe(detail) || `${res.status} ${res.statusText}`);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

export const api = {
  health: () => request("/api/health"),
  startRun: (params) => request("/api/runs", { method: "POST", body: JSON.stringify(params) }),
  getRun: (runId) => request(`/api/runs/${runId}`),
  submitReview: (runId, payload) =>
    request(`/api/runs/${runId}/review`, { method: "POST", body: JSON.stringify(payload) }),
  reportUrl: (runId) => `/api/runs/${runId}/report.html`,
  csvUrl: (runId) => `/api/runs/${runId}/findings.csv`,
  evaluation: (runId) => request(`/api/runs/${runId}/evaluation`),
  datasets: () => request("/api/datasets"),
  dataset: (id) => request(`/api/datasets/${id}`),
  sampleUrl: "/api/datasets/sample.zip",
  // multipart upload: no JSON Content-Type, so the browser sets the form boundary
  uploadDataset: async (files, label) => {
    const body = new FormData();
    for (const f of files) body.append("files", f, f.name);
    if (label) body.append("label", label);
    const res = await fetch("/api/datasets", { method: "POST", body });
    let data = null;
    try {
      data = await res.json();
    } catch {
      /* not JSON */
    }
    if (!res.ok) {
      const d = data?.detail;
      const err = new Error(d?.message || describe(d) || `${res.status} ${res.statusText}`);
      err.status = res.status;
      err.errors = d?.errors || [];
      throw err;
    }
    return data;
  },
};

// ---------------------------------------------------------------------------- live events
export const TERMINAL = ["COMPLETED", "FAILED"];

const initialRun = {
  connected: false,
  status: null,
  iteration: 0,
  steps: {}, // node -> "running" | "done" | "waiting" | "failed"
  summaries: {}, // node -> node_done summary
  tools: [], // tool_call events (kept across re-runs)
  llm: [], // llm_call events
  findings: [], // preliminary findings of the current iteration
  runningTotal: 0,
  warnings: [],
  guardrails: [], // guardrail events (input/output checks around LLM calls)
  review: null, // review_required event
  report: null, // report_ready event
  error: null,
  pid: null,
};

function reduceEvent(state, e) {
  switch (e.type) {
    case "status":
      return { ...state, status: e.status };
    case "node_started":
      if (e.node === "load_data") {
        // A new pass (first run or re-run): reset the per-iteration view, keep the MCP log.
        return {
          ...state,
          iteration: state.iteration + 1,
          steps: { load_data: "running" },
          summaries: {},
          findings: [],
          runningTotal: 0,
          warnings: [],
          guardrails: [],
          review: null,
          report: null,
          error: null,
        };
      }
      return { ...state, steps: { ...state.steps, [e.node]: "running" } };
    case "node_done":
      return {
        ...state,
        steps: { ...state.steps, [e.node]: "done" },
        summaries: { ...state.summaries, [e.node]: e.summary },
      };
    case "tool_call":
      return { ...state, tools: [...state.tools, e], pid: e.pid || state.pid };
    case "llm_call":
      return { ...state, llm: [...state.llm, e] };
    case "finding":
      return {
        ...state,
        findings: [...state.findings, { ...e.finding, seq: e.seq }],
        runningTotal: state.runningTotal + (e.finding.amount || 0),
      };
    case "warning":
      return { ...state, warnings: [...state.warnings, e] };
    case "guardrail":
      return {
        ...state,
        guardrails: [...state.guardrails, e],
        warnings: [...state.warnings, { ...e, guardrail: true, message: `Guardrail ${e.rule}: ${e.message}` }],
      };
    case "review_required":
      return { ...state, review: e, steps: { ...state.steps, human_review: "waiting" } };
    case "report_ready":
      return { ...state, report: e };
    case "error": {
      const steps = { ...state.steps };
      for (const [node, s] of Object.entries(steps)) if (s === "running") steps[node] = "failed";
      return { ...state, error: e.message, steps };
    }
    default:
      return state;
  }
}

function reducer(state, action) {
  if (action.type === "reset") return initialRun;
  if (action.type === "connected") return { ...state, connected: action.value };
  return reduceEvent(state, action.event);
}

/** Subscribe to a run's Server-Sent Events. Every event is replayed on (re)load. */
export function useRunEvents(runId) {
  const [state, dispatch] = useReducer(reducer, initialRun);
  useEffect(() => {
    dispatch({ type: "reset" });
    if (!runId) return undefined;
    const source = new EventSource(`/api/runs/${runId}/events`);
    source.onopen = () => dispatch({ type: "connected", value: true });
    source.onmessage = (msg) => {
      const event = JSON.parse(msg.data);
      dispatch({ type: "event", event });
      if (event.type === "status" && TERMINAL.includes(event.status)) source.close();
    };
    source.onerror = () => dispatch({ type: "connected", value: false });
    return () => source.close();
  }, [runId]);
  return state;
}
