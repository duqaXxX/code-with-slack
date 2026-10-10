/** What every session of the process shares. */
import type { AgentBackend } from "../../agent/seam.ts";
import type { ChatProvider } from "../../chat/seam.ts";
import type { Clock } from "../../clock.ts";
import type { UsageCache } from "../footer.ts";
import type { Holds } from "../hold.ts";
import type { Approvals } from "../requests.ts";
import type { StateStore } from "../state.ts";

export interface SessionDeps {
  /** The chat each session writes its thread through. */
  readonly chat: ChatProvider;
  /** The agent each session starts: its sessions, its listing, its record of trust. */
  readonly agent: AgentBackend;
  readonly state: StateStore;
  readonly approvals: Approvals;
  readonly usage: UsageCache;
  /** Kept in memory only, shared with the handlers of the hold's buttons. */
  readonly holds: Holds;
  /**
   * The time every wait of a session goes by (`INJECTED_TURN_WAIT`, `STOP_TAIL_WAIT`,
   * `IDLE_CLOSE_SECONDS`, `DRAIN_POLL_SECONDS`): a test crosses them without waiting.
   */
  readonly clock: Clock;
  /** Milliseconds since the epoch, for the time left to a limit's reset; `Date.now` when absent. */
  readonly now?: () => number;
  /** `TASKS_KEPT`, unless a test lowers it. */
  readonly tasksKept?: number;
  /** `TASK_REPLIES_KEPT`, unless a test lowers it. */
  readonly taskRepliesKept?: number;
}
