import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, fetchWrapped, type Wrapped } from "./api";

/**
 * Loads one user's story, deliberately cached.
 *
 * - A saved copy younger than FRESH_FOR is shown with no request at all.
 * - An older copy is shown immediately and revalidated with its ETag (a 304 costs no payload).
 * - If the server cannot be reached, the saved copy stays on screen, marked stale.
 * - With nothing saved, transient failures are retried with backoff, then handed to the user.
 * - "Reload" is the one way to bypass the cache on purpose.
 */

const FRESH_FOR = 5 * 60 * 1000;
const RETRY_DELAYS = [1000, 2000, 4000];

type Saved = { wrapped: Wrapped; etag: string; savedAt: number };

export type LoadStep = "connecting" | "receiving" | "preparing";
export type WrappedState =
  | { status: "loading"; step: LoadStep; attempt: number; attempts: number }
  | { status: "ready"; wrapped: Wrapped; stale: boolean; savedAt: number }
  | { status: "empty" } // signed in, but there is no story for this account
  | { status: "unauthorized" }
  | { status: "error"; message: string; requestId?: string };

const cacheKey = (token: string) => `wrapped:v1:${token.slice(-24)}`;

function readCache(token: string): Saved | null {
  try {
    const saved = JSON.parse(localStorage.getItem(cacheKey(token)) ?? "null") as Saved | null;
    return saved && Array.isArray(saved.wrapped?.cards) ? saved : null;
  } catch {
    return null; // corrupt or blocked storage is the same as no cache
  }
}

function writeCache(token: string, saved: Saved | null): void {
  try {
    if (saved) localStorage.setItem(cacheKey(token), JSON.stringify(saved));
    else localStorage.removeItem(cacheKey(token));
  } catch {
    // storage full or disabled: the story still works, it is just not cached
  }
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export function useWrapped(token: string | null): { state: WrappedState; reload: () => void } {
  const [state, setState] = useState<WrappedState>({ status: "loading", step: "connecting", attempt: 1, attempts: RETRY_DELAYS.length + 1 });
  const [generation, setGeneration] = useState(0);
  const force = useRef(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const set = (next: WrappedState) => !cancelled && setState(next);

    (async () => {
      const saved = readCache(token);
      if (saved) {
        set({ status: "ready", wrapped: saved.wrapped, stale: false, savedAt: saved.savedAt });
        if (!force.current && Date.now() - saved.savedAt < FRESH_FOR) return;
      }
      force.current = false;
      const attempts = saved ? 1 : RETRY_DELAYS.length + 1; // with a copy on screen, one quiet try is enough
      for (let attempt = 1; attempt <= attempts; attempt++) {
        if (!saved) set({ status: "loading", step: "connecting", attempt, attempts });
        try {
          const result = await fetchWrapped(token, saved?.etag, () => {
            if (!saved) set({ status: "loading", step: "receiving", attempt, attempts });
          });
          const next: Saved = result ? { ...result, savedAt: Date.now() } : { ...saved!, savedAt: Date.now() };
          if (!saved) set({ status: "loading", step: "preparing", attempt, attempts });
          writeCache(token, next);
          set({ status: "ready", wrapped: next.wrapped, stale: false, savedAt: next.savedAt });
          return;
        } catch (err) {
          const error = err instanceof ApiError ? err : new ApiError(0, "unknown", "Something unexpected went wrong.");
          if (error.status === 401) {
            writeCache(token, null);
            return set({ status: "unauthorized" });
          }
          if (error.status === 404) {
            writeCache(token, null);
            return set({ status: "empty" });
          }
          if (saved) return set({ status: "ready", wrapped: saved.wrapped, stale: true, savedAt: saved.savedAt });
          if (!error.transient || attempt === attempts) {
            return set({ status: "error", message: error.message, requestId: error.requestId });
          }
          await sleep(RETRY_DELAYS[attempt - 1]);
          if (cancelled) return;
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [token, generation]);

  const reload = useCallback(() => {
    force.current = true;
    setGeneration((g) => g + 1);
  }, []);
  return { state, reload };
}
