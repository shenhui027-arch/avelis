// 登录：左侧是标题与一张“带依据的答案”预览（产品本身就是主视觉），右侧是登录卡。不写口号与说明段落。
// 会话过期、被锁定、需要改密码都在这里给出明确去向。动效在“减少动态效果”下全部停止（见 login.css）。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { api, ApiError, login, token, unwrap } from "../api/client";
import { errorText } from "../api/hooks";
import { Hint, Icon } from "../components/Icon";
import { Logo, LogoMark } from "../components/Logo";
import { currentLanguage, t } from "../i18n";
import { LanguageSelect } from "../i18n/LanguageMenu";
import "../styles/login.css";

/** 示意数字的缩写写法跟随语言：中文 1284.6 万，英文 12.8 M；数值与单位分开排版。 */
function compactParts(n: number): { value: string; unit: string } {
  const parts = new Intl.NumberFormat(currentLanguage(), { notation: "compact", maximumFractionDigits: 1 }).formatToParts(n);
  return { value: parts.filter((p) => p.type !== "compact" && p.type !== "literal").map((p) => p.value).join(""),
    unit: parts.filter((p) => p.type === "compact").map((p) => p.value).join("") };
}

/** 产品预览：一条带依据的答案卡片。数字与名称是中性的示意。 */
function AnswerPreview() {
  const bars = [42, 55, 48, 61, 58, 72, 66, 80, 76, 88, 84, 96];
  const kpi = compactParts(12_846_000);
  return (
    <div className="av-login__preview" aria-hidden="true">
      <div className="av-login__q"><span className="av-login__avatar" />{t("本季度核心指标为什么上升？")}</div>
      <div className="av-login__answer">
        <div className="av-login__answer-hd">
          <LogoMark size={18} />
          <span>{t("分析完成")}</span>
          <span className="av-login__ok"><Icon name="check" small />{t("来源已校验")}</span>
        </div>
        <div className="av-login__kpi">
          <div><small>{t("本期")}</small><b>{kpi.value}<em>{kpi.unit}</em></b></div>
          <div><small>{t("环比")}</small><b className="is-up">+12.4%</b></div>
          <div className="av-login__spark-bars">{bars.map((h, i) => <i key={i} style={{ height: `${h}%` }} />)}</div>
        </div>
        <p>{t("变化主要来自 ")}<mark>{t("两个成员")}</mark>{t(" 的增长，合计贡献 83%")}<sup>D3</sup><sup>D7</sup>{t("，口径与数据均可追溯")}</p>
        <div className="av-login__chips"><span>{t("口径 V3 · 已审核")}</span><span>{t("列级血缘 12 条")}</span><span>{t("2 个仍待验证")}</span><span className="av-login__demo">{t("产品示意")}</span></div>
      </div>
    </div>
  );
}

/** 登录卡片上的改密：先用当前密码登录，再改密并换用新凭证；不需要先进入系统。 */
function PasswordForm({ target, onBack }: { target: string; onBack: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ username: "", current: "", next: "", confirm: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // 表单离开后，迟到的响应不能再清除、改写之后建立的会话，也不能再跳转
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;  // StrictMode 会先卸载再挂载一次，重新挂载时要恢复
    return () => { alive.current = false; };
  }, []);
  const mismatch = form.confirm.length > 0 && form.next !== form.confirm;
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (form.next !== form.confirm) return;
    setBusy(true);
    setError(null);
    try {
      await login(form.username, form.current);
      let issued;
      try {
        issued = await unwrap(api.POST("/api/me/password", {
          body: { current_password: form.current, new_password: form.next } }));
      } catch (e) {
        if (alive.current) token.clear();  // 改密没成功就不留下刚才的登录凭证
        throw e;
      }
      if (!alive.current) return;
      token.issue(issued);
      queryClient.clear();
      navigate(target, { replace: true });  // 与登录一致：回到登录前想去的页面（如 MCP 授权确认页）
    } catch (e) {
      if (alive.current) setError(e instanceof ApiError ? e.message : errorText(e));
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
        <span className="av-login__input"><Icon name="users" small />
          <input autoComplete="username" autoFocus value={form.username} placeholder={t("输入账号")} onChange={set("username")} required /></span>
      </label>
      <label className="av-field">
        <span className="av-label">{t("当前密码")}</span>
        <span className="av-login__input"><Icon name="lock" small />
          <input type="password" autoComplete="current-password" value={form.current} onChange={set("current")} required /></span>
      </label>
      <label className="av-field">
        <span className="av-label">{t("新密码")}</span>
        <span className="av-login__input"><Icon name="key" small />
          <input type="password" autoComplete="new-password" value={form.next} placeholder={t("至少 8 位，不能包含账号")}
            onChange={set("next")} required minLength={8} /></span>
      </label>
      <label className="av-field">
        <span className="av-label">{t("确认新密码")}</span>
        <span className="av-login__input"><Icon name="key" small />
          <input type="password" autoComplete="new-password" value={form.confirm} aria-invalid={mismatch} onChange={set("confirm")} required /></span>
        {mismatch ? <span className="av-help av-help--danger">{t("两次输入不一致")}</span> : null}
      </label>
      {error ? <div className="av-banner av-banner--danger" role="alert">{error}</div> : null}
      <button className="av-login__submit" type="submit"
        disabled={busy || !form.username || !form.current || form.next.length < 8 || form.next !== form.confirm}>
        {busy ? t("保存中…") : <>{t("保存并登录")}<Icon name="arrow" small /></>}
      </button>
      <p className="av-login__foot"><button type="button" className="av-login__switch" disabled={busy} onClick={onBack}>{t("返回登录")}</button></p>
    </form>
  );
}

