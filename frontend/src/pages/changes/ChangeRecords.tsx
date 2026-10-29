// 变更记录（留底，ADR 0008）：按数据源、对象、类型、时间、数据库用户筛选；导出 CSV / JSON（带哈希）；管理员核验哈希链。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";

import { api, token, unwrap, type Schemas } from "../../api/client";
import { errorText, useAssets, useDatasources, useTimeZone } from "../../api/hooks";
import { Banner, Count, Empty, Hint, Icon, Pill, Select, Skeleton, Status, shortName, useMessage } from "../../components/ui";
import { formatTime } from "../../format";
import { useMe } from "../../layout/Shell";
import { assetsOfDatasource, recordQuery, type RecordFilters } from "./recordFilters";

type Record = Schemas["ChangeRecordOut"];
type Verify = Schemas["HistoryVerifyOut"];

export const EVENT_LABEL: { [kind: string]: string } = {
  definition_changed: "定义变化", derivation_changed: "字段推导变化", upstream_broken: "上游缺失", column_added: "新增列",
  column_removed: "删除列", column_type_changed: "类型变化", column_rename_candidate: "改名候选", added: "新增",
  deleted: "删除", renamed: "改名", rename_candidate: "改名候选", grain_changed: "粒度变化",
  identity_link_changed: "同一张表匹配变化", external_ref_changed: "外部地址变化", comment_changed: "注释变化",
  column_comment_changed: "列注释变化", server_identity_changed: "实例变化",
};

const ATTRIBUTION: { [status: string]: [("ok" | "warn" | "danger" | "info" | ""), string] } = {
  matched: ["ok", ""], ambiguous: ["warn", "多人，不确定"], unmatched: ["warn", "未知"],
  unavailable: ["danger", "未知 · 审计不可读"], unsupported: ["", "未知 · 未接入审计"], not_applicable: ["", "—"],
};

const CAUSE: { [cause: string]: string } = { no_permission: "未知 · 无权读审计", not_enabled: "未知 · 未开启审计" };

/** 变更事件的数据库用户：只有匹配上唯一用户时显示用户名，其余如实写“未知”并给出原因。 */
export function Actor({ e }: { e: { actor_db_user?: string | null; attribution_status?: string | null; attribution?: { [k: string]: unknown } } }) {
  const status = e.attribution_status ?? "unsupported";
  const cause = String(e.attribution?.cause ?? "");
  const [tone, base] = ATTRIBUTION[status] ?? ["", status];
  const label = status === "unavailable" && cause in CAUSE ? CAUSE[cause] : base;
  const reason = String(e.attribution?.reason ?? "");
  if (status === "matched") {
    return <span className="av-mono" title={reason}>{e.actor_db_user}</span>;
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
      <span className={`av-status${tone ? ` av-status--${tone}` : ""}`}>{label}</span>{reason ? <Hint text={reason} /> : null}
    </span>
  );
}

const PAGE = 50;

