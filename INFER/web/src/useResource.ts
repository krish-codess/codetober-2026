import { useCallback, useEffect, useState } from "react";
import { ApiError, getJson } from "./api";

/**
 * Loads one GET resource, deliberately cached.
 *
 * - A copy younger than `freshFor` is shown with no request at all (switching tabs or datasets and back is free).
 * - An older copy is shown immediately and revalidated once in the background.
 * - If that revalidation fails, the copy stays on screen, marked stale.
 * - With no copy, transient failures are retried with backoff, then handed to the user with a way to retry.
 * - `reload()` is the one way to bypass the cache on purpose.
 */

export const tuning = { freshFor: 60_000, retryDelays: [600, 1800] };

type Saved = { data: unknown; at: number };
const cache = new Map<string, Saved>();
export const clearCache = (): void => cache.clear();

export type Resource<T> =
  | { status: "loading"; attempt: number; attempts: number }
  | { status: "ready"; data: T; stale: boolean; at: number }
  | { status: "empty"; message: string }
  | { status: "unauthorized" }
  | { status: "error"; message: string; requestId?: string };

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

export function useResource<T>(path: string | null): { state: Resource<T>; reload: () => void } {
  const attempts = tuning.retryDelays.length + 1;
  const [state, setState] = useState<Resource<T>>({ status: "loading", attempt: 1, attempts });
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    const set = (next: Resource<T>) => !cancelled && setState(next);
    const saved = cache.get(path);
    if (saved) {
      set({ status: "ready", data: saved.data as T, stale: false, at: saved.at });
      if (Date.now() - saved.at < tuning.freshFor) return;
    } else {
      set({ status: "loading", attempt: 1, attempts });
    }

    (async () => {
      const tries = saved ? 1 : attempts; // with a copy on screen, one quiet try is enough
      for (let attempt = 1; attempt <= tries; attempt++) {
        if (!saved) set({ status: "loading", attempt, attempts });
        try {
          const data = await getJson<T>(path);
          const at = Date.now();
          cache.set(path, { data, at });
          return set({ status: "ready", data, stale: false, at });
        } catch (err) {
          const error = err instanceof ApiError ? err : new ApiError(0, "unknown", "Something unexpected went wrong.");
          if (error.status === 401) return set({ status: "unauthorized" });
          if (error.status === 404) return set({ status: "empty", message: error.message });
          if (saved) return set({ status: "ready", data: saved.data as T, stale: true, at: saved.at });
          if (!error.transient || attempt === tries) return set({ status: "error", message: error.message, requestId: error.requestId });
          await sleep(tuning.retryDelays[attempt - 1]);
          if (cancelled) return;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path, generation, attempts]);

  const reload = useCallback(() => {
    if (path) cache.delete(path);
    setGeneration((g) => g + 1);
  }, [path]);
  return { state, reload };
}
