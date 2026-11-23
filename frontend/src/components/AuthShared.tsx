import { useEffect, useId, useRef, useState, type KeyboardEvent, type RefObject } from "react";

import type { Schemas } from "../api/client";
import { currentLanguage, t } from "../i18n";
import { LanguageSelect } from "../i18n/LanguageMenu";
import { setThemePref, useThemePref } from "../theme";
import { Icon } from "./Icon";
import { LogoMark } from "./Logo";
import { Tile } from "./ui";

export type AuthBranding = Partial<Schemas["BrandingSettings"]> | null | undefined;

export function safeBrandingUrl(url?: string | null): string | null {
  if (!url) return null;
  const trimmed = url.trim();
  if (!trimmed) return null;
  if (trimmed.startsWith("/") && !trimmed.startsWith("//")) return trimmed;
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === "https:" || parsed.protocol === "http:") return parsed.toString();
  } catch {
    return null;
  }
  return null;
}

/** 示意数字的缩写写法跟随语言：中文 1284.6 万，英文 12.8 M；数值与单位分开排版。 */
function compactParts(n: number): { value: string; unit: string } {
  const parts = new Intl.NumberFormat(currentLanguage(), { notation: "compact", maximumFractionDigits: 1 }).formatToParts(n);
  return {
    value: parts.filter((p) => p.type !== "compact" && p.type !== "literal").map((p) => p.value).join(""),
    unit: parts.filter((p) => p.type === "compact").map((p) => p.value).join(""),
  };
}

export function BrandMark({ size = 34, logoUrl, brandName = "Avelis" }: { size?: number; logoUrl?: string | null; brandName?: string }) {
  const [broken, setBroken] = useState(false);
  const safeLogo = !broken ? safeBrandingUrl(logoUrl) : null;
  if (safeLogo) {
    return (
      <span className="av-login__mark av-login__mark--custom" style={{ width: size, height: size }}>
        <img src={safeLogo} alt={brandName} onError={() => setBroken(true)} />
      </span>
    );
  }
  return (
    <span className="av-login__mark" style={{ width: size, height: size }} aria-hidden="true">
      <LogoMark size={Math.round(size * 0.62)} />
    </span>
  );
}

