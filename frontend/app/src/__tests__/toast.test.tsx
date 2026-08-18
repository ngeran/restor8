import { describe, expect, it } from "vitest";
import { describeError } from "../toast";

describe("describeError (§2 typed-error copy)", () => {
  it("maps backend typed errors to readable copy", () => {
    const e = new Error('502: {"error":"CommitFailedError","device":"p3","message":"error: policy bad"}');
    const out = describeError(e);
    expect(out).toContain("Commit rejected");
    expect(out).toContain("p3");
    expect(out).toContain("policy bad");
  });
  it("handles unstructured errors verbatim", () => {
    expect(describeError(new Error("plain boom"))).toBe("plain boom");
  });
  it("auth errors mention the Secret", () => {
    const e = new Error('502: {"error":"AuthenticationFailedError","device":"p2","message":"auth failed"}');
    expect(describeError(e)).toContain("auth_ref");
  });
});
