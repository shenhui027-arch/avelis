// 登录：满屏极光背景 + 流动的血缘星图；左侧讲清产品（标题、三个价值点、一张“带依据的答案”预览），右侧玻璃登录卡。
// 会话过期、被锁定、需要改密码都在这里给出明确去向。动效在“减少动态效果”下全部停止（见 login.css）。
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { ApiError, login } from "../api/client";
import { Icon } from "../components/Icon";
import { Logo, LogoMark } from "../components/Logo";
import "../styles/login.css";

const VALUES = [
  { icon: "lineage", title: "列级血缘", text: "从源表到指标，每一列的来龙去脉自动采集、随时可查" },
  { icon: "shield", title: "口径治理", text: "指标口径有版本、有审核，上游一变，受影响的口径立即拦截" },
  { icon: "analysis", title: "有依据的分析", text: "问一句话，得到能追溯到数据和口径的结论，而不是一段猜测" },
];

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

/** 产品预览：一条带依据的答案卡片。数字与名称是中性的示意。 */
function AnswerPreview() {
  const bars = [42, 55, 48, 61, 58, 72, 66, 80, 76, 88, 84, 96];
  return (
    <div className="av-login__preview" aria-hidden="true">
      <div className="av-login__q"><span className="av-login__avatar" />本季度核心指标为什么上升？</div>
      <div className="av-login__answer">
        <div className="av-login__answer-hd">
          <LogoMark size={18} />
          <span>分析完成</span>
          <span className="av-login__ok"><Icon name="check" small />来源已校验</span>
        </div>
        <div className="av-login__kpi">
          <div><small>本期</small><b>1,284.6<em>万</em></b></div>
          <div><small>环比</small><b className="is-up">+12.4%</b></div>
          <div className="av-login__spark-bars">{bars.map((h, i) => <i key={i} style={{ height: `${h}%` }} />)}</div>
        </div>
        <p>增长主要来自 <mark>新渠道</mark> 与 <mark>复购提升</mark>，两者合计贡献 83%<sup>D3</sup><sup>D7</sup></p>
        <div className="av-login__chips"><span>口径 V3 · 已审核</span><span>列级血缘 12 条</span><span>2 个仍待验证</span></div>
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
      setError(e instanceof ApiError ? { text: e.message, locked: e.status === 429 } : { text: "无法连接后端", locked: false });
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
        <span className="av-login__tag"><i />企业数据治理与智能分析平台</span>
      </header>
      <main className="av-login__main">
        <section className="av-login__hero" aria-label="Avelis">
          <span className="av-login__eyebrow">血缘 · 口径 · 有依据的智能分析</span>
          <h1>让每一个数字<br /><span>都有出处</span></h1>
          <p>Avelis 把元数据、列级血缘和指标口径治理成一张可信的数据底座，再让智能分析在这张底座上回答问题——每个结论都能追溯到数据、口径与权限。</p>
          <ul className="av-login__values">
            {VALUES.map((v) => (
              <li key={v.title}><span className="av-login__icon"><Icon name={v.icon} /></span>
                <div><b>{v.title}</b><small>{v.text}</small></div></li>
            ))}
          </ul>
          <AnswerPreview />
        </section>
        <section className="av-login__panel">
          <form className="av-login__card" onSubmit={submit} noValidate>
            <div className="av-login__card-hd">
              <LogoMark size={44} />
              <h2>欢迎回来</h2>
              <p>使用管理员分配的账号登录 Avelis</p>
            </div>
            {expired && !error ? (
              <div className="av-banner av-banner--info" role="status"><Icon name="info" />会话已过期或已在其他位置失效，请重新登录</div>
            ) : null}
            <label className="av-field">
              <span className="av-label">账号</span>
              <span className="av-login__input"><Icon name="users" small />
                <input autoComplete="username" autoFocus value={username} placeholder="输入账号"
                  onChange={(e) => setUsername(e.target.value)} required /></span>
            </label>
            <label className="av-field">
              <span className="av-label">密码</span>
              <span className="av-login__input"><Icon name="lock" small />
                <input type={reveal ? "text" : "password"} autoComplete="current-password" value={password} placeholder="输入密码"
                  onChange={(e) => setPassword(e.target.value)} required />
                <button type="button" className="av-login__reveal" aria-pressed={reveal}
                  onClick={() => setReveal(!reveal)}>{reveal ? "隐藏" : "显示"}</button></span>
            </label>
            {error ? (
              <div className={`av-banner ${error.locked ? "av-banner--warn" : "av-banner--danger"}`} role="alert">{error.text}</div>
            ) : null}
            <button className="av-login__submit" type="submit" disabled={busy || !username || !password}>
              {busy ? "登录中…" : <>登录<Icon name="arrow" small /></>}
            </button>
            <p className="av-login__foot">忘记密码请联系管理员重置</p>
          </form>
          <ul className="av-login__trust">
            <li><Icon name="shield" small />权限与审计全程留痕</li>
            <li><Icon name="db" small />支持私有化部署</li>
            <li><Icon name="key" small />可接入本地模型</li>
          </ul>
        </section>
      </main>
    </div>
  );
}
