// MCP 接入（ADR 0019）：外部 AI 工具的连接地址、我授权过的应用；管理员另看全部客户端并可禁用。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, ApiError, unwrap } from "../../api/client";
import { useAction, useTimeZone } from "../../api/hooks";
import { Banner, ConfirmDialog, Empty, Hint, PaneHeader, Pill, QueryState, useMessage } from "../../components/ui";
import { formatTime } from "../../format";
import { t } from "../../i18n";
import { SettingsTabs, useRoles } from "../../layout/Shell";

const SOURCE_LABEL: Record<string, string> = { cimd: t("元数据文档"), dynamic: t("动态注册"), pre_registered: t("预登记") };

export function McpPage() {
  const roles = useRoles();
  const isAdmin = roles.has("admin");
  const tz = useTimeZone();
  const [message, setMessage] = useMessage();
  const [confirm, setConfirm] = useState<{ kind: "authorization" | "client"; id: string; name: string } | null>(null);
  const mine = useQuery({ queryKey: ["oauth-authorizations"], retry: false,
    queryFn: () => unwrap(api.GET("/api/oauth/authorizations")) });
  const clients = useQuery({ queryKey: ["oauth-clients"], enabled: isAdmin, retry: false,
    queryFn: () => unwrap(api.GET("/api/oauth/clients")) });
  const revokeMine = useAction((id: string) => unwrap(api.DELETE("/api/oauth/authorizations/{authorization_id}",
    { params: { path: { authorization_id: id } } })), ["oauth-authorizations", "oauth-clients"]);
  const revokeClient = useAction((id: string) => unwrap(api.POST("/api/oauth/clients/{client_record_id}/revoke",
    { params: { path: { client_record_id: id } } })), ["oauth-authorizations", "oauth-clients"]);
  // 服务未开启时 OAuth 与 MCP 端点都返回 404
  const disabled = mine.error instanceof ApiError && mine.error.status === 404;
  const endpoint = `${window.location.origin}/mcp`;

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("设置")}</h1><SettingsTabs current="mcp" /></div>
      </div>
      <div className="av-workspace av-mcp">
        <section className="av-pane">
          <PaneHeader title={<>{t("连接地址")} <Hint text={t("在支持 MCP 的 AI 工具里添加远程服务器，填入这个地址；首次连接会跳到这里登录并确认授权")} /></>}>
            {disabled ? <Pill tone="warn">{t("未开启")}</Pill> : mine.isSuccess ? <Pill tone="ok">{t("已开启")}</Pill> : null}
          </PaneHeader>
          <div className="av-pane__body av-mcp__body">
            {disabled ? <Banner message={{ ok: false, text: t("MCP 服务未开启：管理员需在部署配置中设置 AVELIS_MCP_ENABLED=true 并配置对外地址") }} /> : null}
            <div className="av-mcp__endpoint">
              <code className="av-code av-mono">{endpoint}</code>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => {
                navigator.clipboard?.writeText(endpoint).then(() => setMessage({ ok: true, text: t("已复制") }), () => setMessage({ ok: false, text: t("复制失败，请手动选择") }));
              }}>{t("复制")}</button>
            </div>
            <p className="av-caption">{t("外部工具只能读取口径与血缘，或按已发布口径查询数据；不能提交 SQL，也不能修改任何内容。每次调用都记入审计。")}</p>
            <Banner message={message} />
          </div>
        </section>

        <section className="av-pane">
          <PaneHeader title={t("我授权的应用")} count={mine.data?.length} />
          <div className="av-pane__body">
            {disabled ? <Empty compact text={t("服务未开启")} /> : <QueryState query={mine} isEmpty={(d) => !d.length} emptyText={t("还没有授权过任何应用")}>
              {(items) => <table className="av-table">
                <thead><tr><th style={{ paddingLeft: 16 }}>{t("应用")}</th><th>{t("权限")}</th><th>{t("授权时间")}</th><th>{t("最近使用")}</th><th className="end" /></tr></thead>
                <tbody>{items.map((a) => <tr key={a.id}>
                  <td style={{ paddingLeft: 16 }}>{a.client_name}<div className="av-caption av-mono av-mcp__id" title={a.client_id}>{a.client_id}</div></td>
                  <td>{a.scopes.map((s) => <span key={s} className="av-tag">{s}</span>)}</td>
                  <td className="av-mono">{formatTime(a.created_at, tz)}</td>
                  <td className="av-mono">{a.last_used_at ? formatTime(a.last_used_at, tz) : "—"}</td>
                  <td className="end">{a.revoked_at ? <Pill>{t("已撤销")}</Pill> : <button type="button" className="av-btn av-btn--secondary av-btn--sm"
                    onClick={() => setConfirm({ kind: "authorization", id: a.id, name: a.client_name })}>{t("撤销")}</button>}</td>
                </tr>)}</tbody>
              </table>}
            </QueryState>}
          </div>
        </section>

        {isAdmin && !disabled ? <section className="av-pane">
          <PaneHeader title={<>{t("全部客户端")} <Hint text={t("禁用后，该客户端的所有令牌立即失效，用户需要重新授权也无法再连接")} /></>} count={clients.data?.length} />
          <div className="av-pane__body">
            <QueryState query={clients} isEmpty={(d) => !d.length} emptyText={t("还没有客户端连接过")}>
              {(items) => <table className="av-table">
                <thead><tr><th style={{ paddingLeft: 16 }}>{t("客户端")}</th><th>{t("注册方式")}</th><th>{t("授权人数")}</th><th>{t("最近使用")}</th><th>{t("状态")}</th><th className="end" /></tr></thead>
                <tbody>{items.map((c) => <tr key={c.id}>
                  <td style={{ paddingLeft: 16 }}>{c.client_name}<div className="av-caption av-mono av-mcp__id" title={c.client_id}>{c.client_id}</div></td>
                  <td>{SOURCE_LABEL[c.source] ?? c.source}</td>
                  <td className="av-mono">{c.authorization_count}</td>
                  <td className="av-mono">{c.last_used_at ? formatTime(c.last_used_at, tz) : "—"}</td>
                  <td>{c.disabled_at ? <Pill tone="danger">{t("已禁用")}</Pill> : <Pill tone="ok">{t("可用")}</Pill>}</td>
                  <td className="end">{c.disabled_at ? null : <button type="button" className="av-btn av-btn--secondary av-btn--sm"
                    onClick={() => setConfirm({ kind: "client", id: c.id, name: c.client_name })}>{t("禁用")}</button>}</td>
                </tr>)}</tbody>
              </table>}
            </QueryState>
          </div>
        </section> : null}
      </div>
      {confirm ? <ConfirmDialog danger
        title={confirm.kind === "client" ? t("禁用客户端 {{name}}？", { name: confirm.name }) : t("撤销对 {{name}} 的授权？", { name: confirm.name })}
        body={confirm.kind === "client" ? t("所有用户在该客户端上的令牌立即失效，且无法再次连接。") : t("该应用持有的令牌立即失效，需要重新授权才能再次使用。")}
        confirm={confirm.kind === "client" ? t("禁用") : t("撤销")}
        onCancel={() => setConfirm(null)}
        onConfirm={async () => {
          if (confirm.kind === "client") await revokeClient.mutateAsync(confirm.id); else await revokeMine.mutateAsync(confirm.id);
          setConfirm(null);
          setMessage({ ok: true, text: confirm.kind === "client" ? t("已禁用") : t("已撤销") });
        }} /> : null}
    </div>
  );
}
