// 登录：左侧品牌区（标记、定位、血缘示意），右侧表单。会话过期、被锁定、需要改口令都在这里给出明确去向。
import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router";

import { ApiError, login } from "../api/client";
import { Icon } from "../components/Icon";
import { LogoMark } from "../components/Logo";

const PILLARS = [
  { icon: "lineage", label: "列级血缘" },
  { icon: "shield", label: "口径治理" },
  { icon: "change", label: "变更拦截" },
];

/** 品牌区示意：来源 → 视图 → 口径 → 答案的一条可追溯路径，依据节点高亮。纯装饰。 */
function LineageMotif() {
  const nodes = [
    { x: 24, y: 40, w: 112, label: "shop.orders" },
    { x: 24, y: 118, w: 112, label: "shop.refunds" },
    { x: 196, y: 79, w: 120, label: "daily_sales" },
    { x: 376, y: 79, w: 104, label: "实付销售额 V1", accent: true },
  ];
  return (
    <svg className="av-auth__motif" viewBox="0 0 504 176" aria-hidden="true" focusable="false">
      <path d="M136 57 C166 57 166 96 196 96" />
      <path d="M136 135 C166 135 166 96 196 96" className="is-muted" />
      <path d="M316 96 L376 96" className="is-signal" />
      {nodes.map((n) => (
        <g key={n.label} className={n.accent ? "is-accent" : undefined}>
          <rect x={n.x} y={n.y} width={n.w} height={34} rx={6} />
          <circle cx={n.x + 14} cy={n.y + 17} r={3} />
          <text x={n.x + 26} y={n.y + 21}>{n.label}</text>
        </g>
      ))}
    </svg>
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
    <div className="av-login">
      <section className="av-login__brand" aria-label="Avelis">
        <div className="av-auth__brand"><LogoMark size={28} /><span>Avelis</span></div>
        <div className="av-login__pitch">
          <h1>有依据的智能问数</h1>
          <p>每个答案都能追溯到来源、口径版本与权限；上游一变，受影响的口径即被拦截。</p>
        </div>
        <LineageMotif />
        <ul className="av-login__pillars">
          {PILLARS.map((p) => <li key={p.label}><Icon name={p.icon} />{p.label}</li>)}
        </ul>
      </section>
      <section className="av-login__panel">
        <form className="av-auth__card" onSubmit={submit} noValidate>
          <div className="av-auth__head">
            <h2>登录</h2>
            <p>使用管理员分配的账号</p>
          </div>
          {expired && !error ? (
            <div className="av-banner av-banner--info" role="status"><Icon name="info" />会话已过期或已在其他位置失效，请重新登录</div>
          ) : null}
          <label className="av-field">
            <span className="av-label">账号</span>
            <input className="av-input" autoComplete="username" autoFocus value={username}
              onChange={(e) => setUsername(e.target.value)} required />
          </label>
          <label className="av-field">
            <span className="av-label">口令</span>
            <span className="av-input-group">
              <input className="av-input" type={reveal ? "text" : "password"} autoComplete="current-password"
                value={password} onChange={(e) => setPassword(e.target.value)} required />
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" aria-pressed={reveal}
                onClick={() => setReveal(!reveal)}>{reveal ? "隐藏" : "显示"}</button>
            </span>
          </label>
          {error ? (
            <div className={`av-banner ${error.locked ? "av-banner--warn" : "av-banner--danger"}`} role="alert">{error.text}</div>
          ) : null}
          <button className="av-btn av-btn--primary av-btn--lg" type="submit" disabled={busy || !username || !password}>
            {busy ? "登录中…" : "登录"}
          </button>
          <p className="av-auth__foot">忘记口令请联系管理员重置</p>
        </form>
      </section>
    </div>
  );
}
