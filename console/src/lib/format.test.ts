import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration, memoryStatusLabel, percent, statusTone, truncate } from "./format";

describe("format helpers", () => {
  it("formats byte sizes with binary units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(-1)).toBe("—");
  });

  it("formats durations in Chinese", () => {
    expect(formatDuration(45)).toBe("45 秒");
    expect(formatDuration(3725)).toBe("1 小时 2 分");
  });

  it("truncates long text with an ellipsis", () => {
    expect(truncate("abcdef", 4)).toBe("abc…");
    expect(truncate("abc", 4)).toBe("abc");
  });

  it("computes safe percentages", () => {
    expect(percent(1, 4)).toBe(25);
    expect(percent(1, 0)).toBe(0);
  });

  it("maps memory states to labels and tones", () => {
    expect(memoryStatusLabel("DELETED")).toBe("已遗忘");
    expect(statusTone("DELETED")).toBe("danger");
    expect(statusTone("unknown-state")).toBe("neutral");
  });
});
