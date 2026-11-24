// 登录：左侧建筑感数据叙事舞台（血缘溯源 → 口径校验 → 归因结论联动预览），右侧商业化登录/改密/2FA 表单。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { api, ApiError, login, token, unwrap, verifyTotpLogin } from "../api/client";
import { errorText, useAuthConfig } from "../api/hooks";
import {
  AuthStage, AuthTopControls, PasswordInput, PasswordRulesBar, passwordPolicyChecks, safeBrandingUrl, useCountdown,
} from "../components/AuthShared";
import { Hint, Icon } from "../components/Icon";
import { t } from "../i18n";
import "../styles/login.css";

/** 登录卡片上的改密：先用当前密码登录，再改密并换用新凭证；不需要先进入系统。 */
function PasswordForm({ target, onBack }: { target: string; onBack: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ username: "", current: "", next: "", confirm: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lockout = useCountdown(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const mismatch = form.confirm.length > 0 && form.next !== form.confirm;
  const rules = passwordPolicyChecks(form.next, form.username);
  const setField = (key: keyof typeof form) => (val: string) => setForm((prev) => ({ ...prev, [key]: val }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!rules.valid || form.next !== form.confirm || lockout.remaining > 0) return;
    setBusy(true);
    setError(null);
    try {
      await login(form.username, form.current);
      let issued;
      try {
        issued = await unwrap(api.POST("/api/me/password", {
          body: { current_password: form.current, new_password: form.next },
        }));
      } catch (e) {
        if (alive.current) token.clear();
        throw e;
      }
      if (!alive.current) return;
      token.issue(issued);
      queryClient.clear();
      navigate(target, { replace: true });
    } catch (e) {
      if (!alive.current) return;
      if (e instanceof ApiError) {
        if (e.status === 429 && e.retryAfter) lockout.start(e.retryAfter);
        setError(e.message);
      } else {
        setError(errorText(e));
      }
    } finally {
      if (alive.current) setBusy(false);
    }
  }

  return (
    <form className="av-login__card" onSubmit={submit} noValidate>
      <div className="av-login__card-hd">
        <h2>{t("修改密码")}</h2>
        <p>{t("修改后，其他设备上的登录将失效")}</p>
      </div>
      <label className="av-field">
        <span className="av-label">{t("账号")}</span>
        <span className="av-login__input">
          <Icon name="users" small />
          <input
            autoComplete="username"
            autoFocus
            value={form.username}
            placeholder={t("输入账号")}
            onChange={(e) => setField("username")(e.target.value)}
            required
          />
        </span>
      </label>
      <label className="av-field">
        <span className="av-label">{t("当前密码")}</span>
        <PasswordInput
          autoComplete="current-password"
          value={form.current}
          placeholder={t("输入当前密码")}
          onChange={setField("current")}
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
          onChange={setField("next")}
        />
      </label>
      <PasswordRulesBar password={form.next} confirm={form.confirm} username={form.username} />
      <label className="av-field">
        <span className="av-label">{t("确认新密码")}</span>
        <PasswordInput
          icon="key"
          autoComplete="new-password"
          value={form.confirm}
          placeholder={t("再次输入新密码")}
          invalid={mismatch}
          onChange={setField("confirm")}
        />
        {mismatch ? <span className="av-help av-help--danger">{t("两次输入不一致")}</span> : null}
      </label>
      {lockout.remaining > 0 ? (
        <div className="av-banner av-banner--warn" role="alert">
          <Icon name="clock" small />
          {t("尝试次数过多，请 {{time}} 后重试", { time: lockout.formatted })}
        </div>
      ) : error ? (
        <div className="av-banner av-banner--danger" role="alert">{error}</div>
      ) : null}
      <button
        className="av-login__submit"
        type="submit"
        disabled={busy || lockout.remaining > 0 || !form.username || !form.current || !rules.valid || form.next !== form.confirm}
      >
        {busy ? t("保存中…") : <>{t("保存并登录")}<Icon name="arrow" small /></>}
      </button>
      <p className="av-login__foot">
        <button type="button" className="av-login__switch" disabled={busy} onClick={onBack}>{t("返回登录")}</button>
      </p>
    </form>
  );
}

/** 2FA 验证码步骤：密码校验通过但账号开启了 TOTP 时触发。 */
function TotpChallengeForm({
  username,
  challengeToken,
  target,
  onCancel,
}: {
  username: string;
  challengeToken: string;
  target: string;
  onCancel: () => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const lockout = useCountdown(0);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const cleaned = code.replace(/\s+/g, "");
    if (!cleaned || lockout.remaining > 0) return;
    setBusy(true);
    setError(null);
    try {
      const result = await verifyTotpLogin(challengeToken, cleaned);
      queryClient.clear();
      navigate(result.mustChangePassword ? "/account/password" : target, {
        replace: true,
        state: result.mustChangePassword ? { from: target } : undefined,
      });
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.status === 429 && e.retryAfter) lockout.start(e.retryAfter);
        setError(e.message);
      } else {
        setError(t("无法连接后端"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="av-login__card" onSubmit={submit} noValidate>
      <div className="av-login__card-hd">
        <h2>{t("双因素认证")}</h2>
        <p>{t("请输入验证器应用中账号 {{username}} 的 6 位动态验证码", { username })}</p>
      </div>
      <label className="av-field">
        <span className="av-label">{t("动态验证码")}</span>
        <span className="av-login__input">
          <Icon name="shield" small />
          <input
            inputMode="numeric"
            autoComplete="one-time-code"
            autoFocus
            maxLength={8}
            value={code}
            placeholder={t("6 位数字验证码")}
            onChange={(e) => setCode(e.target.value)}
            required
          />
        </span>
      </label>
      {lockout.remaining > 0 ? (
        <div className="av-banner av-banner--warn" role="alert">
          <Icon name="clock" small />
          {t("验证失败次数过多，请 {{time}} 后重试", { time: lockout.formatted })}
        </div>
      ) : error ? (
        <div className="av-banner av-banner--danger" role="alert">{error}</div>
      ) : null}
      <button className="av-login__submit" type="submit" disabled={busy || lockout.remaining > 0 || code.trim().length < 6}>
        {busy ? t("验证中…") : <>{t("验证并登录")}<Icon name="arrow" small /></>}
      </button>
      <p className="av-login__foot">
        <button type="button" className="av-login__switch" disabled={busy} onClick={onCancel}>{t("返回登录")}</button>
      </p>
    </form>
  );
}

export function Login() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  const authConfig = useAuthConfig();
  const cfg = authConfig.data;
  useEffect(() => {
    document.title = `${t("登录")} · ${cfg?.branding?.brand_name || "Avelis"}`;
  }, [cfg?.branding?.brand_name]);

  const from = (useLocation().state as { from?: string } | null)?.from ?? params.get("from") ?? "/";
  const target = from.startsWith("/") && !from.startsWith("//") ? from : "/";
  const expired = params.get("reason") === "expired";
  const oidcError = params.get("error");
  const initialStep = Number(params.get("step") ?? "0") || 0;

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const [error, setError] = useState<{ text: string; locked: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [changing, setChanging] = useState(false);
  const [challenge, setChallenge] = useState<{ token: string; username: string } | null>(null);
  const lockout = useCountdown(0);
  const usernameRef = useRef<HTMLInputElement | null>(null);
  const passwordRef = useRef<HTMLInputElement | null>(null);

  const userMissing = attempted && !username.trim();
  const passMissing = attempted && !password;

  const brandName = cfg?.branding?.brand_name?.trim() || "Avelis";
  const localEnabled = cfg?.local_login_enabled ?? true;
  const oidc = cfg?.oidc?.enabled ? cfg.oidc : null;
  const termsUrl = safeBrandingUrl(cfg?.branding?.terms_url);
  const privacyUrl = safeBrandingUrl(cfg?.branding?.privacy_url);
  const subtitle = cfg?.branding?.login_subtitle?.trim() || t("使用你的 {{brand}} 账号", { brand: brandName });
  const oidcUrl = oidc
    ? `${oidc.authorize_url}?next=${encodeURIComponent(target)}&remember_me=${rememberMe ? "true" : "false"}`
    : null;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (lockout.remaining > 0) return;
    if (!username.trim() || !password) {
      setAttempted(true);
      setError(null);
      if (!username.trim()) usernameRef.current?.focus();
      else passwordRef.current?.focus();
      return;
    }
    setAttempted(false);
    setBusy(true);
    setError(null);
    try {
      const result = await login(username, password, { rememberMe });
      queryClient.clear();
      navigate(result.mustChangePassword ? "/account/password" : target, {
        replace: true,
        state: result.mustChangePassword ? { from: target } : undefined,
      });
    } catch (e) {
      if (e instanceof ApiError) {
        if ((e.totpRequired || e.code === "totp_required") && e.challengeToken) {
          setChallenge({ token: e.challengeToken, username });
          return;
        }
        if (e.status === 429) {
          if (e.retryAfter) lockout.start(e.retryAfter);
          setError({ text: e.message, locked: true });
        } else {
          setError({ text: e.message, locked: false });
        }
      } else {
        setError({ text: t("无法连接后端"), locked: false });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="av-root av-login">
      <AuthStage branding={cfg?.branding} initialStep={initialStep} />
      <main className="av-login__side">
        <AuthTopControls />
        <section className="av-login__panel">
          {changing ? (
            <PasswordForm target={target} onBack={() => setChanging(false)} />
          ) : challenge ? (
            <TotpChallengeForm
              username={challenge.username}
              challengeToken={challenge.token}
              target={target}
              onCancel={() => { setChallenge(null); setPassword(""); }}
            />
          ) : (
            <form className="av-login__card" onSubmit={submit} noValidate>
              <div className="av-login__card-hd">
                <h2>{t("登录")}</h2>
                <p>{subtitle}</p>
              </div>
              {oidcError && !error ? (
                <div className="av-banner av-banner--danger" role="alert">
                  <Icon name="info" small />
                  {t("单点登录失败（{{reason}}），请重试或联系管理员", { reason: oidcError })}
                </div>
              ) : null}
              {expired && !error && !oidcError ? (
                <div className="av-banner av-banner--info" role="status">
                  <Icon name="info" small />
                  {t("会话已过期或已在其他位置失效，请重新登录")}
                </div>
              ) : null}

              {oidc && oidcUrl ? (
                <a className="av-login__sso" href={oidcUrl}>
                  <Icon name="shield" small />
                  <span>{t("使用 {{provider}} 登录", { provider: oidc.provider_name })}</span>
                </a>
              ) : null}

              {oidc && localEnabled ? (
                <div className="av-login__divider" aria-hidden="true"><span>{t("或使用账号密码")}</span></div>
              ) : null}

              {localEnabled ? (
                <>
                  <label className="av-field">
                    <span className="av-label">{t("账号")}</span>
                    <span className={`av-login__input${userMissing ? " is-invalid" : ""}`}>
                      <Icon name="users" small />
                      <input
                        ref={usernameRef}
                        autoComplete="username"
                        autoFocus
                        value={username}
                        placeholder={t("输入账号")}
                        aria-invalid={userMissing || undefined}
                        aria-describedby={userMissing ? "login-username-error" : undefined}
                        onChange={(e) => setUsername(e.target.value)}
                        required
                      />
                    </span>
                    {userMissing ? <span id="login-username-error" className="av-help av-help--danger" role="alert">{t("请输入账号")}</span> : null}
                  </label>
                  <label className="av-field">
                    <span className="av-label">{t("密码")}</span>
                    <PasswordInput
                      inputRef={passwordRef}
                      autoComplete="current-password"
                      value={password}
                      placeholder={t("输入密码")}
                      invalid={passMissing}
                      describedBy={passMissing ? "login-password-error" : undefined}
                      onChange={setPassword}
                    />
                    {passMissing ? <span id="login-password-error" className="av-help av-help--danger" role="alert">{t("请输入密码")}</span> : null}
                  </label>
                  <div className="av-login__row">
                    <label className="av-login__remember">
                      <input
                        type="checkbox"
                        checked={rememberMe}
                        onChange={(e) => setRememberMe(e.target.checked)}
                      />
                      <span>{t("记住登录（7 天）")}</span>
                    </label>
                    <span className="av-login__forgot">
                      {t("忘记密码")}
                      <Hint text={t("忘记密码请联系管理员重置")} />
                    </span>
                  </div>
                  {lockout.remaining > 0 ? (
                    <div className="av-banner av-banner--warn" role="alert">
                      <Icon name="clock" small />
                      {t("账号暂时锁定，请 {{time}} 后再试", { time: lockout.formatted })}
                    </div>
                  ) : error ? (
                    <div className={`av-banner ${error.locked ? "av-banner--warn" : "av-banner--danger"}`} role="alert">
                      {error.text}
                    </div>
                  ) : null}
                  <button
                    className="av-login__submit"
                    type="submit"
                    disabled={busy || lockout.remaining > 0}
                  >
                    {busy ? t("登录中…") : <>{t("登录")}<Icon name="arrow" small /></>}
                  </button>
                  <p className="av-login__foot">
                    <button
                      type="button"
                      className="av-login__switch"
                      disabled={busy}
                      onClick={() => { setAttempted(false); setChanging(true); }}
                    >
                      {t("修改密码")}
                    </button>
                  </p>
                </>
              ) : !oidc ? (
                <div className="av-banner av-banner--info" role="status">
                  {t("本地登录已禁用，请联系管理员配置单点登录")}
                </div>
              ) : (
                <label className="av-login__remember">
                  <input
                    type="checkbox"
                    checked={rememberMe}
                    onChange={(e) => setRememberMe(e.target.checked)}
                  />
                  <span>{t("记住登录（7 天）")}</span>
                </label>
              )}

              {termsUrl || privacyUrl ? (
                <footer className="av-login__legal">
                  {termsUrl ? <a href={termsUrl} target="_blank" rel="noreferrer">{t("服务条款")}</a> : null}
                  {privacyUrl ? <a href={privacyUrl} target="_blank" rel="noreferrer">{t("隐私政策")}</a> : null}
                </footer>
              ) : null}
            </form>
          )}
        </section>
      </main>
    </div>
  );
}
