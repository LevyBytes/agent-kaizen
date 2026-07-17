/** In-memory FIFO for text follow-ups. Nothing in this class is persisted. */

export const FOLLOW_UP_QUEUE_LIMIT = 10;

export type QueuePauseReason =
  | "failure"
  | "interrupt"
  | "kill"
  | "approval_timeout"
  | "lease_conflict"
  | "uncertain_transport";

export interface QueuedFollowUp {
  id: string;
  prompt: string;
  requestToken?: string;
}

export interface FollowUpQueueView {
  items: Array<{ id: string; preview: string; dispatching: boolean }>;
  paused: boolean;
  pauseReason?: QueuePauseReason;
}

export type EnqueueResult =
  | { accepted: true; item: QueuedFollowUp }
  | { accepted: false; reason: "empty" | "full" };

export class FollowUpQueue {
  private readonly items: QueuedFollowUp[] = [];
  private pauseReasonValue: QueuePauseReason | undefined;
  private dispatchingId: string | undefined;
  private sequence = 0;

  get size(): number {
    return this.items.length;
  }

  get paused(): boolean {
    return this.pauseReasonValue !== undefined;
  }

  get pauseReason(): QueuePauseReason | undefined {
    return this.pauseReasonValue;
  }

  enqueue(prompt: string, requestToken?: string): EnqueueResult {
    const text = prompt.trim();
    if (!text) return { accepted: false, reason: "empty" };
    if (this.items.length >= FOLLOW_UP_QUEUE_LIMIT) return { accepted: false, reason: "full" };
    const item: QueuedFollowUp = {
      id: `follow-up-${++this.sequence}`,
      prompt: text,
      ...(requestToken ? { requestToken } : {}),
    };
    this.items.push(item);
    return { accepted: true, item: { ...item } };
  }

  /** A queued prompt may be offered only after a durable normal turn close. */
  beginDispatchAfterNormalCompletion(): QueuedFollowUp | undefined {
    if (this.paused || this.dispatchingId || this.items.length === 0) return undefined;
    this.dispatchingId = this.items[0].id;
    return { ...this.items[0] };
  }

  /** Remove only the exact head whose durable user message was observed. */
  acknowledgeDurable(itemId: string): boolean {
    if (this.dispatchingId !== itemId || this.items[0]?.id !== itemId) return false;
    this.items.shift();
    this.dispatchingId = undefined;
    return true;
  }

  /** Preserve the head after a denial or uncertain dispatch. */
  rejectDispatch(itemId: string): void {
    if (this.dispatchingId === itemId) this.dispatchingId = undefined;
  }

  /** Pause pending work until clear(); an empty queue is a no-op. */
  pause(reason: QueuePauseReason): void {
    if (this.items.length === 0) return;
    this.pauseReasonValue = reason;
    this.dispatchingId = undefined;
  }

  clear(): void {
    this.items.length = 0;
    this.pauseReasonValue = undefined;
    this.dispatchingId = undefined;
  }

  view(): FollowUpQueueView {
    return {
      items: this.items.map((item) => ({
        id: item.id,
        preview: preview(item.prompt),
        dispatching: item.id === this.dispatchingId,
      })),
      paused: this.paused,
      ...(this.pauseReasonValue ? { pauseReason: this.pauseReasonValue } : {}),
    };
  }
}

function preview(prompt: string): string {
  const collapsed = prompt.replace(/\s+/g, " ").trim();
  const codePoints = [...collapsed];
  return codePoints.length <= 80 ? collapsed : `${codePoints.slice(0, 79).join("")}\u2026`;
}
