import { api, type Memory } from "./api";
import { act } from "./store";

/**
 * Saving a memory's text from the panel (the Mac inspector, and editing a row in the list): one request
 * at a time per memory, the newest text last. The queue lives here rather than in a component because a
 * component can go away while its save is on its way (selecting another memory and coming back mounts a
 * new inspector; a row's edit box closes) and the next edit of the same memory must queue behind it, so
 * an older edit can never land after a newer one.
 */
export const SUPERSEDED = Symbol("superseded");
export type SaveResult = Memory | undefined | typeof SUPERSEDED;

type Job = { text: string; done: (result: SaveResult) => void };
type Queue = { next: Job | null; running: boolean };
const queues = new Map<string, Queue>();

/** Whether a save of this memory is on its way or waiting, so the inspector's copy of it may be stale. */
export function saving(id: string): boolean {
  const q = queues.get(id);
  return !!q && (q.running || !!q.next);
}

/**
 * Save `text` as the memory's content. Resolves with the saved memory, `undefined` when the save failed
 * (the error is shown), or SUPERSEDED when newer text for the memory replaced it before it was sent.
 */
export function saveText(id: string, text: string): Promise<SaveResult> {
  const q = queues.get(id) ?? { next: null, running: false };
  queues.set(id, q);
  return new Promise((done) => {
    q.next?.done(SUPERSEDED);
    q.next = { text, done };
    if (!q.running) void run(id, q);
  });
}

async function run(id: string, q: Queue) {
  q.running = true;
  let previous: Memory | undefined; // what the last save left in the vault; a prop may lag behind it
  while (q.next) {
    const job = q.next;
    q.next = null;
    const saved = previous?.content === job.text ? previous : await act(() => api.patchMemory(id, { content: job.text }));
    previous = saved ?? previous;
    job.done(saved);
  }
  q.running = false;
  queues.delete(id);
}

/** Whether newer text for the memory is waiting to be saved after the current one. */
export function waiting(id: string): boolean {
  return !!queues.get(id)?.next;
}
