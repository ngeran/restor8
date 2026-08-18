import { describe, expect, it } from "vitest";
import { classifyDiffLine } from "../Configurations";

// §11: these encode the SHIPPED bug (comma-operator startsWith) so it
// can never regress silently.
describe("classifyDiffLine", () => {
  it("classifies diff headers", () => {
    expect(classifyDiffLine("+++ b/config")).toBe("header");
    expect(classifyDiffLine("--- a/config")).toBe("header");
    expect(classifyDiffX("@@ -1,3 +1,4 @@")).toBe("header");
  });
  it("classifies adds/dels/context", () => {
    expect(classifyDiffLine("+   static {")).toBe("add");
    expect(classifyDiffLine("-       route 192.0.2.0/24 discard;")).toBe("del");
    expect(classifyDiffLine("   routing-options {")).toBe("ctx");
  });
  it("a minus that is part of context stays context-ish (starts with - only counts)", () => {
    expect(classifyDiffLine("")).toBe("ctx");
    expect(classifyDiffLine("  plain line")).toBe("ctx");
  });
});
function classifyDiffX(l: string) { return classifyDiffLine(l); }