export function ChangeRecords() {
  const tz = useTimeZone();
  const me = useMe();
  const sources = useDatasources();
  const assets = useAssets();
  const [filters, setFilters] = useState<RecordFilters>({ datasource_id: "", asset_id: "", column: "", kind: "", actor: "", attribution_status: "", since: "", until: "" });
  const [offset, setOffset] = useState(0);
  const [message, setMessage] = useMessage();
  const set = (k: keyof typeof filters) => (v: string) => { setFilters((f) => ({ ...f, [k]: v, ...(k === "datasource_id" ? { asset_id: "" } : {}) })); setOffset(0); };
  const { query, invalid } = recordQuery(filters, tz);
  const records = useQuery({
    queryKey: ["change-events", query, offset],
    queryFn: () => unwrap(api.GET("/api/change-events", { params: { query: { ...query, limit: PAGE, offset } } })),
  });
  const isAdmin = !!me.data?.roles.includes("admin");
  const [verify, setVerify] = useState<Verify | null>(null);
  const [busy, setBusy] = useState(false);

  const download = async (format: "csv" | "json") => {
    setBusy(true);
    try {
      const params = new URLSearchParams({ ...query, format } as { [k: string]: string });
      const resp = await fetch(`/api/change-events/export?${params}`, { headers: { Authorization: `Bearer ${token.get() ?? ""}` } });
      if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail ?? `导出失败（${resp.status}）`);
      const url = URL.createObjectURL(await resp.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") ?? "")?.[1] ?? `change-events.${format}`;
      link.click();
      URL.revokeObjectURL(url);
      setMessage({ ok: true, text: `已导出 ${resp.headers.get("X-Avelis-Export-Rows")} 条 · SHA-256 ${resp.headers.get("X-Avelis-Export-Sha256")?.slice(0, 16)}…` });
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };
  const runVerify = async () => {
    setBusy(true);
    try {
      setVerify(await unwrap(api.GET("/api/history/verify")));
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const data = records.data;
  const dsAssets = assetsOfDatasource(assets.data ?? [], filters.datasource_id);
  return (
    <section className="av-pane" aria-label="变更记录" style={{ gridColumn: "2 / span 2" }}>
      <div className="av-pane__body">
        <div className="av-page">
          <div className="av-pagehd">
            <div className="av-pagehd__title"><h1>变更记录 <Hint text="数据库结构与定义的每一次变化都留底：只追加、哈希链防篡改，默认保留 3 年。数据库用户来自数据库自己的 DDL 审计，匹配不上时如实写“未知”" /></h1></div>
            <div className="av-actions">
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={busy} onClick={() => download("csv")}><Icon name="download" />CSV</button>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={busy} onClick={() => download("json")}><Icon name="download" />JSON</button>
              {isAdmin ? <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={busy} onClick={runVerify}>核验</button> : null}
            </div>
          </div>
          <Banner message={message} />
          {invalid.length ? <Banner message={{ ok: false, text: `所选日期在业务时区 ${tz} 中不存在（整天被跳过），该日期条件未生效，请换一天` }} /> : null}
          {verify ? <VerifyResult v={verify} tz={tz} /> : null}

          <div className="av-filters" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: 8 }}>
            <Select label="数据源" value={filters.datasource_id} onChange={set("datasource_id")}>
              <option value="">全部数据源</option>
              {(sources.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
            </Select>
            <Select label="资产" value={filters.asset_id} onChange={set("asset_id")}>
              <option value="">全部资产</option>
              {dsAssets.map((a) => <option key={a.id} value={a.id}>{shortName(a.qualified)}</option>)}
            </Select>
            <input className="av-input" aria-label="字段" placeholder="字段" value={filters.column} onChange={(e) => set("column")(e.target.value.trim())} />
            <Select label="变更类型" value={filters.kind} onChange={set("kind")}>
              <option value="">全部类型</option>
              {Object.entries(EVENT_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
            <Select label="数据库用户" value={filters.actor} onChange={set("actor")}>
              <option value="">全部用户</option>
              {(data?.actors ?? []).map((a) => <option key={a} value={a}>{a}</option>)}
            </Select>
            <Select label="归因" value={filters.attribution_status} onChange={set("attribution_status")}>
              <option value="">全部归因</option>
              <option value="matched">已确认用户</option>
              <option value="ambiguous,unmatched,unavailable,unsupported">未知或不确定</option>
            </Select>
            <input className="av-input" type="date" aria-label="起始日期（业务时区）" value={filters.since} aria-invalid={invalid.includes("since")} title={invalid.includes("since") ? `${tz} 中没有这一天` : undefined} onChange={(e) => set("since")(e.target.value)} />
            <input className="av-input" type="date" aria-label="截止日期（不含，业务时区）" value={filters.until} aria-invalid={invalid.includes("until")} title={invalid.includes("until") ? `${tz} 中没有这一天` : undefined} onChange={(e) => set("until")(e.target.value)} />
          </div>

          <section className="av-panel">
            <div className="av-panel__hd">
              <h2 className="av-panel__title">记录 <Count n={data?.total ?? 0} /></h2>
              <div className="av-actions">
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>上一页</button>
                <span className="av-caption av-mono">{data?.total ? `${offset + 1}–${Math.min(offset + PAGE, data.total)}` : "0"}</span>
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!data || offset + PAGE >= data.total} onClick={() => setOffset(offset + PAGE)}>下一页</button>
              </div>
            </div>
            {records.isLoading ? <Skeleton rows={6} /> : null}
            {data && !data.items.length ? <Empty text="没有符合条件的变更" compact /> : null}
            {data?.items.length ? (
              <table className="av-table">
                <thead><tr><th style={{ paddingLeft: 16 }}>发现时间</th><th>对象</th><th>变化</th><th>详情</th><th>数据库用户</th><th className="end">变更集</th></tr></thead>
                <tbody>{data.items.map((e: Record) => (
                  <tr key={e.id}>
                    <td className="av-mono" style={{ paddingLeft: 16, whiteSpace: "nowrap" }}>{formatTime(e.detected_at, tz)}</td>
                    <td className="av-mono" title={`${e.datasource} · ${e.asset ?? ""}`}>{shortName(e.asset) || "—"}{e.column ? `.${e.column}` : ""}</td>
                    <td><Pill>{EVENT_LABEL[e.kind] ?? e.kind}</Pill></td>
                    <td className="av-caption" style={{ whiteSpace: "normal" }}>{recordDetail(e)}</td>
                    <td><Actor e={e} /></td>
                    <td className="end"><Link className="av-btn av-btn--ghost av-btn--sm" to={`/changes/${e.change_set_id}`} title={`链序号 ${e.chain_seq ?? "—"} · ${e.row_hash ?? ""}`}>{e.change_set_id.slice(0, 8)}</Link></td>
                  </tr>))}</tbody>
              </table>
            ) : null}
          </section>
        </div>
      </div>
    </section>
  );
}

function recordDetail(e: Record): string {
  const b = e.before as { [k: string]: unknown };
  const a = e.after as { [k: string]: unknown };
  switch (e.kind) {
    case "column_comment_changed": case "comment_changed": return `${String(b.comment ?? "（无）")} → ${String(a.comment ?? "（无）")}`;
    case "column_type_changed": return `${String(b.data_type)} → ${String(a.data_type)}`;
    case "column_added": return String(a.data_type ?? "");
    case "column_removed": return String(b.data_type ?? "");
    case "renamed": case "rename_candidate": return `${String(b.name)} → ${String(a.name)}`;
    case "definition_changed": return `规范化指纹 ${String(b.hash ?? "").slice(0, 8)} → ${String(a.hash ?? "").slice(0, 8)}`;
    default: return String(a.reason ?? "");
  }
}

const ISSUE: { [kind: string]: string } = {
  content_mismatch: "内容被改", link_broken: "前后不相接", gap_unregistered: "记录缺失", gap_mismatch: "锚点不符",
  unsealed: "未封链", duplicate_seq: "序号重复", mark_mismatch: "状态列被改", duplicate_marks: "标记重复",
  head_mismatch: "链头不符", head_missing: "链头缺失",
};

function VerifyResult({ v, tz }: { v: Verify; tz: string }) {
  return (
    <section className="av-panel">
      <div className="av-panel__hd">
        <h2 className="av-panel__title">哈希链核验 {v.ok ? <Pill tone="ok">完整</Pill> : <Pill tone="danger">发现 {v.issue_count} 处问题</Pill>}</h2>
        <span className="av-caption av-mono">{formatTime(v.verified_at, tz)} · {v.checked_rows} 行 · 链头 {String(v.head?.seq ?? "—")}:{String(v.head?.hash ?? "").slice(0, 12)} · 清理锚点 {v.anchors}</span>
      </div>
      {v.issues.length ? (
        <table className="av-table">
          <thead><tr><th style={{ paddingLeft: 16 }}>序号</th><th>表</th><th>行</th><th>问题</th><th>说明</th></tr></thead>
          <tbody>{v.issues.map((i, n) => (
            <tr key={n}>
              <td className="av-mono" style={{ paddingLeft: 16 }}>{i.seq ?? "—"}</td>
              <td className="av-mono">{i.table}</td>
              <td className="av-mono">{i.row_id?.slice(0, 12) ?? "—"}</td>
              <td><Status value="failed" label={ISSUE[i.kind] ?? i.kind} /></td>
              <td className="av-caption" style={{ whiteSpace: "normal" }}>{i.detail}</td>
            </tr>))}</tbody>
        </table>
      ) : null}
    </section>
  );
}
