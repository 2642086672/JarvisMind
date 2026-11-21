import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "./App";

describe("App", () => {
  it("asks for the owner credential when no session exists", () => {
    render(<App />);
    expect(screen.getByText("使用所有者令牌登录")).toBeTruthy();
    expect(screen.getByLabelText("所有者令牌")).toBeTruthy();
  });
});
