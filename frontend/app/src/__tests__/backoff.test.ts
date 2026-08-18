import { describe, expect, it } from "vitest";
import { backoffDelay } from "../events";

describe("backoffDelay (§9 reconnect policy)", () => {
  it("grows exponentially from 1s", () => {
    expect(backoffDelay(0)).toBeGreaterThanOrEqual(800);
    expect(backoffDelay(0)).toBeLessThanOrEqual(1200);
  });
  it("caps at 15s (+jitter)", () => {
    for (let i = 0; i < 50; i++) {
      expect(backoffDelay(20)).toBeLessThanOrEqual(18000);
    }
  });
  it("always within ±20% of the raw value", () => {
    for (let attempt = 0; attempt < 8; attempt++) {
      const raw = Math.min(1000 * 2 ** attempt, 15000);
      const d = backoffDelay(attempt);
      expect(d).toBeGreaterThanOrEqual(raw * 0.79);
      expect(d).toBeLessThanOrEqual(raw * 1.21);
    }
  });
});
