import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

/**
 * Load data for a page. A request started for an older dependency set is aborted,
 * so a slow response can never overwrite the data for the current selection.
 */
export function useAsync<T>(load: (signal: AbortSignal) => Promise<T>, deps: readonly unknown[]): AsyncState<T> {
  const [state, setState] = useState<{ data: T | undefined; error: unknown; loading: boolean }>({
    data: undefined,
    error: undefined,
    loading: true,
  });
  const [version, setVersion] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setState((previous) => ({ data: previous.data, error: undefined, loading: true }));
    load(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ data, error: undefined, loading: false });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setState({ data: undefined, error, loading: false });
      });
    return () => controller.abort();
    // The caller owns the dependency list; ``version`` forces a manual reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, version]);

  const reload = useCallback(() => setVersion((value) => value + 1), []);
  return { data: state.data, error: state.error, loading: state.loading, reload };
}
