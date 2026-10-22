import { describe, expect, it } from "vitest";
import { formatCost, formatInr, formatMoney } from "./format";

describe("money formatting", () => {
  it("shows USD and INR side by side", () => {
    // $0.000524 at 88 INR/USD = ₹0.046112
    expect(formatMoney(0.000524, 88)).toBe("$0.000524 · ₹0.0461");
    expect(formatMoney(1.5, 88)).toBe("$1.5000 · ₹132.00");
  });
  it("omits INR when no rate is configured", () => {
    expect(formatMoney(0.000524, null)).toBe("$0.000524");
  });
  it("never invents a cost for unknown", () => {
    expect(formatMoney(null, 88)).toBe("cost n/a");
    expect(formatCost(undefined)).toBe("cost n/a");
  });
  it("handles zero", () => {
    expect(formatMoney(0, 88)).toBe("$0 · ₹0");
    expect(formatInr(0, 88)).toBe("₹0");
  });
});