export function Login() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  // 回到登录前想去的页面；直接打开登录页时进工作台
  const from = (useLocation().state as { from?: string } | null)?.from ?? params.get("from") ?? "/";
  const target = from.startsWith("/") && !from.startsWith("//") ? from : "/";
  const expired = params.get("reason") === "expired";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [reveal, setReveal] = useState(false);
  const [error, setError] = useState<{ text: string; locked: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [changing, setChanging] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await login(username, password);
      queryClient.clear();  // 换账号登录：丢弃上一个会话的缓存
      navigate(result.mustChangePassword ? "/account/password" : target,
        { replace: true, state: result.mustChangePassword ? { from: target } : undefined });
    } catch (e) {
      setError(e instanceof ApiError ? { text: e.message, locked: e.status === 429 } : { text: t("无法连接后端"), locked: false });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="av-root av-login">
      <header className="av-login__top">
        <Logo size={30} />
        <LanguageSelect className="av-login__lang" />
      </header>
      <main className="av-login__main">
        <section className="av-login__hero" aria-label="Avelis">
          <h1>{t("让每一条数据")}<br />{t("都有出处")}</h1>
          <AnswerPreview />
        </section>
        <section className="av-login__panel">
          {changing ? <PasswordForm target={target} onBack={() => setChanging(false)} /> : (
          <form className="av-login__card" onSubmit={submit} noValidate>
            <div className="av-login__card-hd">
              <h2>{t("登录")}</h2>
            </div>
            {expired && !error ? (
              <div className="av-banner av-banner--info" role="status"><Icon name="info" />{t("会话已过期或已在其他位置失效，请重新登录")}</div>
            ) : null}
            <label className="av-field">
              <span className="av-label">{t("账号")}</span>
              <span className="av-login__input"><Icon name="users" small />
                <input autoComplete="username" autoFocus value={username} placeholder={t("输入账号")}
                  onChange={(e) => setUsername(e.target.value)} required /></span>
            </label>
            <label className="av-field">
              <span className="av-label">{t("密码")}</span>
              <span className="av-login__input"><Icon name="lock" small />
                <input type={reveal ? "text" : "password"} autoComplete="current-password" value={password} placeholder={t("输入密码")}
                  onChange={(e) => setPassword(e.target.value)} required />
                <button type="button" className="av-login__reveal" aria-pressed={reveal}
                  onClick={() => setReveal(!reveal)}>{reveal ? t("隐藏") : t("显示")}</button></span>
            </label>
            {error ? (
              <div className={`av-banner ${error.locked ? "av-banner--warn" : "av-banner--danger"}`} role="alert">{error.text}</div>
            ) : null}
            <button className="av-login__submit" type="submit" disabled={busy || !username || !password}>
              {busy ? t("登录中…") : <>{t("登录")}<Icon name="arrow" small /></>}
            </button>
            <p className="av-login__foot">
              <button type="button" className="av-login__switch" disabled={busy} onClick={() => setChanging(true)}>{t("修改密码")}</button>
              <span className="av-login__forgot">{t("忘记密码")}<Hint text={t("忘记密码请联系管理员重置")} /></span>
            </p>
          </form>
          )}
        </section>
      </main>
    </div>
  );
}
