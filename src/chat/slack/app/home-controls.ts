/**
 * The controls of the session index (the app's Home tab, `home.ts`): its filters, Show all, Edit,
 * Delete and Clean up. Port of `home_owner` and the `on_home_*` listeners of `slack_app.py`.
 */
import { EDIT_ON, type Home, readFilter } from "../home.ts";
import { type AppParts, logger } from "./answers.ts";
import { interactionActor, isOwner } from "./guards.ts";
import { type Ack, firstAction, type Payload, record, str } from "./wire.ts";

export class HomeControls {
  private readonly parts: AppParts;
  private readonly home: Home;

  constructor(parts: AppParts) {
    this.parts = parts;
    this.home = parts.home;
  }

  /**
   * Whether a use of a Home tab control is the owner's. The page is published to the owner
   * alone, and each use is still checked on its own, as every inbound path is; a Home tab
   * payload names no channel, so there is none to guard.
   */
  private homeOwner(body: Payload): boolean {
    if (this.parts.stopped.aborted) return false; // the daemon is gone: nothing to publish or delete
    const [user, team] = interactionActor(body);
    if (isOwner(this.parts.identity, user, team)) return true;
    logger.info("ignored an inbound event from someone other than the owner");
    return false;
  }

  // The Home tab's link button: Slack follows the link itself and still sends the click, which
  // only needs acknowledging. Nothing is read from it and nothing is done, whoever clicked.
  onHomeLink = async (ack: Ack): Promise<void> => {
    await ack();
  };

  onHomeFilter = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    if (!this.homeOwner(body)) return;
    // The controls' own state rides on the payload: every filter is read from it at once.
    const values = record(record(body.view).state).values;
    await this.home.choose(readFilter(values, this.home.chosen));
  };

  onHomeShowAll = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    if (!this.homeOwner(body)) return;
    // Showing all of a channel is choosing that channel in the filter (`Home.choose` checks it
    // is a bound one).
    const channel = str(firstAction(body).value);
    await this.home.choose({ ...this.home.chosen, channel });
  };

  onHomeEdit = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    if (!this.homeOwner(body)) return;
    await this.home.edit(str(firstAction(body).value) === EDIT_ON);
  };

  // Slack sends this click only once the owner confirmed in the button's own dialog.
  onHomeDelete = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    if (!this.homeOwner(body)) return;
    // The value names the thread; `Home.delete` deletes only one `state.json` holds.
    const value = str(firstAction(body).value);
    const at = value.indexOf(":");
    const channel = at === -1 ? value : value.slice(0, at);
    const threadTs = at === -1 ? "" : value.slice(at + 1);
    await this.home.delete(channel, threadTs);
  };

  // Sent, like Delete, only once the owner confirmed in the button's own dialog.
  onHomeClean = async (ack: Ack, body: Payload): Promise<void> => {
    await ack();
    if (!this.homeOwner(body)) return;
    await this.home.clean(str(firstAction(body).value));
  };
}
