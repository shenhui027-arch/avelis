// 审计与防篡改验真（管理员）：历史哈希链状态、按需全链验真、携带请求链路上下文的安全审计检索。
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useTimeZone } from "../../api/hooks";
import { Banner, Crumbs, Empty, Hint, PaneHeader, QueryState, Status } from "../../components/ui";
import { formatTime } from "../../format";
import { t } from "../../i18n";
import { SettingsTabs, useMe } from "../../layout/Shell";

type AuditRow = Schemas["AuditOut"];
type VerifyOut = Schemas["HistoryVerifyOut"];

export function AuditCompliancePage() {
  const me = useMe();
  const tz = useTimeZone();
  const isAdmin = !!me.data?.roles.includes("admin");

  const [actionFilter, setActionFilter] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [requestIdFilter, setRequestIdFilter] = useState("");
  const [ipFilter, setIpFilter] = useState("");
  const [verifyReport, setVerifyReport] = useState<VerifyOut | null>(null);
  const [verifyError, setVerifyError] = useState<string | null>(null);

  const historyStatus = useQuery({
    queryKey: ["history-status"],
    enabled: isAdmin,
    queryFn: ({ signal }) => unwrap(api.GET("/api/history/status", { signal })),
  });

  const auditList = useQuery({
    queryKey: ["audit-events", actionFilter, actorFilter, requestIdFilter, ipFilter],
    enabled: isAdmin,
    queryFn: ({ signal }) => unwrap(
      api.GET("/api/audit", {
        params: {
          query: {
            action: actionFilter.trim() || undefined,
            actor_name: actorFilter.trim() || undefined,
            request_id: requestIdFilter.trim() || undefined,
            client_ip: ipFilter.trim() || undefined,
            limit: 200,
          },
        },
        signal,
      }),
    ),
  });

  const verifyMutation = useMutation({
    mutationFn: () => unwrap(api.GET("/api/history/verify")),
    onSuccess: (report) => {
      setVerifyError(null);
      setVerifyReport(report);
    },
    onError: (err) => {
      setVerifyError(errorText(err));
    },
  });

  return (
    <div className="av-fill">
      <Crumbs back={{ label: t("工作台"), to: "/" }} items={[t("设置"), t("审计与验真")]} />
      <div className="av-pagehd">
        <div className="av-pagehd__title">
          <h1>{t("设置")}</h1>
          <SettingsTabs current="audit" />
        </div>
        <div className="av-actions">
          <button
            type="button"
            className="av-btn av-btn--primary"
            disabled={!isAdmin || verifyMutation.isPending}
            onClick={() => verifyMutation.mutate()}
          >
            {verifyMutation.isPending ? t("验真中…") : t("立即全链验真")}
          </button>
        </div>
      </div>

      <div className="av-workspace">
        <section className="av-pane">
          <PaneHeader title={t("历史哈希链状态")}>
            <Hint text={t("覆盖元数据观察层与治理审计层的全库防篡改哈希链；验证只读执行，不修改历史")} />
          </PaneHeader>
          <div className="av-panel__bd av-asset-stack">
            <QueryState query={historyStatus}>
              {(st) => (
                <dl className="av-kv">
                  <dt>{t("链序号计数")}</dt>
                  <dd className="av-mono">{st.chained_rows ?? 0}</dd>
                  <dt>{t("链头摘要")}</dt>
                  <dd className="av-mono">
                    {st.chain_head ? `#${String(st.chain_head.seq ?? "")} · ${String(st.chain_head.hash ?? "").slice(0, 16)}…` : "—"}
                  </dd>
                  <dt>{t("历史保留期")}</dt>
                  <dd>{st.retention_days > 0 ? t("{{days}} 天", { days: st.retention_days }) : t("永久保留")}</dd>
                  <dt>{t("最近清理")}</dt>
                  <dd className="av-mono">
                    {st.last_purge && typeof st.last_purge.at === "string" ? formatTime(st.last_purge.at, tz) : t("暂无")}
                  </dd>
                </dl>
              )}
            </QueryState>

            {verifyError ? <Banner message={{ ok: false, text: verifyError }} /> : null}
            {verifyReport ? (
              <div className={`av-banner ${verifyReport.ok ? "av-banner--ok" : "av-banner--danger"}`} role="status">
                <strong>{verifyReport.ok ? t("全链验真通过") : t("发现哈希链异常")}</strong>
                <span>
                  {t("已核验 {{count}} 条记录，问题数：{{issues}}", {
                    count: verifyReport.checked_rows ?? 0,
                    issues: verifyReport.issue_count,
                  })}
                </span>
                {verifyReport.first_break ? (
                  <span className="av-mono">
                    {t("首个断点")}：{String((verifyReport.first_break as { reason?: string }).reason ?? JSON.stringify(verifyReport.first_break))}
                  </span>
                ) : null}
              </div>
            ) : null}
          </div>

          <PaneHeader title={t("安全与治理审计日志")} count={auditList.isSuccess ? auditList.data.length : undefined}>
            <div className="av-actions">
              <input
                className="av-input"
                aria-label={t("按动作筛选")}
                placeholder={t("动作（如 answer.view）")}
                value={actionFilter}
                onChange={(e) => setActionFilter(e.target.value)}
              />
              <input
                className="av-input"
                aria-label={t("按账号筛选")}
                placeholder={t("操作人账号")}
                value={actorFilter}
                onChange={(e) => setActorFilter(e.target.value)}
              />
              <input
                className="av-input av-mono"
                aria-label={t("按来源 IP 筛选")}
                placeholder={t("来源 IP")}
                value={ipFilter}
                onChange={(e) => setIpFilter(e.target.value)}
              />
              <input
                className="av-input av-mono"
                aria-label={t("按请求号筛选")}
                placeholder="Request ID"
                value={requestIdFilter}
                onChange={(e) => setRequestIdFilter(e.target.value)}
              />
            </div>
          </PaneHeader>

          <div className="av-scroll">
            <QueryState
              query={auditList}
              isEmpty={(rows) => !rows.length}
              emptyText={t("没有符合条件的审计记录")}
            >
              {(rows: AuditRow[]) => (
                <table className="av-table">
                  <thead>
                    <tr>
                      <th style={{ paddingLeft: 16 }}>{t("时间")}</th>
                      <th>{t("操作人")}</th>
                      <th>{t("动作")}</th>
                      <th>{t("对象")}</th>
                      <th>{t("来源 IP")}</th>
                      <th>{t("请求追踪号")}</th>
                      <th>{t("结果")}</th>
                      <th style={{ paddingRight: 16 }}>{t("详情摘要")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((row) => (
                      <tr key={row.id}>
                        <td className="av-mono" style={{ paddingLeft: 16 }}>{formatTime(row.at, tz)}</td>
                        <td className="av-mono">{row.actor}</td>
                        <td><span className="av-tag av-mono">{row.action}</span></td>
                        <td className="av-mono av-muted">
                          {row.object_type}{row.object_id ? `:${row.object_id.slice(0, 8)}` : ""}
                        </td>
                        <td className="av-mono">{row.client_ip ?? "—"}</td>
                        <td className="av-mono" title={row.user_agent ?? undefined}>
                          {row.request_id ? row.request_id.slice(0, 12) : "—"}
                        </td>
                        <td>
                          <Status value={row.outcome === "failure" ? "failed" : "active"} label={row.outcome === "failure" ? t("失败") : t("成功")} />
                        </td>
                        <td className="av-caption av-mono" style={{ paddingRight: 16 }}>
                          {summarizeDetail(row.detail)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </QueryState>
            {!isAdmin ? <Empty compact text={t("仅管理员可查看审计与全链验真")} /> : null}
          </div>
        </section>
      </div>
    </div>
  );
}

function summarizeDetail(detail: unknown): string {
  if (!detail || typeof detail !== "object") return "—";
  const entries = Object.entries(detail as Record<string, unknown>).filter(
    ([k]) => !["request_id", "client_ip", "user_agent", "_governance"].includes(k),
  );
  if (!entries.length) return "—";
  const text = JSON.stringify(Object.fromEntries(entries));
  return text.length > 90 ? `${text.slice(0, 87)}…` : text;
}
