import { describe, expect, it } from "vitest";
import { axisTicks, barPercent, compactNumber, formatUsdAxis, labelIndexes, niceMax, shortDate } from "./chart";

describe("niceMax", () => {
  it.each([
    [0.0013, 0.002],
    [0.0021, 0.0025],
    [0.0031, 0.005],
    [0.0051, 0.01],
    [1, 1],
    [7, 10],
    [0, 1],
  ])("%s -> %s", (input, expected) => expect(niceMax(input)).toBeCloseTo(expected, 10));
});

describe("axisTicks", () => {
  it("goes from 0 to the nice ceiling in equal steps", () => {
    expect(axisTicks(0.0041)).toEqual([0, 0.00125, 0.0025, 0.00375, 0.005]);
  });
});

describe("formatUsdAxis", () => {
  it("keeps tiny costs readable", () => {
    expect(formatUsdAxis(0)).toBe("$0");
    expect(formatUsdAxis(0.00125)).toBe("$0.0013");
    expect(formatUsdAxis(0.005)).toBe("$0.005");
    expect(formatUsdAxis(2.5)).toBe("$2.5");
    expect(formatUsdAxis(120)).toBe("$120");
  });
});

describe("compactNumber", () => {
  it("compacts from 10K up", () => {
    expect(compactNumber(950)).toBe("950");
    expect(compactNumber(9_500)).toBe("9,500");
    expect(compactNumber(12_400)).toBe("12.4K");
    expect(compactNumber(3_200_000)).toBe("3.2M");
  });
});

describe("shortDate", () => {
  it("does not shift the day across timezones", () => {
    expect(shortDate("2026-10-22")).toBe("Oct 22");
    expect(shortDate("2026-01-01")).toBe("Jan 1");
  });
});

describe("barPercent", () => {
  it("scales to the axis and keeps tiny values visible", () => {
    expect(barPercent(5, 10)).toBe(50);
    expect(barPercent(0.0001, 10)).toBe(1.5);
    expect(barPercent(0, 10)).toBe(0);
  });
});

describe("labelIndexes", () => {
  it("shows every label when there is room", () => {
    expect(labelIndexes(5)).toEqual([0, 1, 2, 3, 4]);
  });
  it("thins out long ranges but always keeps the last day", () => {
    const idx = labelIndexes(30);
    expect(idx.length).toBeLessThanOrEqual(7);
    expect(idx[idx.length - 1]).toBe(29);
    expect(new Set(idx).size).toBe(idx.length);
  });
});
