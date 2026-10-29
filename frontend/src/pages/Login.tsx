import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router";

import { ApiError, login } from "../api/client";

export function Login() {
  const navigate = useNavigate();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      navigate("/settings/models");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "无法连接后端");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="av-root av-login">
      <form onSubmit={submit}>
        <h1><span className="av-rail__logo" style={{ margin: 0 }}>A</span>Avelis</h1>
        <label className="av-field">
          <span className="av-label">用户名</span>
          <input className="av-input" autoComplete="username" value={username}
            onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <label className="av-field">
          <span className="av-label">口令</span>
          <input className="av-input" type="password" autoComplete="current-password" value={password}
            onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
        <button className="av-btn av-btn--primary av-btn--lg" type="submit" disabled={busy}>登录</button>
      </form>
    </div>
  );
}
