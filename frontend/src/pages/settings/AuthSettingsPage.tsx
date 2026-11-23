// 认证与品牌设置（仅管理员）：标准 OIDC（PKCE + JIT）、反代 Forward Auth、本地登录开关与轻白标配置。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api, unwrap } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Banner, Empty, Hint, PaneHeader, QueryState, Select, Switch, useMessage } from "../../components/ui";
import { t } from "../../i18n";
import { SettingsTabs, useMe, useRoles } from "../../layout/Shell";

export function AuthSettingsPage() {
  const me = useMe();
  const roles = useRoles();
  const isAdmin = roles.has("admin");
  const query = useQuery({
    queryKey: ["auth-settings"],
    enabled: isAdmin,
    queryFn: () => unwrap(api.GET("/api/settings/auth")),
  });

  const [loaded, setLoaded] = useState(false);
  const [localLogin, setLocalLogin] = useState(true);
  const [forwardEnabled, setForwardEnabled] = useState(false);
  const [forwardHeader, setForwardHeader] = useState("X-Forwarded-User");
  const [forwardAutoCreate, setForwardAutoCreate] = useState(true);
  const [forwardSecret, setForwardSecret] = useState("");
  const [clearForwardSecret, setClearForwardSecret] = useState(false);

  const [oidcEnabled, setOidcEnabled] = useState(false);
  const [providerName, setProviderName] = useState("SSO");
  const [issuerUrl, setIssuerUrl] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [clearClientSecret, setClearClientSecret] = useState(false);
  const [scopes, setScopes] = useState("openid profile email");
  const [autoCreateUser, setAutoCreateUser] = useState(true);
  const [defaultRole, setDefaultRole] = useState("analyst");

  const [brandName, setBrandName] = useState("Avelis");
  const [brandLogoUrl, setBrandLogoUrl] = useState("");
  const [loginSubtitle, setLoginSubtitle] = useState("");
  const [termsUrl, setTermsUrl] = useState("");
  const [privacyUrl, setPrivacyUrl] = useState("");
  const [message, setMessage] = useMessage();

  useEffect(() => {
    const d = query.data;
    if (!d) return;
    setLocalLogin(d.local_login_enabled);
    setForwardEnabled(d.forward_auth_enabled);
    setForwardHeader(d.forward_auth_header);
    setForwardAutoCreate(d.forward_auth_auto_create);
    setForwardSecret("");
    setClearForwardSecret(false);
    setOidcEnabled(d.oidc.enabled);
    setProviderName(d.oidc.provider_name);
    setIssuerUrl(d.oidc.issuer_url);
    setClientId(d.oidc.client_id);
    setClientSecret("");
    setClearClientSecret(false);
    setScopes(d.oidc.scopes);
    setAutoCreateUser(d.oidc.auto_create_user);
    setDefaultRole(d.oidc.default_role);
    setBrandName(d.branding.brand_name ?? "Avelis");
    setBrandLogoUrl(d.branding.brand_logo_url ?? "");
    setLoginSubtitle(d.branding.login_subtitle ?? "");
    setTermsUrl(d.branding.terms_url ?? "");
    setPrivacyUrl(d.branding.privacy_url ?? "");
    setLoaded(true);
  }, [query.data]);

  const save = useAction(
    () => unwrap(api.PUT("/api/settings/auth", {
      body: {
        local_login_enabled: localLogin,
        forward_auth_enabled: forwardEnabled,
        forward_auth_header: forwardHeader.trim() || "X-Forwarded-User",
        forward_auth_auto_create: forwardAutoCreate,
        forward_auth_secret: forwardSecret ? forwardSecret : null,
        clear_forward_auth_secret: clearForwardSecret,
        oidc: {
          enabled: oidcEnabled,
          provider_name: providerName.trim() || "SSO",
          issuer_url: issuerUrl.trim(),
          client_id: clientId.trim(),
          client_secret: clientSecret ? clientSecret : null,
          clear_client_secret: clearClientSecret,
          scopes: scopes.trim() || "openid profile email",
          auto_create_user: autoCreateUser,
          default_role: defaultRole,
        },
        branding: {
          brand_name: brandName.trim() || "Avelis",
          brand_logo_url: brandLogoUrl.trim() || null,
          login_subtitle: loginSubtitle.trim() || null,
          terms_url: termsUrl.trim() || null,
          privacy_url: privacyUrl.trim() || null,
        },
      },
    })),
    ["auth-settings", "auth-config"],
  );

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("设置")}</h1><SettingsTabs current="auth" /></div>
        {isAdmin ? (
          <div className="av-actions">
            <button
              type="button"
              className="av-btn av-btn--primary"
              disabled={!loaded || save.isPending}
              aria-busy={save.isPending}
              onClick={() => save.mutate(undefined, {
                onSuccess: () => {
                  setForwardSecret("");
                  setClientSecret("");
                  setMessage({ ok: true, text: t("已保存认证与品牌配置") });
                },
                onError: (e) => setMessage({ ok: false, text: errorText(e) }),
              })}
            >
              {save.isPending ? t("保存中…") : t("保存")}
            </button>
          </div>
        ) : null}
      </div>

      {me.data === undefined ? (
        <QueryState query={me} rows={6}>{() => null}</QueryState>
      ) : !isAdmin ? (
        <Empty text={t("只有管理员可以配置认证与品牌设置")} />
      ) : (
        <div className="av-workspace">
          <section className="av-pane">
            <PaneHeader title={<>{t("认证与品牌")} <Hint text={t("支持 1 组标准 OIDC 单点登录、受信任反向代理 Forward Auth 与登录页白标定制")} /></>} />
            <QueryState query={query} rows={6}>
              {(data) => (
                <div className="av-scroll av-auth-settings">
                  <Banner message={message} />

                  <div className="av-auth-settings__section">
                    <h3>{t("登录方式与反向代理")}</h3>
                    <div className="av-field av-field--inline">
                      <span className="av-label">{t("允许本地账号密码登录")}</span>
                      <Switch label={t("允许本地账号密码登录")} checked={localLogin} onChange={setLocalLogin} />
                    </div>
                    <div className="av-field av-field--inline">
                      <span className="av-label">{t("启用反向代理 Forward Auth")}</span>
                      <Switch label={t("启用反向代理 Forward Auth")} checked={forwardEnabled} onChange={setForwardEnabled} />
                    </div>
                    {forwardEnabled ? (
                      <div className="av-auth-settings__grid">
                        <label className="av-field">
                          <span className="av-label">{t("身份请求头名称")}</span>
                          <input className="av-input av-mono" value={forwardHeader} onChange={(e) => setForwardHeader(e.target.value)} />
                        </label>
                        <label className="av-field">
                          <span className="av-label">
                            {t("共享密钥（X-Forwarded-Auth-Secret）")}
                            {data.has_forward_auth_secret && data.forward_auth_secret_hint ? (
                              <span className="av-caption av-mono"> · {data.forward_auth_secret_hint}</span>
                            ) : null}
                          </span>
                          <input
                            className="av-input av-mono"
                            type="password"
                            autoComplete="new-password"
                            placeholder={data.has_forward_auth_secret ? t("留空保留当前密钥") : t("可选")}
                            value={forwardSecret}
                            onChange={(e) => setForwardSecret(e.target.value)}
                          />
                        </label>
                        <div className="av-field av-field--inline">
                          <span className="av-label">{t("首次访问自动创建账号（JIT）")}</span>
                          <Switch label={t("首次访问自动创建账号（JIT）")} checked={forwardAutoCreate} onChange={setForwardAutoCreate} />
                        </div>
                        {data.has_forward_auth_secret ? (
                          <label className="av-option">
                            <input type="checkbox" checked={clearForwardSecret} onChange={(e) => setClearForwardSecret(e.target.checked)} />
                            <span className="av-option__title">{t("清除已配置的共享密钥")}</span>
                          </label>
                        ) : null}
                      </div>
                    ) : null}
                  </div>

                  <div className="av-auth-settings__section">
                    <h3>{t("标准 OIDC 单点登录")}</h3>
                    <div className="av-field av-field--inline">
                      <span className="av-label">{t("启用 OIDC 单点登录（PKCE）")}</span>
                      <Switch label={t("启用 OIDC 单点登录（PKCE）")} checked={oidcEnabled} onChange={setOidcEnabled} />
                    </div>
                    <div className="av-auth-settings__grid">
                      <label className="av-field">
                        <span className="av-label">{t("按钮显示名称")}</span>
                        <input className="av-input" value={providerName} placeholder="SSO" onChange={(e) => setProviderName(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("Issuer URL")}</span>
                        <input className="av-input av-mono" value={issuerUrl} placeholder="https://idp.example.com" onChange={(e) => setIssuerUrl(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("Client ID")}</span>
                        <input className="av-input av-mono" value={clientId} onChange={(e) => setClientId(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">
                          {t("Client Secret")}
                          {data.oidc.has_client_secret && data.oidc.client_secret_hint ? (
                            <span className="av-caption av-mono"> · {data.oidc.client_secret_hint}</span>
                          ) : null}
                        </span>
                        <input
                          className="av-input av-mono"
                          type="password"
                          autoComplete="new-password"
                          placeholder={data.oidc.has_client_secret ? t("留空保留当前密钥") : t("可选（公共客户端可留空）")}
                          value={clientSecret}
                          onChange={(e) => setClientSecret(e.target.value)}
                        />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("Scopes")}</span>
                        <input className="av-input av-mono" value={scopes} onChange={(e) => setScopes(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("新用户默认角色")}</span>
                        <Select value={defaultRole} label={t("新用户默认角色")} onChange={setDefaultRole}>
                          <option value="analyst">{t("查询者")}</option>
                          <option value="steward">{t("维护者")}</option>
                          <option value="reviewer">{t("复核者")}</option>
                        </Select>
                      </label>
                    </div>
                    <div className="av-field av-field--inline">
                      <span className="av-label">{t("首次 OIDC 登录自动创建账号（JIT）")}</span>
                      <Switch label={t("首次 OIDC 登录自动创建账号（JIT）")} checked={autoCreateUser} onChange={setAutoCreateUser} />
                    </div>
                    {data.oidc.has_client_secret ? (
                      <label className="av-option">
                        <input type="checkbox" checked={clearClientSecret} onChange={(e) => setClearClientSecret(e.target.checked)} />
                        <span className="av-option__title">{t("清除已配置的 Client Secret")}</span>
                      </label>
                    ) : null}
                  </div>

                  <div className="av-auth-settings__section">
                    <h3>{t("轻白标外观")}</h3>
                    <div className="av-auth-settings__grid">
                      <label className="av-field">
                        <span className="av-label">{t("品牌名称")}</span>
                        <input className="av-input" value={brandName} maxLength={64} onChange={(e) => setBrandName(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("品牌 Logo 地址")}</span>
                        <input className="av-input av-mono" value={brandLogoUrl} placeholder="https://... 或 /logo.svg" onChange={(e) => setBrandLogoUrl(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("登录页副标题")}</span>
                        <input className="av-input" value={loginSubtitle} placeholder={t("使用你的企业账号登录")} onChange={(e) => setLoginSubtitle(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("服务条款链接")}</span>
                        <input className="av-input av-mono" value={termsUrl} placeholder="https://..." onChange={(e) => setTermsUrl(e.target.value)} />
                      </label>
                      <label className="av-field">
                        <span className="av-label">{t("隐私政策链接")}</span>
                        <input className="av-input av-mono" value={privacyUrl} placeholder="https://..." onChange={(e) => setPrivacyUrl(e.target.value)} />
                      </label>
                    </div>
                  </div>
                </div>
              )}
            </QueryState>
          </section>
        </div>
      )}
    </div>
  );
}
