// MCP 授权确认（ADR 0019 §2）：外部 AI 工具发起 OAuth 授权后，后端校验请求并跳到这里。
// 复用 Avelis 已有的登录（Bearer 令牌，不用 Cookie）：同意或拒绝后由后端给出回调地址，再整页跳回客户端。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useSearchParams } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText } from "../../api/hooks";
import { LogoMark } from "../../components/Logo";
import { t } from "../../i18n";

const SCOPE_LABEL: Record<string, string> = {
  "metrics:read": t("查看已发布的口径定义"),
  "lineage:read": t("查看资产血缘与结构变化"),
  "ask:execute": t("按口径查询数据（不能提交 SQL）"),
};

export function OAuthConsent() {
  const [params] = useSearchParams();
  const ticket = params.get("ticket") ?? "";
  const request = useQuery({
    queryKey: ["oauth-consent", ticket], enabled: !!ticket, retry: false,
    queryFn: () => unwrap(api.GET("/api/oauth/consent/{ticket}", { params: { path: { ticket } } })),
  });
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function decide(choice: "approve" | "deny") {
    setBusy(choice);
    setError(null);
    try {
      const out = choice === "approve"
        ? await unwrap(api.POST("/api/oauth/consent/{ticket}/approve", { params: { path: { ticket } } }))
        : await unwrap(api.POST("/api/oauth/consent/{ticket}/deny", { params: { path: { ticket } } }));
      // 回调地址由后端按已登记的 redirect_uri 生成，整页跳转回客户端
      window.location.assign(out.redirect_to);
    } catch (e) {
      setError(errorText(e));
      setBusy(null);
    }
  }

  const r = request.data;
  return (
    <div className="av-root av-auth">
      <div className="av-auth__card av-consent">
        <div className="av-auth__brand"><LogoMark size={28} /><span>Avelis</span></div>
        {!ticket ? <div className="av-banner av-banner--danger" role="alert">{t("授权请求无效：缺少票据")}</div>
          : request.isPending ? <p className="av-caption">{t("加载中…")}</p>
          : request.isError ? <div className="av-banner av-banner--danger" role="alert">{t("授权请求已失效或不存在，请回到原应用重新发起")}</div>
          : r ? <>
            <div className="av-auth__head">
              <h1>{t("授权 {{name}} 访问 Avelis", { name: r.client_name })}</h1>
              <p>{t("该应用将以你的身份、按你现有的权限访问以下内容")}</p>
            </div>
            <dl className="av-kv av-consent__kv">
              <dt>{t("应用")}</dt><dd><span className="av-consent__client">{r.client_name}</span>
                <span className="av-caption av-mono av-consent__id" title={r.client_id}>{r.client_id}</span></dd>
              <dt>{t("权限")}</dt><dd><ul className="av-consent__scopes">
                {r.scopes.map((s) => <li key={s.scope}><span className="av-tag">{s.scope}</span> {SCOPE_LABEL[s.scope] ?? s.description}</li>)}</ul></dd>
              <dt>{t("业务域")}</dt><dd className="av-consent__domains">{r.domains.length
                ? r.domains.map((d) => <span key={d.id} className="av-tag">{d.name}</span>) : t("（你目前没有可访问的业务域）")}</dd>
            </dl>
            <p className="av-caption">{t("可以随时在“设置 → MCP 接入”撤销授权")}</p>
            {error ? <div className="av-banner av-banner--danger" role="alert">{error}</div> : null}
            <div className="av-auth__actions">
              <button type="button" className="av-btn av-btn--secondary av-btn--lg" disabled={!!busy} onClick={() => decide("deny")}>
                {busy === "deny" ? t("处理中…") : t("拒绝")}</button>
              <button type="button" className="av-btn av-btn--primary av-btn--lg" disabled={!!busy} autoFocus onClick={() => decide("approve")}>
                {busy === "approve" ? t("处理中…") : t("同意并继续")}</button>
            </div>
          </> : null}
      </div>
    </div>
  );
}
