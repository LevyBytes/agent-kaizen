/** One workspace session/list poll with fan-out to every conversation controller. */

/** Client returning the full driven-and-observed session/list response. */
export interface SessionListClient {
  list(args?: { controller?: "driven" | "observed"; limit?: number }): Promise<Record<string, unknown>>;
  dispose?(): void;
}

/** Monotonic poll revision with at most one of response or error populated. */
export interface SessionIndexUpdate {
  revision: number;
  response?: Record<string, unknown>;
  error?: Error;
}

export type SessionIndexListener = (update: SessionIndexUpdate) => void;

/** Single-flight poller that converts every completion to a revisioned broadcast. */
export class SessionIndex {
  private responseValue: Record<string, unknown> | undefined;
  private errorValue: Error | undefined;
  private revisionValue = 0;
  private inFlight: Promise<SessionIndexUpdate> | undefined;
  private readonly listeners = new Set<SessionIndexListener>();

  constructor(private readonly client: SessionListClient) {}

  /** Return the latest shared OK response; consumers must treat it as read-only. */
  current(): Record<string, unknown> | undefined {
    return this.responseValue;
  }

  /** Return shared current response/error references with the current revision. */
  snapshot(): SessionIndexUpdate {
    return {
      revision: this.revisionValue,
      ...(this.responseValue ? { response: this.responseValue } : {}),
      ...(this.errorValue ? { error: this.errorValue } : {}),
    };
  }

  /** Subscribe and immediately replay a snapshot after the first completed poll. */
  subscribe(listener: SessionIndexListener): { dispose(): void } {
    this.listeners.add(listener);
    if (this.revisionValue > 0) listener(this.snapshot());
    return { dispose: () => this.listeners.delete(listener) };
  }

  /** Share an active poll or start one; completed polls never reject. */
  refresh(): Promise<SessionIndexUpdate> {
    if (this.inFlight) return this.inFlight;
    const operation = this.poll().finally(() => {
      if (this.inFlight === operation) this.inFlight = undefined;
    });
    this.inFlight = operation;
    return operation;
  }

  dispose(): void {
    this.listeners.clear();
    this.client.dispose?.();
  }

  /** Capture all errors, increment the revision, and broadcast a stable listener snapshot. */
  private async poll(): Promise<SessionIndexUpdate> {
    try {
      // No controller filter: one response contains driven and observed sessions for all panels.
      const response = await this.client.list({ limit: 1_000 });
      if (!response || typeof response !== "object" || response.status !== "OK") {
        throw new SessionIndexError(response);
      }
      this.responseValue = response;
      this.errorValue = undefined;
    } catch (error) {
      this.responseValue = undefined;
      this.errorValue = error instanceof Error ? error : new Error(String(error));
    }
    this.revisionValue += 1;
    const update = this.snapshot();
    for (const listener of [...this.listeners]) listener(update);
    return update;
  }
}

/** Non-OK session/list response retaining the raw response and best available detail. */
export class SessionIndexError extends Error {
  readonly code?: string;

  constructor(readonly response: Record<string, unknown>) {
    const code = typeof response.code === "string" ? response.code : undefined;
    const detail = [response.message, response.error, response.required_action, code]
      .find((value): value is string => typeof value === "string" && !!value.trim());
    super(detail ? `session/list denied: ${detail}` : "session/list denied");
    this.name = "SessionIndexError";
    this.code = code;
  }
}
