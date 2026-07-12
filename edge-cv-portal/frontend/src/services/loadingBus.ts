/**
 * Global in-flight request tracker.
 *
 * Every API call routes through ApiService.request, which brackets the fetch
 * with begin()/end(). A single global indicator (GlobalLoadingBar) subscribes
 * to the active-count so the user always sees activity while data is loading —
 * no per-page wiring required.
 */
type Listener = (activeCount: number) => void;

let activeCount = 0;
const listeners = new Set<Listener>();

function notify() {
  for (const l of listeners) {
    try {
      l(activeCount);
    } catch {
      /* listener errors must not break request tracking */
    }
  }
}

export function beginRequest(): void {
  activeCount += 1;
  notify();
}

export function endRequest(): void {
  activeCount = Math.max(0, activeCount - 1);
  notify();
}

export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  // Emit current state immediately so late subscribers are in sync.
  listener(activeCount);
  return () => {
    listeners.delete(listener);
  };
}

export function getActiveCount(): number {
  return activeCount;
}
