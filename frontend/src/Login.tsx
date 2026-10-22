import { useState, type FormEvent } from "react";
import { ApiError } from "./api";
import { useAuth } from "./AuthContext";
import { EyeIcon, EyeOffIcon, LogoMark } from "./Icons";

type Mode = "login" | "signup";
const MIN_PASSWORD = 8; // the server enforces the real rule; this just saves a round trip

export default function Login() {
  const { login, signup, notice } = useAuth();
  const [mode, setMode] = useState<Mode>("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signingUp = mode === "signup";

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    // Native browser validation is off (noValidate) so every message looks the same; the
    // server re-checks everything, these are just quick, friendly answers.
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]{2,}$/.test(email.trim())) {
      setError("Enter a valid email address.");
      return;
    }
    if (signingUp && password.length < MIN_PASSWORD) {
      setError(`Password must be at least ${MIN_PASSWORD} characters.`);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await (signingUp ? signup : login)(email.trim(), password);
      // On success the auth context flips to "authenticated" and this screen disappears.
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the server. Is the backend running?");
      setBusy(false);
    }
  }

  return (
    <div className="auth-page">
      <div className="auth-card">
        <LogoMark width={44} height={44} />
        <h1>{signingUp ? "Create your account" : "Welcome back"}</h1>
        <p className="auth-sub">
          {signingUp ? "Your chats are private to your account." : "Sign in to continue to Streambot."}
        </p>

        <div className="auth-tabs" role="tablist" aria-label="Sign in or create an account">
          <button type="button" role="tab" aria-selected={!signingUp} className={!signingUp ? "on" : ""} onClick={() => switchMode("login")}>
            Sign in
          </button>
          <button type="button" role="tab" aria-selected={signingUp} className={signingUp ? "on" : ""} onClick={() => switchMode("signup")}>
            Create account
          </button>
        </div>

        {notice && !error && <div className="auth-notice">{notice}</div>}
        {error && (
          <div className="auth-error" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={(e) => void submit(e)} noValidate>
          <label>
            <span>Email</span>
            <input
              type="email"
              name="email"
              autoComplete="email"
              autoFocus
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
          </label>
          <label>
            <span>Password</span>
            <div className="auth-password">
              <input
                type={show ? "text" : "password"}
                name="password"
                autoComplete={signingUp ? "new-password" : "current-password"}
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={signingUp ? `At least ${MIN_PASSWORD} characters` : "Your password"}
              />
              <button type="button" className="auth-eye" onClick={() => setShow((s) => !s)} aria-label={show ? "Hide password" : "Show password"}>
                {show ? <EyeOffIcon /> : <EyeIcon />}
              </button>
            </div>
          </label>
          <button type="submit" className="btn btn-primary auth-submit" disabled={busy || !email || !password}>
            {busy ? "Please wait…" : signingUp ? "Create account" : "Sign in"}
          </button>
        </form>

        <p className="auth-switch">
          {signingUp ? "Already have an account?" : "New to Streambot?"}{" "}
          <button type="button" className="link" onClick={() => switchMode(signingUp ? "login" : "signup")}>
            {signingUp ? "Sign in" : "Create an account"}
          </button>
        </p>
      </div>
    </div>
  );
}
