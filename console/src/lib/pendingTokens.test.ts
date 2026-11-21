import { describe, expect, it } from "vitest";
import { clearPendingTokens, hasPendingToken, rememberPendingToken, takePendingToken } from "./pendingTokens";

describe("pending confirmation tokens", () => {
  it("are held in memory and handed out exactly once", () => {
    rememberPendingToken("req-1", "token-abc");
    expect(hasPendingToken("req-1")).toBe(true);
    expect(takePendingToken("req-1")).toBe("token-abc");
    expect(takePendingToken("req-1")).toBeUndefined();
    expect(hasPendingToken("req-1")).toBe(false);
  });

  it("can be cleared on sign-out", () => {
    rememberPendingToken("req-2", "token-def");
    clearPendingTokens();
    expect(takePendingToken("req-2")).toBeUndefined();
  });
});
