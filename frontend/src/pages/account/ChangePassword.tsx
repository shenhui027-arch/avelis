// 修改密码：首次登录或被管理员重置后强制进入；成功后其他会话失效，当前会话换用新令牌。
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router";

import { api, token, unwrap } from "../../api/client";
import { errorText } from "../../api/hooks";
import { LogoMark } from "../../components/Logo";
import { useMe } from "../../layout/Shell";

export function ChangePassword() {
  const me = useMe();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const from = (useLocation().state as { from?: string } | null)?.from ?? "/";
  const forced = !!me.data?.must_change_password;
  const [form, setForm] = useState({ current: "", next: "", confirm: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = form.confirm.length > 0 && form.next !== form.confirm;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (form.next !== form.confirm) return;
    setBusy(true);
    setError(null);
    try {
      const out = await unwrap(api.POST("/api/me/password", {
        body: { current_password: form.current, new_password: form.next } }));
      token.issue(out);
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      navigate(forced ? from : "/", { replace: true });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="av-root av-auth">
      <form className="av-auth__card" onSubmit={submit}>
        <div className="av-auth__brand"><LogoMark size={28} /><span>Avelis</span></div>
        <div className="av-auth__head">
          <h1>{forced ? "设置新密码" : "修改密码"}</h1>
          <p>{forced ? "首次登录或密码被重置，需先设置新密码" : "修改后，其他设备上的登录将失效"}</p>
        </div>
        <label className="av-field"><span className="av-label">当前密码</span>
          <input className="av-input" type="password" autoComplete="current-password" required autoFocus
            value={form.current} onChange={(e) => setForm({ ...form, current: e.target.value })} /></label>
        <label className="av-field"><span className="av-label">新密码</span>
          <input className="av-input" type="password" autoComplete="new-password" required minLength={8}
            value={form.next} onChange={(e) => setForm({ ...form, next: e.target.value })} />
          <span className="av-help">至少 8 位，不能包含账号</span></label>
        <label className="av-field"><span className="av-label">确认新密码</span>
          <input className="av-input" type="password" autoComplete="new-password" required aria-invalid={mismatch}
            value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} />
          {mismatch ? <span className="av-help av-help--danger">两次输入不一致</span> : null}</label>
        {error ? <div className="av-banner av-banner--danger" role="alert">{error}</div> : null}
        <div className="av-auth__actions">
          {forced ? null : <button type="button" className="av-btn av-btn--secondary av-btn--lg" onClick={() => navigate(-1)}>取消</button>}
          <button className="av-btn av-btn--primary av-btn--lg" type="submit"
            disabled={busy || form.next.length < 8 || form.next !== form.confirm}>{busy ? "保存中…" : "保存"}</button>
        </div>
      </form>
    </div>
  );
}
