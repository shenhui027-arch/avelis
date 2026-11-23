// 账号安全：个人双因素认证（TOTP 2FA）配置与个人访问令牌（PAT）管理。
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { create as createQrMatrix } from "qrcode";
import { useMemo, useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useTimeZone } from "../../api/hooks";
import {
  Banner, ConfirmDialog, Drawer, Hint, Icon, PaneHeader, Pill, QueryState, Select, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";
import { t } from "../../i18n";
import { useMe } from "../../layout/Shell";

type Pat = Schemas["PatOut"];

export function SecurityPage() {
  const me = useMe();
  const tz = useTimeZone();
  const qc = useQueryClient();
  const [message, setMessage] = useMessage();

  // --- TOTP 2FA ---
  const totp = useQuery({ queryKey: ["me-totp"], queryFn: () => unwrap(api.GET("/api/me/totp")) });
  const [enrollData, setEnrollData] = useState<Schemas["TotpEnrollOut"] | null>(null);
  const [verifyCode, setVerifyCode] = useState("");
  const [copiedSecret, setCopiedSecret] = useState(false);
  const [copiedUri, setCopiedUri] = useState(false);
  const [disabling, setDisabling] = useState(false);
  const [disableCred, setDisableCred] = useState("");

  const qrSvg = useMemo(() => {
    if (!enrollData?.otpauth_uri) return null;
    try {
      const { modules } = createQrMatrix(enrollData.otpauth_uri, { errorCorrectionLevel: "M" });
      const n = modules.size;
      const parts: string[] = [];
      for (let r = 0; r < n; r++) {
        for (let c = 0; c < n; c++) {
          if (modules.get(r, c)) parts.push(`M${c} ${r}h1v1h-1z`);
        }
      }
      return { size: n, path: parts.join("") };
    } catch {
      return null;
    }
  }, [enrollData?.otpauth_uri]);

  const startEnroll = useAction(() => unwrap(api.POST("/api/me/totp/enroll")), []);
  const verifyEnroll = useAction(
    () => unwrap(api.POST("/api/me/totp/verify", { body: { code: verifyCode.trim() } })),
    ["me-totp", "me"],
  );
  const disableTotp = useAction(
    () => unwrap(api.POST("/api/me/totp/disable", {
      body: me.data?.auth_provider === "local" && disableCred.trim().length !== 6
        ? { current_password: disableCred }
        : /^\d{6}$/.test(disableCred.trim())
          ? { code: disableCred.trim() }
          : { current_password: disableCred },
    })),
    ["me-totp", "me"],
  );

  // --- 个人访问令牌（PAT） ---
  const tokens = useQuery({ queryKey: ["me-tokens"], queryFn: () => unwrap(api.GET("/api/me/tokens")) });
  const [creatingPat, setCreatingPat] = useState(false);
  const [createdToken, setCreatedToken] = useState<{ name: string; token: string } | null>(null);
  const [revoking, setRevoking] = useState<Pat | null>(null);
  const [copiedPat, setCopiedPat] = useState(false);

  const revokePat = useAction(
    (tokenId: string) => unwrap(api.DELETE("/api/me/tokens/{token_id}", { params: { path: { token_id: tokenId } } })),
    ["me-tokens"],
  );

  const copyText = (text: string, mark: (v: boolean) => void) => {
    navigator.clipboard?.writeText(text).then(() => {
      mark(true);
      window.setTimeout(() => mark(false), 2000);
    });
  };

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("账号安全")}</h1></div>
      </div>
      <div className="av-workspace av-security">
        <section className="av-pane">
          <PaneHeader title={<>{t("双因素认证（TOTP）")} <Hint text={t("启用后，本地登录除账号密码外还需输入验证器应用生成的 6 位动态验证码")} /></>}>
            {totp.data ? (
              totp.data.enabled ? <Pill tone="ok">{t("已启用")}</Pill> : <Pill>{t("未启用")}</Pill>
            ) : null}
          </PaneHeader>
          <div className="av-pane__body av-security__body">
            <Banner message={message} />
            <QueryState query={totp} rows={2}>
              {(status) => (
                status.enabled ? (
                  <div className="av-security__totp-active">
                    <p className="av-muted">
                      {status.enabled_at
                        ? t("已于 {{time}} 启用双因素认证。", { time: formatTime(status.enabled_at, tz) })
                        : t("双因素认证已保护当前账号。")}
                    </p>
                    <div>
                      <button type="button" className="av-btn av-btn--danger av-btn--sm" onClick={() => { setDisableCred(""); setDisabling(true); }}>
                        {t("停用双因素认证")}
                      </button>
                    </div>
                  </div>
                ) : enrollData ? (
                  <div className="av-security__enroll">
                    <p className="av-muted">{t("请将密钥或配置链接添加到验证器应用，然后输入当前生成的 6 位验证码完成绑定：")}</p>
                    <div className="av-security__enroll-grid">
                      {qrSvg ? (
                        <div className="av-security__qr">
                          <div className="av-security__qr-frame">
                            <svg
                              className="av-security__qr-svg"
                              viewBox={`-2 -2 ${qrSvg.size + 4} ${qrSvg.size + 4}`}
                              role="img"
                              aria-label={t("双因素认证配置二维码")}
                              shapeRendering="crispEdges"
                            >
                              <rect x={-2} y={-2} width={qrSvg.size + 4} height={qrSvg.size + 4} fill="#ffffff" />
                              <path d={qrSvg.path} fill="#0f1419" />
                            </svg>
                          </div>
                          <span className="av-caption">{t("使用验证器应用扫码绑定")}</span>
                        </div>
                      ) : null}
                      <div className="av-security__enroll-fields">
                        <div className="av-field">
                          <span className="av-label">{t("密钥（Base32）")}</span>
                          <div className="av-webhooks__secretrow">
                            <code className="av-code av-mono av-webhooks__secret">{enrollData.secret}</code>
                            <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => copyText(enrollData.secret, setCopiedSecret)}>
                              {copiedSecret ? t("已复制") : t("复制")}
                            </button>
                          </div>
                        </div>
                        <div className="av-field">
                          <span className="av-label">{t("配置链接（otpauth）")}</span>
                          <div className="av-webhooks__secretrow">
                            <code className="av-code av-mono av-webhooks__secret">{enrollData.otpauth_uri}</code>
                            <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => copyText(enrollData.otpauth_uri, setCopiedUri)}>
                              {copiedUri ? t("已复制") : t("复制")}
                            </button>
                          </div>
                        </div>
                      </div>
                    </div>
                    <div className="av-security__verify-row">
                      <label className="av-field" style={{ flex: 1, maxWidth: 240 }}>
                        <span className="av-label">{t("6 位验证码")}</span>
                        <input
                          className="av-input av-mono"
                          inputMode="numeric"
                          maxLength={8}
                          placeholder="000000"
                          value={verifyCode}
                          onChange={(e) => setVerifyCode(e.target.value)}
                        />
                      </label>
                      <button
                        type="button"
                        className="av-btn av-btn--primary"
                        disabled={verifyEnroll.isPending || verifyCode.trim().length < 6}
                        onClick={() => verifyEnroll.mutate(undefined, {
                          onSuccess: () => {
                            setEnrollData(null);
                            setVerifyCode("");
                            setMessage({ ok: true, text: t("已启用双因素认证") });
                            void qc.invalidateQueries({ queryKey: ["me-totp"] });
                          },
                          onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                        })}
                      >
                        {verifyEnroll.isPending ? t("验证中…") : t("确认启用")}
                      </button>
                      <button type="button" className="av-btn av-btn--ghost" onClick={() => { setEnrollData(null); setVerifyCode(""); }}>
                        {t("取消")}
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="av-security__totp-idle">
                    <p className="av-muted">{t("尚未启用双因素认证。建议为管理员与高频访问账号开启动态口令保护。")}</p>
                    <div>
                      <button
                        type="button"
                        className="av-btn av-btn--primary av-btn--sm"
                        disabled={startEnroll.isPending}
                        onClick={() => startEnroll.mutate(undefined, {
                          onSuccess: (data) => { setEnrollData(data); setMessage(null); },
                          onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                        })}
                      >
                        <Icon name="shield" small />
                        {startEnroll.isPending ? t("生成中…") : t("开启双因素认证")}
                      </button>
                    </div>
                  </div>
                )
              )}
            </QueryState>
          </div>
        </section>

        <section className="av-pane">
          <PaneHeader title={<>{t("个人访问令牌（PAT）")} <Hint text={t("用于脚本或自动化工具通过 Bearer 凭证调用业务接口；创建时仅显示一次")} /></>} count={tokens.data?.length}>
            <button type="button" className="av-btn av-btn--primary av-btn--sm" onClick={() => setCreatingPat(true)}>
              <Icon name="plus" small />
              {t("生成令牌")}
            </button>
          </PaneHeader>
          <div className="av-scroll">
            <QueryState query={tokens} isEmpty={(list) => !list.length} emptyText={t("暂无个人访问令牌")}>
              {(list) => (
                <table className="av-table">
                  <thead>
                    <tr>
                      <th style={{ paddingLeft: 16 }}>{t("名称")}</th>
                      <th>{t("前缀")}</th>
                      <th>{t("权限范围")}</th>
                      <th>{t("到期时间")}</th>
                      <th>{t("最近使用")}</th>
                      <th>{t("状态")}</th>
                      <th className="end" style={{ paddingRight: 16 }}>{t("操作")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((item) => (
                      <tr key={item.id}>
                        <td style={{ paddingLeft: 16 }}><b>{item.name}</b></td>
                        <td className="av-mono">{item.prefix}…</td>
                        <td>{item.scopes.map((s) => <span key={s} className="av-tag" style={{ marginRight: 4 }}>{s}</span>)}</td>
                        <td className="av-mono">{item.expires_at ? formatTime(item.expires_at, tz) : t("永不过期")}</td>
                        <td className="av-mono">{item.last_used_at ? formatTime(item.last_used_at, tz) : "—"}</td>
                        <td>{item.revoked_at ? <Pill>{t("已吊销")}</Pill> : <Pill tone="ok">{t("有效")}</Pill>}</td>
                        <td className="end" style={{ paddingRight: 16 }}>
                          {!item.revoked_at ? (
                            <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setRevoking(item)}>
                              {t("吊销")}
                            </button>
                          ) : <span className="av-muted">—</span>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </QueryState>
          </div>
        </section>
      </div>

      {disabling ? (
        <ConfirmDialog
          danger
          title={t("停用双因素认证？")}
          body={t("停用后登录将不再校验动态验证码。请输入当前密码或 6 位动态验证码以确认。")}
          confirm={t("停用")}
          onCancel={() => setDisabling(false)}
          onConfirm={async () => {
            await disableTotp.mutateAsync(undefined);
            setDisabling(false);
            setMessage({ ok: true, text: t("已停用双因素认证") });
          }}
        >
          <label className="av-field" style={{ marginTop: 8 }}>
            <span className="av-label">{t("当前密码或 6 位验证码")}</span>
            <input
              className="av-input"
              type="password"
              autoComplete="current-password"
              value={disableCred}
              onChange={(e) => setDisableCred(e.target.value)}
            />
          </label>
        </ConfirmDialog>
      ) : null}

      {creatingPat ? (
        <CreatePatDrawer
          onClose={() => setCreatingPat(false)}
          onCreated={(created) => {
            setCreatingPat(false);
            setCopiedPat(false);
            setCreatedToken({ name: created.name, token: created.token });
          }}
        />
      ) : null}

      {createdToken ? (
        <ConfirmDialog
          title={t("已生成令牌 · {{name}}", { name: createdToken.name })}
          body={t("完整令牌仅显示这一次，请立即复制并妥善保存。")}
          confirm={t("我已保存")}
          onCancel={() => setCreatedToken(null)}
          onConfirm={() => setCreatedToken(null)}
        >
          <div className="av-webhooks__secretrow">
            <code className="av-code av-mono av-webhooks__secret">{createdToken.token}</code>
            <button
              type="button"
              className="av-btn av-btn--secondary av-btn--sm"
              onClick={() => copyText(createdToken.token, setCopiedPat)}
            >
              {copiedPat ? t("已复制") : t("复制")}
            </button>
          </div>
        </ConfirmDialog>
      ) : null}

      {revoking ? (
        <ConfirmDialog
          danger
          title={t("吊销令牌 {{name}}？", { name: revoking.name })}
          body={t("吊销后使用该令牌的脚本或接口请求将立即失效，不可恢复。")}
          confirm={t("吊销")}
          onCancel={() => setRevoking(null)}
          onConfirm={async () => {
            await revokePat.mutateAsync(revoking.id);
            setRevoking(null);
            setMessage({ ok: true, text: t("已吊销个人访问令牌") });
          }}
        />
      ) : null}
    </div>
  );
}

function CreatePatDrawer({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: Schemas["PatCreatedOut"]) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<("read" | "write")[]>(["read"]);
  const [expiry, setExpiry] = useState("90");
  const [message, setMessage] = useMessage();

  const create = useAction(
    () => unwrap(api.POST("/api/me/tokens", {
      body: {
        name: name.trim(),
        scopes,
        expires_in_days: expiry ? Number(expiry) : null,
      },
    })),
    ["me-tokens"],
  );

  const toggleScope = (s: "read" | "write") => {
    setScopes((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));
  };

  return (
    <Drawer
      title={t("生成个人访问令牌")}
      onClose={() => { if (!create.isPending) onClose(); }}
      footer={(
        <>
          <button type="button" className="av-btn av-btn--secondary" disabled={create.isPending} onClick={onClose}>
            {t("取消")}
          </button>
          <button
            type="button"
            className="av-btn av-btn--primary"
            disabled={!name.trim() || !scopes.length || create.isPending}
            onClick={() => create.mutate(undefined, {
              onSuccess: onCreated,
              onError: (e) => setMessage({ ok: false, text: errorText(e) }),
            })}
          >
            {create.isPending ? t("生成中…") : t("生成")}
          </button>
        </>
      )}
    >
      <div className="av-webhooks__form">
        <label className="av-field">
          <span className="av-label">{t("令牌名称")}</span>
          <input
            className="av-input"
            maxLength={80}
            placeholder={t("如：自动化脚本 / CI 查询")}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <div className="av-field">
          <span className="av-label">{t("权限范围")}</span>
          <label className="av-option">
            <input type="checkbox" checked={scopes.includes("read")} onChange={() => toggleScope("read")} />
            <span className="av-option__title">{t("只读（read）")}</span>
          </label>
          <label className="av-option">
            <input type="checkbox" checked={scopes.includes("write")} onChange={() => toggleScope("write")} />
            <span className="av-option__title">{t("写入（write）")}</span>
          </label>
        </div>
        <label className="av-field">
          <span className="av-label">{t("有效期")}</span>
          <Select value={expiry} label={t("有效期")} onChange={setExpiry}>
            <option value="30">{t("30 天")}</option>
            <option value="90">{t("90 天")}</option>
            <option value="365">{t("365 天")}</option>
            <option value="">{t("永不过期")}</option>
          </Select>
        </label>
        <Banner message={message} />
      </div>
    </Drawer>
  );
}
