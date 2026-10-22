import { afterEach, describe, expect, it, vi } from "vitest";
import { clearToken, getToken, isExpired, notifyUnauthorized, onUnauthorized, setToken, tokenExpiry } from "./auth";

// A JWT's payload is base64url JSON. Build one the way a server would (without a real signature).
const b64url = (obj: object) => btoa(JSON.stringify(obj)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const jwt = (payload: object) => `${b64url({ alg: "HS256", typ: "JWT" })}.${b64url(payload)}.signature`;

afterEach(() => vi.unstubAllGlobals());

describe("token expiry", () => {
  it("reads exp from the payload", () => {
    expect(tokenExpiry(jwt({ sub: "u", exp: 1_900_000_000 }))).toBe(1_900_000_000);
  });

  it("knows a token is expired, or not, at a given moment", () => {
    const token = jwt({ sub: "u", exp: 1_000 });
    expect(isExpired(token, 999_000)).toBe(false);
    expect(isExpired(token, 1_000_000)).toBe(true);
  });

  it.each(["", "abc", "a.b.c", "a..c", jwt({ sub: "u" })])("treats %j as expired (malformed or no exp)", (token) => {
    expect(isExpired(token)).toBe(true);
  });

  it("handles payloads that need base64 padding", () => {
    expect(tokenExpiry(jwt({ exp: 12345, pad: "xy" }))).toBe(12345);
  });
});

describe("token storage", () => {
  it("does not crash when localStorage is unavailable", () => {
    vi.stubGlobal("localStorage", undefined);
    expect(getToken()).toBeNull();
    expect(() => setToken("x")).not.toThrow();
    expect(() => clearToken()).not.toThrow();
  });

  it("does not crash when localStorage throws (private mode)", () => {
    const boom = () => {
      throw new Error("blocked");
    };
    vi.stubGlobal("localStorage", { getItem: boom, setItem: boom, removeItem: boom });
    expect(getToken()).toBeNull();
    expect(() => setToken("x")).not.toThrow();
  });

  it("stores, reads and clears the token", () => {
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    });
    setToken("abc");
    expect(getToken()).toBe("abc");
    clearToken();
    expect(getToken()).toBeNull();
  });
});

describe("the unauthorized hook", () => {
  it("calls the registered handler, and stops after it is unregistered", () => {
    const fn = vi.fn();
    const off = onUnauthorized(fn);
    notifyUnauthorized();
    expect(fn).toHaveBeenCalledTimes(1);
    off();
    notifyUnauthorized();
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it("is a no-op when nobody is listening", () => {
    expect(() => notifyUnauthorized()).not.toThrow();
  });
});
