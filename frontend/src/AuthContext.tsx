import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, getMe, login as apiLogin, signup as apiSignup } from "./api";
import { clearToken, getToken, isExpired, onUnauthorized, setToken } from "./auth";
import type { User } from "./types";

/**
 * Who is signed in?
 *   checking       we have a saved token and are asking the server if it still works
 *   anonymous      nobody is signed in: show the login screen
 *   authenticated  signed in: show the app
 *   unreachable    a saved token exists but the server could not be reached (not a logout!)
 */
type Status = "checking" | "anonymous" | "authenticated" | "unreachable";

interface AuthValue {
  status: Status;
  user: User | null;
  /** A one-line reason shown on the login screen ("Your session expired..."). */
  notice: string | null;
  login: (email: string, password: string) => Promise<void>;
  signup: (email: string, password: string) => Promise<void>;
  logout: () => void;
  retry: () => void;
}

const AuthContext = createContext<AuthValue | null>(null);
const EXPIRED = "Your session expired. Please sign in again.";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>(() => (getToken() ? "checking" : "anonymous"));
  const [user, setUser] = useState<User | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const dropSession = useCallback((message: string | null) => {
    clearToken();
    setUser(null);
    setNotice(message);
    setStatus("anonymous");
  }, []);

  // On load: if there is a saved token, ask the server whether it still works.
  const verify = useCallback(async () => {
    const token = getToken();
    if (!token) return setStatus("anonymous");
    if (isExpired(token)) return dropSession(EXPIRED); // no need to ask: we can see it has expired
    setStatus("checking");
    try {
      setUser(await getMe());
      setStatus("authenticated");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) dropSession(EXPIRED);
      else setStatus("unreachable"); // the server is down; keep the token, the user is not logged out
    }
  }, [dropSession]);

  useEffect(() => {
    void verify();
  }, [verify]);

  // Any request anywhere that comes back 401 ends the session, once, in one place.
  useEffect(() => onUnauthorized(() => dropSession(EXPIRED)), [dropSession]);

  const accept = useCallback((token: string, who: User) => {
    setToken(token);
    setUser(who);
    setNotice(null);
    setStatus("authenticated");
  }, []);

  const value = useMemo<AuthValue>(
    () => ({
      status,
      user,
      notice,
      login: async (email, password) => {
        const res = await apiLogin(email, password);
        accept(res.token, res.user);
      },
      signup: async (email, password) => {
        const res = await apiSignup(email, password);
        accept(res.token, res.user);
      },
      logout: () => {
        dropSession(null);
        window.location.hash = ""; // the next person starts on a new chat, not on someone's old URL
      },
      retry: () => void verify(),
    }),
    [status, user, notice, accept, dropSession, verify],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