/** 锁定时长倒计时（MM:SS）。 */
export function useCountdown(initial = 0) {
  const [remaining, setRemaining] = useState(Math.max(0, Math.ceil(initial)));
  useEffect(() => {
    if (remaining <= 0) return;
    const timer = window.setInterval(() => {
      setRemaining((prev) => (prev <= 1 ? 0 : prev - 1));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [remaining > 0]);
  const start = (seconds: number) => setRemaining(Math.max(0, Math.ceil(seconds)));
  const reset = () => setRemaining(0);
  const mins = String(Math.floor(remaining / 60)).padStart(2, "0");
  const secs = String(remaining % 60).padStart(2, "0");
  return { remaining, start, reset, formatted: `${mins}:${secs}` };
}

/** 右上角工具条：主题快捷切换 + 语言切换。 */
export function AuthTopControls() {
  const pref = useThemePref();
  const isDark = pref === "dark" || (pref === "system" && typeof window !== "undefined" && !window.matchMedia("(prefers-color-scheme: light)").matches);
  const toggleTheme = () => setThemePref(isDark ? "light" : "dark");
  return (
    <div className="av-login__side-top">
      <button
        type="button"
        className="av-login__theme-btn"
        aria-label={isDark ? t("切换到浅色主题") : t("切换到深色主题")}
        onClick={toggleTheme}
      >
        <Icon name={isDark ? "sun" : "moon"} small />
        <span>{isDark ? t("浅色") : t("深色")}</span>
      </button>
      <LanguageSelect className="av-login__lang" />
    </div>
  );
}

/** 带前置图标与显隐切换的密码输入框。 */
export function PasswordInput({
  id,
  value,
  onChange,
  placeholder,
  autoComplete,
  autoFocus,
  disabled,
  invalid,
  describedBy,
  inputRef,
  icon = "lock",
  required = true,
  minLength,
  name,
}: {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  autoComplete?: string;
  autoFocus?: boolean;
  disabled?: boolean;
  invalid?: boolean;
  describedBy?: string;
  inputRef?: RefObject<HTMLInputElement | null>;
  icon?: "lock" | "key";
  required?: boolean;
  minLength?: number;
  name?: string;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <span className={`av-login__input${invalid ? " is-invalid" : ""}`}>
      <Icon name={icon} small />
      <input
        ref={inputRef}
        id={id}
        name={name}
        type={visible ? "text" : "password"}
        autoComplete={autoComplete}
        autoFocus={autoFocus}
        disabled={disabled}
        required={required}
        minLength={minLength}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
      <button
        type="button"
        className="av-login__reveal"
        aria-label={visible ? t("隐藏密码") : t("显示密码")}
        aria-pressed={visible}
        disabled={disabled}
        onClick={() => setVisible((v) => !v)}
      >
        <Icon name={visible ? "eyeOff" : "eye"} small />
      </button>
    </span>
  );
}

export function passwordPolicyChecks(password: string, username?: string) {
  const hasMinLength = password.length >= 8;
  const hasDistinctChars = new Set(password).size >= 4;
  const cleanUser = username?.trim().toLowerCase() ?? "";
  const excludesUsername = !cleanUser || !password.toLowerCase().includes(cleanUser);
  return {
    hasMinLength,
    hasDistinctChars,
    excludesUsername,
    valid: hasMinLength && hasDistinctChars && excludesUsername,
  };
}

/** 实时密码规则校验条：对齐后端 8 位 + 4 个不同字符 + 不含账号策略。 */
export function PasswordRulesBar({
  password,
  confirm,
  username,
}: {
  password: string;
  confirm?: string;
  username?: string;
}) {
  const rules = passwordPolicyChecks(password, username);
  const hasConfirm = confirm !== undefined && confirm.length > 0;
  const matches = !hasConfirm || password === confirm;
  const passedCount = [
    rules.hasMinLength,
    rules.hasDistinctChars,
    password.length > 0 && rules.excludesUsername,
    password.length >= 12 && new Set(password).size >= 6,
  ].filter(Boolean).length;
  const tone = password.length === 0 ? "idle" : passedCount >= 3 && rules.valid ? "ok" : passedCount >= 2 ? "warn" : "danger";

  const items = [
    { ok: rules.hasMinLength, label: t("至少 8 个字符") },
    { ok: rules.hasDistinctChars, label: t("至少 4 个不同字符") },
    { ok: password.length > 0 && rules.excludesUsername, label: t("不包含账号名") },
    ...(confirm !== undefined ? [{ ok: hasConfirm && matches, label: t("两次输入一致") }] : []),
  ];

  return (
    <div className="av-pw-rules" aria-live="polite">
      <div className={`av-pw-rules__meter is-${tone}`} aria-hidden="true">
        {[0, 1, 2, 3].map((idx) => (
          <span key={idx} className={idx < passedCount ? "is-on" : undefined} />
        ))}
      </div>
      <ul className="av-pw-rules__list">
        {items.map((item) => (
          <li key={item.label} className={item.ok ? "is-met" : undefined}>
            <Icon name="check" small />
            <span>{item.label}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const STAGE_STEPS = [
  { key: "lineage", num: "01", title: t("血缘溯源"), tone: "lineage" },
  { key: "metric", num: "02", title: t("口径校验"), tone: "catalog" },
  { key: "attribution", num: "03", title: t("归因结论"), tone: "ask" },
] as const;

/** 左侧建筑感数据叙事舞台：血缘溯源 → 口径校验 → 归因结论交互联动预览。 */
export function AuthStage({
  branding,
  initialStep = 0,
}: {
  branding?: AuthBranding;
  initialStep?: number;
}) {
  const [step, setStep] = useState(Math.min(2, Math.max(0, initialStep)));
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const baseId = useId();
  const brandName = branding?.brand_name?.trim() || "Avelis";
  const bars = [40, 55, 48, 62, 58, 72, 66, 80, 96];
  const kpi = compactParts(12_846_000);

  const handleTabKeyDown = (e: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (e.key === "ArrowRight") next = (index + 1) % STAGE_STEPS.length;
    else if (e.key === "ArrowLeft") next = (index + STAGE_STEPS.length - 1) % STAGE_STEPS.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = STAGE_STEPS.length - 1;
    else return;
    e.preventDefault();
    setStep(next);
    tabRefs.current[next]?.focus();
  };

  return (
    <section className="av-login__brand" aria-label={brandName}>
      <div className="av-login__brand-top">
        <span className="av-login__logo">
          <BrandMark logoUrl={branding?.brand_logo_url} brandName={brandName} />
          <b>{brandName}</b>
        </span>
        <span className="av-login__trust-chip">
          <i aria-hidden="true" />
          {t("可追溯数据问数")}
        </span>
      </div>

      <div className="av-login__brand-body">
        <header className="av-login__headline">
          <h1>
            {t("让每一条数据")}
            <br />
            {t("都有出处")}
          </h1>
          <p className="av-login__lead">{t("指标有定义、血缘有路径、结论有依据")}</p>
        </header>

        <div className="av-login__story">
          <div className="av-login__rail" role="tablist" aria-label={t("产品能力链")}>
            {STAGE_STEPS.map((item, idx) => {
              const active = step === idx;
              return (
                <button
                  key={item.key}
                  ref={(el) => { tabRefs.current[idx] = el; }}
                  type="button"
                  role="tab"
                  id={`${baseId}-tab-${item.key}`}
                  aria-controls={`${baseId}-panel-${item.key}`}
                  aria-selected={active}
                  tabIndex={active ? 0 : -1}
                  className={`av-login__step tone-${item.tone}${active ? " is-active" : ""}`}
                  onClick={() => setStep(idx)}
                  onKeyDown={(e) => handleTabKeyDown(e, idx)}
                >
                  <span className="av-login__step-num av-mono">{item.num}</span>
                  <span className="av-login__step-title">{item.title}</span>
                </button>
              );
            })}
          </div>

          <div
            className={`av-login__stage-card tone-${STAGE_STEPS[step].tone}`}
            role="tabpanel"
            id={`${baseId}-panel-${STAGE_STEPS[step].key}`}
            aria-labelledby={`${baseId}-tab-${STAGE_STEPS[step].key}`}
            tabIndex={0}
          >
            <div className="av-login__stage-chrome">
              <div className="av-login__stage-dots" aria-hidden="true">
                <span /><span /><span />
              </div>
              <span className="av-login__stage-label av-mono">
                {STAGE_STEPS[step].num} · {STAGE_STEPS[step].title}
              </span>
              <span className="av-login__ok">
                <Icon name="check" small />
                {step === 0 ? t("上游 100% 就绪") : step === 1 ? t("V3 · 已核验") : t("来源已校验")}
              </span>
            </div>

            {step === 0 ? (
              <div className="av-login__stage-pane" key="lineage">
                <div className="av-login__c-hd">
                  <Tile icon="lineage" tone="lineage" size="sm" />
                  <span>{t("核心指标的来源")}</span>
                  <span className="av-login__stage-tag av-mono">SQL DAG</span>
                </div>
                <svg viewBox="0 0 420 116" className="av-login__graph" aria-hidden="true">
                  <rect x="6" y="12" width="104" height="34" rx="9" className="is-node" />
                  <text x="16" y="27" className="is-tier">RAW</text>
                  <text x="16" y="39">src_events</text>

                  <rect x="6" y="68" width="104" height="34" rx="9" className="is-node" />
                  <text x="16" y="83" className="is-tier">RAW</text>
                  <text x="16" y="95">src_users</text>

                  <rect x="156" y="40" width="110" height="36" rx="9" className="is-mid" />
                  <text x="168" y="55" className="is-tier is-mid">DWD</text>
                  <text x="168" y="68" className="is-mid">dwd_fact</text>

                  <rect x="310" y="40" width="104" height="36" rx="9" className="is-end" />
                  <text x="322" y="55" className="is-tier is-end">METRIC</text>
                  <text x="322" y="68" className="is-end">{t("核心指标")}</text>

                  <path d="M110 29 C134 29 132 58 156 58" className="is-edge" />
                  <path d="M110 85 C134 85 132 58 156 58" className="is-path" />
                  <path d="M266 58 L310 58" className="is-path" />
                  <circle cx="156" cy="58" r="3" className="is-dot" />
                  <circle cx="310" cy="58" r="3" className="is-dot-end" />
                </svg>
                <div className="av-login__trace-strip av-mono">
                  <span>src_events.amt</span>
                  <Icon name="arrow" small />
                  <span>dwd_fact.net_amt</span>
                  <Icon name="arrow" small />
                  <b>metric_value</b>
                </div>
              </div>
            ) : null}

            {step === 1 ? (
              <div className="av-login__stage-pane" key="metric">
                <div className="av-login__metric-sheet">
                  <div className="av-login__metric-hero">
                    <div className="av-login__c-hd">
                      <Tile icon="chart" tone="catalog" size="sm" />
                      <span>{t("口径定义")}</span>
                    </div>
                    <div className="av-login__metric-num">
                      <b className="av-login__metric">39</b>
                      <small>{t("已发布")}</small>
                    </div>
                  </div>
                  <div className="av-login__formula av-mono">
                    <span>SUM(metric_amt) / COUNT(DISTINCT member_id)</span>
                  </div>
                </div>
                <div className="av-login__checks">
                  <div className="av-login__check-row">
                    <span>{t("口径表达式一致")}</span>
                    <span className="av-login__ok"><Icon name="check" small />{t("已核验")}</span>
                  </div>
                  <div className="av-login__check-row">
                    <span>{t("聚合粒度唯一")}</span>
                    <span className="av-login__ok"><Icon name="check" small />{t("已核验")}</span>
                  </div>
                  <div className="av-login__check-row">
                    <span>{t("上游表健康")}</span>
                    <span className="av-login__ok"><Icon name="check" small />{t("正常")}</span>
                  </div>
                </div>
              </div>
            ) : null}

            {step === 2 ? (
              <div className="av-login__stage-pane" key="attribution">
                <div className="av-login__c-hd">
                  <BrandMark size={22} logoUrl={branding?.brand_logo_url} brandName={brandName} />
                  <span>{t("分析完成")}</span>
                  <span className="av-login__stage-tag av-mono">V3 · dwd_fact</span>
                </div>
                <div className="av-login__kpi">
                  <div>
                    <small>{t("本期")}</small>
                    <b>{kpi.value}<em>{kpi.unit}</em></b>
                  </div>
                  <div>
                    <small>{t("环比")}</small>
                    <b className="is-up">+12.4%</b>
                  </div>
                  <div className="av-login__spark-bars" aria-hidden="true">
                    {bars.map((h, i) => <i key={i} style={{ height: `${h}%` }} />)}
                  </div>
                </div>
                <p className="av-login__narrative">
                  {t("变化主要来自 ")}
                  <mark>{t("两个成员")}</mark>
                  {t(" 的增长，合计贡献 83%")}
                  {t("，口径与数据均可追溯")}
                </p>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </section>
  );
}
