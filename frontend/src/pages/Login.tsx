// 登录：满屏极光背景 + 流动的血缘星图；左侧讲清产品（标题、三个价值点、一张“带依据的答案”预览），右侧玻璃登录卡。
// 会话过期、被锁定、需要改密码都在这里给出明确去向。动效在“减少动态效果”下全部停止（见 login.css）。
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { ApiError, login } from "../api/client";
import { Icon } from "../components/Icon";
import { Logo, LogoMark } from "../components/Logo";
import { currentLanguage, t } from "../i18n";
import { LanguageSelect } from "../i18n/LanguageMenu";
import "../styles/login.css";

const VALUES = [
  { icon: "lineage", title: "login.values.lineageTitle", text: "login.values.lineageText" },
  { icon: "shield", title: "login.values.governTitle", text: "login.values.governText" },
  { icon: "analysis", title: "login.values.groundedTitle", text: "login.values.groundedText" },
] as const;

// 背景星图：几列节点，曲线从左向右汇流，光点沿线流动。纯装饰。
const STARS = [
  [70, 120], [60, 300], [90, 480], [80, 640],
  [330, 210], [300, 420], [340, 590],
  [580, 300], [600, 520],
  [840, 410],
] as const;
const LINKS = [[0, 4], [1, 4], [1, 5], [2, 5], [3, 6], [2, 6], [4, 7], [5, 7], [5, 8], [6, 8], [7, 9], [8, 9]] as const;

function Constellation() {
  const curve = (a: readonly number[], b: readonly number[]) => {
    const mx = (a[0] + b[0]) / 2;
    return `M${a[0]} ${a[1]} C${mx} ${a[1]} ${mx} ${b[1]} ${b[0]} ${b[1]}`;
  };
  return (
    <svg className="av-login__stars" viewBox="0 0 900 760" preserveAspectRatio="xMidYMid slice" aria-hidden="true" focusable="false">
      {LINKS.map(([a, b], i) => {
        const d = curve(STARS[a], STARS[b]);
        return (
          <g key={i}>
            <path d={d} className="av-login__link" />
            <circle r="2.2" className="av-login__spark">
              <animateMotion dur={`${4 + (i % 4)}s`} begin={`${(i * 0.7) % 4}s`} repeatCount="indefinite" path={d} />
            </circle>
          </g>
        );
      })}
      {STARS.map(([x, y], i) => (
        <g key={i} className={i === STARS.length - 1 ? "av-login__star is-sink" : "av-login__star"}>
          <circle cx={x} cy={y} r={i === STARS.length - 1 ? 9 : 4.5} />
        </g>
      ))}
    </svg>
  );
}

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
      <div className="av-login__q"><span className="av-login__avatar" />{t("login.preview.question")}</div>
      <div className="av-login__answer">
        <div className="av-login__answer-hd">
          <LogoMark size={18} />
          <span>{t("login.preview.done")}</span>
          <span className="av-login__ok"><Icon name="check" small />{t("login.preview.sourcesChecked")}</span>
        </div>
        <div className="av-login__kpi">
          <div><small>{t("login.preview.current")}</small><b>{kpi.value}<em>{kpi.unit}</em></b></div>
          <div><small>{t("login.preview.mom")}</small><b className="is-up">+12.4%</b></div>
          <div className="av-login__spark-bars">{bars.map((h, i) => <i key={i} style={{ height: `${h}%` }} />)}</div>
        </div>
        <p>{t("login.preview.summaryBefore")}<mark>{t("login.preview.summaryMark")}</mark>{t("login.preview.summaryAfter")}<sup>D3</sup><sup>D7</sup>{t("login.preview.summaryTail")}</p>
        <div className="av-login__chips"><span>{t("login.preview.chipVersion")}</span><span>{t("login.preview.chipLineage")}</span><span>{t("login.preview.chipPending")}</span><span className="av-login__demo">{t("login.preview.chipDemo")}</span></div>
      </div>
    </div>
  );
}

