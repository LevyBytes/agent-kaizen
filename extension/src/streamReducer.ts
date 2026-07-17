/** Fail-closed ephemeral reducer: global monotone delta gaps suppress output until durable reconciliation. */

import type { SessionDelta } from "./sessionClient";
import type { WireEvent } from "./webviewProtocol";

export interface StreamingBubble {
  turnId: string;
  text: string;
}

export interface StreamAcceptanceState {
  /** Bounded tail of consumed contiguous sequences, including sequences consumed while suppressed. */
  sequences: number[];
  ordered: boolean;
  durableReplacements: number;
  suppressed: boolean;
}

export class StreamReducer {
  private readonly chunks = new Map<number, { turnId: string; text: string }>();
  private readonly finalizedTurns = new Set<string>();
  private cursor = 0;
  private activeTurn: string | undefined;
  private suppressed = false;
  private acceptedSequences: number[] = [];
  private ordered = true;
  private durableReplacements = 0;

  /** Deduplicate/reorder deltas, suppress gaps or drops, and advance a monotone cursor. */
  apply(deltas: readonly SessionDelta[], deltaCursor: number, dropped: boolean): void {
    if (dropped) {
      this.chunks.clear();
      this.activeTurn = undefined;
      this.suppressed = true;
      this.bumpCursor(deltaCursor);
      return;
    }
    const ordered = deltas
      .filter(validDelta)
      .sort((left, right) => left.seq - right.seq);
    for (const delta of ordered) {
      if (delta.seq <= this.cursor || this.finalizedTurns.has(delta.turn_id)) continue;
      if (delta.seq !== this.cursor + 1) {
        this.chunks.clear();
        this.activeTurn = undefined;
        this.suppressed = true;
        this.ordered = false;
        this.cursor = Math.max(this.cursor, delta.seq);
        continue;
      }
      this.cursor = delta.seq;
      this.acceptedSequences.push(delta.seq);
      if (this.acceptedSequences.length > 4_096) this.acceptedSequences.shift();
      if (this.suppressed) continue;
      if (this.activeTurn && this.activeTurn !== delta.turn_id) this.chunks.clear();
      this.activeTurn = delta.turn_id;
      this.chunks.set(delta.seq, { turnId: delta.turn_id, text: delta.text });
    }
    // The server watermark is authoritative; explicit delta_dropped reports drive suppression.
    this.bumpCursor(deltaCursor);
  }

  /** Finalize streamed turns only when durable events identify the same turn. */
  reconcileDurable(events: readonly WireEvent[]): void {
    let durableAssistant = false;
    let terminalActiveTurn = false;
    for (const event of events) {
      if (event.event_kind === "turn" && (event.marker === "close_ok" || event.marker === "close_fail" || event.marker === "close_canceled")) {
        if (typeof event.turn_id === "string" && event.turn_id) {
          terminalActiveTurn ||= event.turn_id === this.activeTurn;
          this.finalizeTurn(event.turn_id);
        }
      }
      if (event.event_kind !== "chat_message" || event.marker !== "point" || typeof event.body !== "string") continue;
      try {
        const body = JSON.parse(event.body) as Record<string, unknown>;
        if (body.role !== "assistant") continue;
        durableAssistant = true;
        if (typeof body.turn_id === "string" && body.turn_id) this.finalizeTurn(body.turn_id);
        else if (this.activeTurn) this.finalizeTurn(this.activeTurn);
      } catch {
        // Malformed durable bodies do not become a final assistant message.
      }
    }
    if (terminalActiveTurn && this.activeTurn) this.finalizeTurn(this.activeTurn);
    if (durableAssistant && this.chunks.size > 0) this.durableReplacements += 1;
    if (durableAssistant || terminalActiveTurn) this.clearTail();
    if (durableAssistant) this.suppressed = false;
  }

  snapshot(): StreamingBubble | undefined {
    if (this.suppressed || !this.activeTurn) return undefined;
    const text = [...this.chunks.entries()]
      .sort(([left], [right]) => left - right)
      .filter(([, chunk]) => chunk.turnId === this.activeTurn)
      .map(([, chunk]) => chunk.text)
      .join("");
    return text ? { turnId: this.activeTurn, text } : undefined;
  }

  acceptanceState(): StreamAcceptanceState {
    return {
      sequences: [...this.acceptedSequences],
      ordered: this.ordered,
      durableReplacements: this.durableReplacements,
      suppressed: this.suppressed,
    };
  }

  /** Clear failure telemetry and the tail while preserving cursor and finalized-turn history. */
  clearFailure(): void {
    this.clearTail();
    this.suppressed = false;
    this.acceptedSequences = [];
    this.ordered = true;
    this.durableReplacements = 0;
  }

  /** Fully reinitialize streaming state for a new conversation leg. */
  reset(): void {
    this.clearTail();
    this.finalizedTurns.clear();
    this.cursor = 0;
    this.suppressed = false;
    this.acceptedSequences = [];
    this.ordered = true;
    this.durableReplacements = 0;
  }

  private clearTail(): void {
    this.chunks.clear();
    this.activeTurn = undefined;
  }

  private bumpCursor(value: number): void {
    if (Number.isSafeInteger(value) && value >= this.cursor) this.cursor = value;
  }

  private finalizeTurn(turnId: string): void {
    this.finalizedTurns.add(turnId);
    if (this.finalizedTurns.size > 1_024) {
      const oldest = this.finalizedTurns.values().next().value;
      if (oldest !== undefined) this.finalizedTurns.delete(oldest);
    }
  }
}

function validDelta(value: SessionDelta): boolean {
  return Number.isSafeInteger(value.seq) && value.seq > 0 && typeof value.turn_id === "string" && !!value.turn_id && typeof value.text === "string";
}
