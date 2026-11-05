// 登录：左侧淡蓝点阵面板上是大标题与三张错落的产品示意卡（分析结论、口径、血缘路径），右侧白底登录表单。
// 会话过期、被锁定、需要改密码都在这里给出明确去向。动效在“减少动态效果”下全部停止（见 login.css）。
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { api, ApiError, login, token, unwrap } from "../api/client";
import { errorText } from "../api/hooks";
import { Hint, Icon } from "../components/Icon";
import { LogoMark } from "../components/Logo";
import { Tile } from "../components/ui";
import { currentLanguage, t } from "../i18n";
import { LanguageSelect } from "../i18n/LanguageMenu";
import "../styles/login.css";

/** 示意数字的缩写写法跟随语言：中文 1284.6 万，英文 12.8 M；数值与单位分开排版。 */
function compactParts(n: number): { value: string; unit: string } {
  const parts = new Intl.NumberFormat(currentLanguage(), { notation: "compact", maximumFractionDigits: 1 }).formatToParts(n);
  return { value: parts.filter((p) => p.type !== "compact" && p.type !== "literal").map((p) => p.value).join(""),
    unit: parts.filter((p) => p.type === "compact").map((p) => p.value).join("") };
}

/** 品牌标记：蓝色圆角块里的白色“A”（块内把 Logo 用到的令牌换成白色）。 */
function BrandMark({ size = 34 }: { size?: number }) {
  return <span className="av-login__mark" style={{ width: size, height: size }} aria-hidden="true"><LogoMark size={Math.round(size * 0.62)} /></span>;
}

/** 产品拼贴：分析结论、口径卡、血缘路径三张示意卡错落叠放，说明产品在做什么。数字与名称都是中性的示意。 */
function Collage() {
  const bars = [40, 55, 48, 62, 58, 72, 66, 80, 96];
  const kpi = compactParts(12_846_000);
  return (
    <div className="av-login__collage" aria-hidden="true">
      <div className="av-login__c av-login__c--metric">
        <div className="av-login__c-hd"><Tile icon="chart" tone="catalog" size="sm" />{t("口径")}</div>
        <b className="av-login__metric">39</b><small>{t("已发布")}</small>
      </div>
      <div className="av-login__c av-login__c--answer">
        <div className="av-login__c-hd"><BrandMark size={22} />{t("分析完成")}
          <span className="av-login__ok"><Icon name="check" small />{t("来源已校验")}</span></div>
        <div className="av-login__kpi">
          <div><small>{t("本期")}</small><b>{kpi.value}<em>{kpi.unit}</em></b></div>
          <div><small>{t("环比")}</small><b className="is-up">+12.4%</b></div>
          <div className="av-login__spark-bars">{bars.map((h, i) => <i key={i} style={{ height: `${h}%` }} />)}</div>
        </div>
        <p>{t("变化主要来自 ")}<mark>{t("两个成员")}</mark>{t(" 的增长，合计贡献 83%")}{t("，口径与数据均可追溯")}</p>
      </div>
      <div className="av-login__c av-login__c--lineage">
        <div className="av-login__c-hd"><Tile icon="lineage" tone="lineage" size="sm" />{t("核心指标的来源")}</div>
        <svg viewBox="0 0 340 90" className="av-login__graph">
          <rect x="4" y="12" width="84" height="26" rx="7" className="is-node" /><text x="14" y="29">src_events</text>
          <rect x="4" y="52" width="84" height="26" rx="7" className="is-node" /><text x="14" y="69">src_users</text>
          <rect x="128" y="32" width="92" height="26" rx="7" className="is-mid" /><text x="138" y="49" className="is-mid">dwd_fact</text>
          <rect x="250" y="32" width="86" height="26" rx="7" className="is-end" /><text x="262" y="49" className="is-end">{t("核心指标")}</text>
          <path d="M88 25 C110 25 106 45 128 45" className="is-edge" />
          <path d="M88 65 C110 65 106 45 128 45" className="is-path" />
          <path d="M220 45 L250 45" className="is-path" />
        </svg>
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
      {/* 左：品牌色面板（标题 + 产品示意）；右：登录表单。两栏同高，窄屏时品牌面板收成顶部色带 */}
      <section className="av-login__brand" aria-label="Avelis">
        <span className="av-login__logo"><BrandMark /><b>Avelis</b></span>
        <div className="av-login__brand-body">
          <h1>{t("让每一条数据")}<br />{t("都有出处")}</h1>
          <Collage />
        </div>
      </section>
      <main className="av-login__side">
        <div className="av-login__side-top"><LanguageSelect className="av-login__lang" /></div>
        <section className="av-login__panel">
          {changing ? <PasswordForm target={target} onBack={() => setChanging(false)} /> : (
          <form className="av-login__card" onSubmit={submit} noValidate>
            <div className="av-login__card-hd">
              <h2>{t("登录")}</h2>
              <p>{t("使用你的 Avelis 账号")}</p>
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