export function Login() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [params] = useSearchParams();
  // 回到登录前想去的页面；直接打开登录页时进工作台
  const from = (useLocation().state as { from?: string } | null)?.from ?? params.get("from") ?? "/";
  const expired = params.get("reason") === "expired";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [reveal, setReveal] = useState(false);
  const [error, setError] = useState<{ text: string; locked: boolean } | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await login(username, password);
      queryClient.clear();  // 换账号登录：丢弃上一个会话的缓存
      const target = from.startsWith("/") && !from.startsWith("//") ? from : "/";
      navigate(result.mustChangePassword ? "/account/password" : target,
        { replace: true, state: result.mustChangePassword ? { from: target } : undefined });
    } catch (e) {
      setError(e instanceof ApiError ? { text: e.message, locked: e.status === 429 } : { text: t("login.unreachable"), locked: false });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="av-root av-login">
      <div className="av-login__aurora" aria-hidden="true"><i /><i /><i /></div>
      <div className="av-login__grid" aria-hidden="true" />
      <Constellation />
      <header className="av-login__top">
        <Logo size={34} />
        <span className="av-login__top-end">
          <span className="av-login__tag"><i />{t("login.tag")}</span>
          <LanguageSelect className="av-login__lang" />
        </span>
      </header>
      <main className="av-login__main">
        <section className="av-login__hero" aria-label="Avelis">
          <span className="av-login__eyebrow">{t("login.eyebrow")}</span>
          <h1>{t("login.titleLine1")}<br /><span>{t("login.titleLine2")}</span></h1>
          <p>{t("login.intro")}</p>
          <ul className="av-login__values">
            {VALUES.map((v) => (
              <li key={v.title}><span className="av-login__icon"><Icon name={v.icon} /></span>
                <div><b>{t(v.title)}</b><small>{t(v.text)}</small></div></li>
            ))}
          </ul>
          <AnswerPreview />
        </section>
        <section className="av-login__panel">
          <form className="av-login__card" onSubmit={submit} noValidate>
            <div className="av-login__card-hd">
              <LogoMark size={44} />
              <h2>{t("login.welcome")}</h2>
              <p>{t("login.subtitle")}</p>
            </div>
            {expired && !error ? (
              <div className="av-banner av-banner--info" role="status"><Icon name="info" />{t("login.expired")}</div>
            ) : null}
            <label className="av-field">
              <span className="av-label">{t("login.username")}</span>
              <span className="av-login__input"><Icon name="users" small />
                <input autoComplete="username" autoFocus value={username} placeholder={t("login.usernamePlaceholder")}
                  onChange={(e) => setUsername(e.target.value)} required /></span>
            </label>
            <label className="av-field">
              <span className="av-label">{t("login.password")}</span>
              <span className="av-login__input"><Icon name="lock" small />
                <input type={reveal ? "text" : "password"} autoComplete="current-password" value={password} placeholder={t("login.passwordPlaceholder")}
                  onChange={(e) => setPassword(e.target.value)} required />
                <button type="button" className="av-login__reveal" aria-pressed={reveal}
                  onClick={() => setReveal(!reveal)}>{reveal ? t("login.hide") : t("login.show")}</button></span>
            </label>
            {error ? (
              <div className={`av-banner ${error.locked ? "av-banner--warn" : "av-banner--danger"}`} role="alert">{error.text}</div>
            ) : null}
            <button className="av-login__submit" type="submit" disabled={busy || !username || !password}>
              {busy ? t("login.submitting") : <>{t("login.submit")}<Icon name="arrow" small /></>}
            </button>
            <p className="av-login__foot">{t("login.forgot")}</p>
          </form>
          <ul className="av-login__trust">
            <li><Icon name="shield" small />{t("login.trustAudit")}</li>
            <li><Icon name="db" small />{t("login.trustPrivate")}</li>
            <li><Icon name="key" small />{t("login.trustLocalModel")}</li>
          </ul>
        </section>
      </main>
    </div>
  );
}
