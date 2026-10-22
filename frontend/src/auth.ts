// Where the login token lives in the browser, and what to do when it stops working.
//
// After login the server returns a signed token (a JWT). We keep it in localStorage and send
// it as `Authorization: Bearer <token>` on every request. This is the usual single-page-app
// pattern. Its known weakness: any script that runs in the page (XSS) could read it. Streambot
// defends against that where it matters (model output is rendered without raw HTML or images,
// links are safe) and the token expires on its own (JWT_EXPIRES_DAYS, default 7).

const TOKEN_KEY = "streambot:token";

// localStorage can throw (private mode, blocked site data) or be missing: never let that crash the app.
export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* the user just has to sign in again next time */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing to do */
  }
}

/**
 * The `exp` claim of a JWT (seconds since 1970), read WITHOUT verifying it. A token is
 * header.payload.signature, and the payload is plain base64url JSON. Only the server can verify
 * the signature; we read `exp` just to avoid sending a request we already know will be refused.
 */
export function tokenExpiry(token: string): number | null {
  try {
    const payload = token.split(".")[1];
    if (!payload) return null;
    const padded = payload.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(payload.length / 4) * 4, "=");
    const exp = JSON.parse(atob(padded)).exp;
    return typeof exp === "number" ? exp : null;
  } catch {
    return null;
  }
}

/** True if the token is malformed or past its expiry. */
export function isExpired(token: string, nowMs: number = Date.now()): boolean {
  const exp = tokenExpiry(token);
  return exp === null || exp * 1000 <= nowMs;
}

// Any API call that comes back 401 means "your login no longer works". Instead of every
// component handling it, api.ts calls notifyUnauthorized() and the auth context logs out once.
let handler: (() => void) | null = null;

export function onUnauthorized(fn: () => void): () => void {
  handler = fn;
  return () => {
    if (handler === fn) handler = null;
  };
}

export function notifyUnauthorized(): void {
  handler?.();
}
