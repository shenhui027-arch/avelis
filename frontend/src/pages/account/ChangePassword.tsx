// 修改密码：统一登录页建筑感分栏语言；首次登录或被重置后强制进入，成功后换用新令牌并使其他会话失效。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { useLocation, useNavigate } from "react-router";

import { api, ApiError, token, unwrap } from "../../api/client";
import { errorText, useAuthConfig } from "../../api/hooks";
import {
  AuthStage, AuthTopControls, PasswordInput, PasswordRulesBar, passwordPolicyChecks, useCountdown,
} from "../../components/AuthShared";
import { Icon } from "../../components/Icon";
import { t } from "../../i18n";
import { useMe } from "../../layout/Shell";
import "../../styles/login.css";

export function ChangePassword() {
  const me = useMe();
  const authConfig = useAuthConfig();
  useEffect(() => {
    document.title = `${t("修改密码")} · ${authConfig.data?.branding?.brand_name || "Avelis"}`;
  }, [authConfig.data?.branding?.brand_name]);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const from = (useLocation().state as { from?: string } | null)?.from ?? "/";
  const forced = !!me.data?.must_change_password;
  const username = me.data?.username;

  const [form, setForm] = useState({ current: "", next: "", confirm: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lockout = useCountdown(0);

  const mismatch = form.confirm.length > 0 && form.next !== form.confirm;
  const rules = passwordPolicyChecks(form.next, username);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!rules.valid || form.next !== form.confirm || lockout.remaining > 0) return;
    setBusy(true);
    setError(null);
    try {
      const out = await unwrap(api.POST("/api/me/password", {
        body: { current_password: form.current, new_password: form.next },
      }));
      token.issue(out);
      await queryClient.invalidateQueries({ queryKey: ["me"] });
      navigate(forced ? from : "/", { replace: true });
    } catch (e) {
      if (e instanceof ApiError && e.status === 429 && e.retryAfter) {
        lockout.start(e.retryAfter);
      }
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="av-root av-login">
      <AuthStage branding={authConfig.data?.branding} />
      <main className="av-login__side">
        <AuthTopControls />
        <section className="av-login__panel">
          <form className="av-login__card" onSubmit={submit} noValidate>
            <div className="av-login__card-hd">
              <h1>{forced ? t("设置新密码") : t("修改密码")}</h1>
              <p>{forced ? t("首次登录或密码被重置，需先设置新密码") : t("修改后，其他设备上的登录将失效")}</p>
            </div>
            {forced ? (
              <div className="av-banner av-banner--warn" role="status">
                <Icon name="shield" small />
                {t("出于安全要求，完成密码设置后才能访问工作台")}
              </div>
            ) : null}
            <label className="av-field">
              <span className="av-label">{t("当前密码")}</span>
              <PasswordInput
                autoComplete="current-password"
                autoFocus
                value={form.current}
                placeholder={t("输入当前密码")}
                onChange={(v) => setForm((prev) => ({ ...prev, current: v }))}
              />
            </label>
            <label className="av-field">
              <span className="av-label">{t("新密码")}</span>
              <PasswordInput
                icon="key"
                autoComplete="new-password"
                value={form.next}
                placeholder={t("至少 8 位，不能包含账号")}
                minLength={8}
                onChange={(v) => setForm((prev) => ({ ...prev, next: v }))}
              />
            </label>
            <PasswordRulesBar password={form.next} confirm={form.confirm} username={username} />
            <label className="av-field">
              <span className="av-label">{t("确认新密码")}</span>
              <PasswordInput
                icon="key"
                autoComplete="new-password"
                value={form.confirm}
                placeholder={t("再次输入新密码")}
                invalid={mismatch}
                onChange={(v) => setForm((prev) => ({ ...prev, confirm: v }))}
              />
              {mismatch ? <span className="av-help av-help--danger">{t("两次输入不一致")}</span> : null}
            </label>
            {lockout.remaining > 0 ? (
              <div className="av-banner av-banner--warn" role="alert">
                <Icon name="clock" small />
                {t("修改密码尝试次数过多，请 {{time}} 后重试", { time: lockout.formatted })}
              </div>
            ) : error ? (
              <div className="av-banner av-banner--danger" role="alert">{error}</div>
            ) : null}
            <div className="av-login__actions">
              {forced ? null : (
                <button type="button" className="av-login__cancel" disabled={busy} onClick={() => navigate(-1)}>
                  {t("取消")}
                </button>
              )}
              <button
                className="av-login__submit"
                type="submit"
                disabled={busy || lockout.remaining > 0 || !form.current || !rules.valid || form.next !== form.confirm}
              >
                {busy ? t("保存中…") : <>{t("保存")}<Icon name="arrow" small /></>}
              </button>
            </div>
          </form>
        </section>
      </main>
    </div>
  );
}
