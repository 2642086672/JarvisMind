/**
 * One-time confirmation tokens returned when a forget or modification request is
 * created. They are held only in memory for this page session: never written to
 * storage, and gone after a reload. A token is taken (removed) when it is used.
 */
const tokens = new Map<string, string>();

export function rememberPendingToken(requestId: string, token: string): void {
  tokens.set(requestId, token);
}

export function takePendingToken(requestId: string): string | undefined {
  const token = tokens.get(requestId);
  tokens.delete(requestId);
  return token;
}

export function hasPendingToken(requestId: string): boolean {
  return tokens.has(requestId);
}

export function clearPendingTokens(): void {
  tokens.clear();
}
