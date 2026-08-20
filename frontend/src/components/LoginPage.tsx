import { useState } from "react";
import { BrandMark } from "./BrandMark";
import { ApiError, login } from "../api";
import { Moon, Sun } from "../icons";
import { useTheme } from "../hooks";
import { LoginHeart } from "./LoginHeart";
import type { Identity } from "../types";

interface Props {
  /** Called with the resolved identity. Persisting it is App's job. */
  onLogin: (identity: Identity) => void;
}

export function LoginPage({ onLogin }: Props) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, toggleTheme] = useTheme();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      onLogin(await login(email, password));
    } catch (err) {
      // The server's message, shown as-is. A failed fetch is different: it
      // means no backend at all, which the browser reports only as the
      // uselessly generic "Failed to fetch".
      setError(
        err instanceof ApiError
          ? err.message
          : "Couldn't reach the API. Is the backend running?",
      );
      setSubmitting(false);
    }
  }

  return (
    <div className="login-page">
      <button
        type="button"
        className="theme-toggle login-theme"
        aria-label={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
        onClick={toggleTheme}
      >
        {theme === "dark" ? <Sun /> : <Moon />}
      </button>

      <div className="login-stack">
        <LoginHeart />

        <div className="login-card">
          <div className="brand login-brand">
            <span className="brand-mark" aria-hidden="true"><BrandMark size={36} /></span>
            <span className="brand-name">Mel</span>
          </div>
          <p className="login-tagline">Sign in to the employee directory</p>

          <form onSubmit={submit}>
            <label className="login-field">
              <span>Work email</span>
              <input
                type="email"
                autoComplete="username"
                autoFocus
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
            </label>
            <label className="login-field">
              <span>Password</span>
              <input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </label>

            {error && <p className="login-error" role="alert">{error}</p>}

            <button type="submit" className="btn btn-primary login-submit" disabled={submitting}>
              {submitting ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
