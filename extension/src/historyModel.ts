/** Fail-closed normalization of session lists, run fidelity, and artifact-expiry metadata. */

import type { HistoryEntryView, HistorySnapshot } from "./webviewProtocol";

/** Convert raw session-list data to a sorted, truncation-aware snapshot. */
export function historyFromSessionList(response: Record<string, unknown> | undefined): HistorySnapshot {
  const rows = Array.isArray(response?.sessions) ? response.sessions : [];
  const entries = rows
    .map(historyEntry)
    .filter((entry): entry is HistoryEntryView => entry !== undefined)
    .sort((left, right) => compareNewest(left, right));
  const sessionsOmitted = finiteNonNegative(response?.sessions_omitted);
  return {
    entries,
    ...(sessionsOmitted ? { sessionsOmitted } : {}),
    truncated: response?.truncated === true || sessionsOmitted > 0,
  };
}

/** Normalize one row, rejecting entries without session, controller, or run identity. */
function historyEntry(value: unknown): HistoryEntryView | undefined {
  if (!record(value)) return undefined;
  const sessionId = clean(value.session_id);
  const controller = value.controller === "observed" ? "observed" : value.controller === "driven" ? "driven" : undefined;
  const runs = Array.isArray(value.runs) ? value.runs.filter(record) : [];
  const latest = latestRun(value, runs);
  const agentRunId = clean(value.latest_run_id) ?? clean(latest?.id) ?? clean(latest?.agent_run_id);
  if (!sessionId || !controller || !agentRunId) return undefined;

  const createdAt = clean(value.created_at) ?? clean(latest?.created_at);
  const snippet = clean(value.snippet) ?? clean(value.summary);
  const title = clean(value.title) ?? titleFallback(snippet);
  const turnState = clean(value.latest_run_state) ?? clean(latest?.turn_state);
  const terminalState = clean(value.latest_terminal_state) ?? clean(latest?.terminal_state);
  // Any reported terminal state is authoritative even when the turn state is absent.
  const terminal = latest?.terminal === true || turnState === "terminal" || terminalState !== undefined;
  const resumable = controller === "driven" && value.resumable === true;
  const readOnly = controller === "observed" || (terminal && !resumable);
  const engine = clean(value.engine) ?? clean(latest?.engine);
  const fidelity = fidelityMetadata(value);
  const expiredArtifacts = expiredArtifactMetadata(value);
  const omittedMessages = finiteNonNegative(
    value.omitted_message_count ?? value.omitted_messages ?? value.messages_omitted,
  );
  const omittedRunCount = Math.max(
    finiteNonNegative(value.runs_total) - finiteNonNegative(value.runs_returned),
    0,
  );
  const runsOmitted = value.runs_truncated === true ? Math.max(omittedRunCount, 1) : omittedRunCount;

  return {
    sessionId,
    agentRunId,
    controller,
    title,
    ...(snippet ? { snippet } : {}),
    ...(createdAt ? { createdAt } : {}),
    ...(engine ? { engine } : {}),
    state: resumable ? "resumable" : readOnly ? "read-only" : turnState === "idle" ? "idle" : "running",
    resumable,
    readOnly,
    ...(terminalState ? { terminalState } : {}),
    ...(fidelity ? { fidelity } : {}),
    ...(omittedMessages ? { omittedMessages } : {}),
    ...(runsOmitted ? { runsOmitted } : {}),
    ...(expiredArtifacts.length ? { expiredArtifacts } : {}),
  };
}

/** Select latest_run_id when present, otherwise the final returned run. */
function latestRun(row: Record<string, unknown>, runs: Record<string, unknown>[]): Record<string, unknown> | undefined {
  const requested = clean(row.latest_run_id);
  if (requested) {
    const match = runs.find((run) => clean(run.id ?? run.agent_run_id) === requested);
    if (match) return match;
  }
  return runs.at(-1);
}

/** Normalize continuation fidelity to reduced, full, or unknown. */
function fidelityMetadata(row: Record<string, unknown>): HistoryEntryView["fidelity"] | undefined {
  const source = record(row.continuation) ? row.continuation : row;
  const raw = clean(source.resume_fidelity ?? source.fidelity);
  const transcriptSeeded = source.transcript_seeded === true || raw === "transcript_seeded" || raw === "reduced";
  if (transcriptSeeded) {
    return { mode: "reduced", message: "Continued from durable transcript metadata; original vendor context was not replayed." };
  }
  if (raw === "full" || raw === "vendor") return { mode: "full", message: "Full vendor continuation is available." };
  return undefined;
}

/** Prefer live artifacts; otherwise treat expired_artifacts entries as expired. */
function expiredArtifactMetadata(row: Record<string, unknown>): NonNullable<HistoryEntryView["expiredArtifacts"]> {
  const usingExpiredList = !Array.isArray(row.artifacts) && Array.isArray(row.expired_artifacts);
  const values = Array.isArray(row.artifacts) ? row.artifacts : usingExpiredList ? row.expired_artifacts as unknown[] : [];
  return values.flatMap((value) => {
    if (!record(value) || !(value.expired === true || value.available === false || usingExpiredList)) return [];
    const kind = clean(value.kind ?? value.media_type) ?? "artifact";
    const name = clean(value.name);
    const sha = clean(value.sha256);
    const sha256 = sha && /^[0-9a-f]{64}$/i.test(sha) ? sha : undefined;
    const bytes = finiteNonNegative(value.bytes);
    return [{ kind, ...(name ? { name } : {}), ...(sha256 ? { sha256 } : {}), ...(bytes ? { bytes } : {}) }];
  });
}

/** Build an at-most-80-code-point title or the untitled fallback. */
function titleFallback(snippet: string | undefined): string {
  if (!snippet) return "Untitled conversation";
  const codePoints = [...snippet];
  return codePoints.length <= 80 ? snippet : `${codePoints.slice(0, 79).join("")}\u2026`;
}

/** Sort dated entries newest-first, then use descending session id. */
function compareNewest(left: HistoryEntryView, right: HistoryEntryView): number {
  const leftTime = Date.parse(left.createdAt ?? "");
  const rightTime = Date.parse(right.createdAt ?? "");
  const leftRank = Number.isFinite(leftTime) ? leftTime : Number.NEGATIVE_INFINITY;
  const rightRank = Number.isFinite(rightTime) ? rightTime : Number.NEGATIVE_INFINITY;
  if (leftRank !== rightRank) return rightRank > leftRank ? 1 : -1;
  return right.sessionId.localeCompare(left.sessionId);
}

/** Floor a finite positive number; all other values become zero. */
function finiteNonNegative(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
}

/** Trim a non-empty string. */
function clean(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

/** Narrow non-null, non-array objects. */
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
