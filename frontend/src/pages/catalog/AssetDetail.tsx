// 资产详情：字段（推导指纹、业务含义确认）、定义与解析、粒度、同一张表、版本。
import { useState } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useAsset, useDomains, useTimeZone, type AssetDetail as AssetDetailT } from "../../api/hooks";
import {
  Banner, Code, Hint, Icon, KIND_LABEL, Pill, Plat, Select, Status, Tabs, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";
import { useRoles } from "../../layout/Shell";

type Tab = "columns" | "definition" | "grain" | "link" | "versions";

export function AssetDetail({ assetId }: { assetId: string }) {
  const asset = useAsset(assetId);
  const [tab, setTab] = useState<Tab>("columns");
  const a = asset.data;
  if (!a) return null;
  const external = a.kind === "external_table" || a.kind === "catalog_table" || a.linked_from.length > 0;
  return (
    <div className="av-pane__body">
      <div className="av-page">
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <span className="av-caption av-mono"><Plat kind={a.datasource.kind} /> {a.datasource.name} · {a.qualified.split(".").slice(0, 2).join(".")}</span>
          <div className="av-pagehd">
            <div className="av-pagehd__title">
              <h1 className="av-mono">{a.qualified.split(".").pop()}</h1>
              <span className="av-tag">{KIND_LABEL[a.kind] ?? a.kind}</span>
              {a.version ? <span className="av-tag">v{a.version.version_no}</span> : null}
              {a.status !== "active" ? <Status value={a.status} /> : null}
              {a.datasource.is_test_adapter ? <Pill tone="warn">测试适配器</Pill> : null}
            </div>
            <div className="av-actions">
              <DomainSelect asset={a} />
              <Link className="av-btn av-btn--secondary av-btn--sm" to={`/lineage?asset=${a.id}`}><Icon name="lineage" small />血缘</Link>
            </div>
          </div>
          {a.version?.comment ? <span className="av-caption">{a.version.comment}</span> : null}
        </div>
        <Tabs value={tab} onChange={setTab} options={[
          { value: "columns", label: <>字段 <span className="av-count">{a.columns.length}</span></> },
          ...(a.definition ? [{ value: "definition" as Tab, label: "定义" }] : []),
          { value: "grain", label: "粒度" },
          ...(external ? [{ value: "link" as Tab, label: "同一张表" }] : []),
          { value: "versions", label: <>版本 <span className="av-count">{a.versions.length}</span></> },
        ]} />
        {tab === "columns" ? <Columns a={a} /> : null}
        {tab === "definition" ? <Definition a={a} /> : null}
        {tab === "grain" ? <Grains a={a} /> : null}
        {tab === "link" ? <Links a={a} /> : null}
        {tab === "versions" ? <Versions a={a} /> : null}
      </div>
    </div>
  );
}

function DomainSelect({ asset }: { asset: AssetDetailT }) {
  const domains = useDomains();
  const roles = useRoles();
  const [message, setMessage] = useMessage();
  const patch = useAction((domain_id: string | null) => unwrap(api.PATCH("/api/assets/{asset_id}", {
    params: { path: { asset_id: asset.id } }, body: { domain_id } })), ["asset", "assets", "graph"]);
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }} title={message?.text}>
      <Select value={asset.domain_id ?? ""} label="业务域" disabled={!roles.has("steward") && !roles.has("admin")} style={{ width: 140 }}
        onChange={(v) => patch.mutate(v || null, { onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
        <option value="">未归属业务域</option>
        {(domains.data ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
      </Select>
      {message ? <span className="av-status av-status--danger">{message.text}</span> : null}
    </span>
  );
}

function Columns({ a }: { a: AssetDetailT }) {
  const [confirming, setConfirming] = useState<string | null>(null);
  const [meaning, setMeaning] = useState("");
  const [message, setMessage] = useMessage();
  const confirm = useAction((column: string) => unwrap(api.POST("/api/assets/{asset_id}/columns/{column}/confirmations", {
    params: { path: { asset_id: a.id, column } }, body: { meaning, reason: null } })), ["asset"]);
  return (
    <section className="av-panel">
      <Banner message={message} />
      <table className="av-table">
        <thead><tr><th style={{ paddingLeft: 16 }}>字段</th><th>类型</th><th>推导 <Hint text="字段推导指纹：上游字段、表达式、过滤与关联条件。指纹变化时，绑定它的确认与口径不再生效" /></th><th>业务含义</th><th className="end" /></tr></thead>
        <tbody>
          {a.columns.map((c) => {
            const valid = c.confirmations.find((x) => x.valid);
            const stale = c.confirmations.find((x) => !x.valid);
            return (
              <tr key={c.name}>
                <td className="av-mono" style={{ paddingLeft: 16 }}>{c.name}{c.is_key ? <span className="av-tag" style={{ marginLeft: 6 }}>键</span> : null}
                  {c.comment ? <div className="av-caption" style={{ fontFamily: "var(--font-sans)" }}>{c.comment}</div> : null}</td>
                <td className="av-mono av-muted">{c.data_type}</td>
                <td title={c.fingerprint_reason ?? undefined}><Status value={c.fingerprint_status ?? "unknown"} />
                  <span className="av-mono av-muted" style={{ marginLeft: 6 }}>{c.fingerprint?.slice(0, 8)}</span></td>
                <td style={{ whiteSpace: "normal" }}>
                  {confirming === c.name ? (
                    <div className="av-formrow"><input className="av-input" autoFocus value={meaning} placeholder="例如：订单实付金额（不扣退款）" onChange={(e) => setMeaning(e.target.value)} /></div>
                  ) : valid ? <span>{valid.meaning}</span>
                    : stale ? <span className="av-status av-status--warn" title={stale.meaning}>推导已变化，原确认不再生效</span>
                      : <span className="av-muted">未确认</span>}
                </td>
                <td className="end">
                  {confirming === c.name ? (
                    <div className="av-actions">
                      <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setConfirming(null)}>取消</button>
                      <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={!meaning}
                        onClick={() => confirm.mutate(c.name, {
                          onSuccess: () => { setConfirming(null); setMeaning(""); setMessage({ ok: true, text: `已确认 ${c.name} 的业务含义（绑定当前推导指纹）` }); },
                          onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                        })}>确认</button>
                    </div>
                  ) : <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => { setConfirming(c.name); setMeaning(valid?.meaning ?? ""); }}>确认含义</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function Definition({ a }: { a: AssetDetailT }) {
  const parse = a.parse;
  const details = (parse?.details ?? {}) as { columns?: { name: string; expression: string; relation: string | null }[]; joins?: unknown[] };
  return (
    <>
      <section className="av-panel">
        <div className="av-panel__hd"><h2 className="av-panel__title">视图定义 <span className="av-caption">（数据库返回的原文）</span></h2>
          {parse ? <Status value={parse.status} /> : null}</div>
        <div className="av-panel__bd" style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <Code sql={a.definition?.raw_sql ?? ""} />
          {parse?.reason ? <span className="av-status av-status--warn">{parse.reason}</span> : null}
          <span className="av-caption av-mono">规范化指纹 {a.definition?.canonical_hash.slice(0, 16)} · {parse?.parser_version}</span>
        </div>
      </section>
      {details.columns?.length ? (
        <section className="av-panel">
          <div className="av-panel__hd"><h2 className="av-panel__title">输出列</h2></div>
          <table className="av-table">
            <thead><tr><th style={{ paddingLeft: 16 }}>列</th><th>取值</th><th>表达式</th></tr></thead>
            <tbody>{details.columns.map((c) => (
              <tr key={c.name}><td className="av-mono" style={{ paddingLeft: 16 }}>{c.name}</td>
                <td>{c.relation ? <span className="av-tag">{({ value_direct: "直接", value_computed: "计算", value_aggregated: "聚合" } as Record<string, string>)[c.relation]}</span> : "—"}</td>
                <td className="av-mono av-muted">{c.expression}</td></tr>))}</tbody>
          </table>
        </section>
      ) : null}
    </>
  );
}

function Grains({ a }: { a: AssetDetailT }) {
  const roles = useRoles();
  const [cols, setCols] = useState<string[]>([]);
  const [reason, setReason] = useState("");
  const [message, setMessage] = useMessage();
  const declare = useAction(() => unwrap(api.POST("/api/assets/{asset_id}/grains", {
    params: { path: { asset_id: a.id } }, body: { columns: cols, reason } })), ["asset"]);
  const verify = useAction((id: string) => unwrap(api.POST("/api/grains/{grain_id}/verify", { params: { path: { grain_id: id } } })), ["asset"]);
  const origin: Record<string, string> = { observed: "数据库键", parsed: "定义解析", declared: "人工声明", linked: "同一张表继承", passthrough: "透传" };
  return (
    <section className="av-panel">
      <div className="av-panel__hd"><h2 className="av-panel__title">粒度 <Hint text="唯一确定一行的列。关联时右侧关联列覆盖其粒度为 N:1，否则可能放大" /></h2></div>
      <Banner message={message} />
      <table className="av-table">
        <thead><tr><th style={{ paddingLeft: 16 }}>键</th><th>来源</th><th>状态</th><th>说明</th><th className="end" /></tr></thead>
        <tbody>
          {a.grains.map((g, i) => (
            <tr key={i}>
              <td className="av-mono" style={{ paddingLeft: 16 }}>{g.columns.join(", ")}</td>
              <td>{origin[g.origin] ?? g.origin}</td>
              <td><Status value={g.status} /></td>
              <td className="av-caption" style={{ whiteSpace: "normal" }}>{g.note ?? ""}</td>
              <td className="end">{g.origin === "declared" && g.grain_id ? (
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={verify.isPending}
                  onClick={() => verify.mutate(g.grain_id!, {
                    onSuccess: (r) => setMessage({ ok: r.status === "verified", text: r.status === "verified" ? "核验通过：键唯一" : `核验结果：${r.status}${(r.verify_detail as { duplicate_example?: string[] })?.duplicate_example ? `，重复示例 ${(r.verify_detail as { duplicate_example: string[] }).duplicate_example.join(", ")}` : ""}` }),
                    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                  })}>核验</button>) : null}</td>
            </tr>
          ))}
          {!a.grains.length ? <tr><td colSpan={5} className="av-muted" style={{ paddingLeft: 16 }}>没有已知粒度（明细模型表、外部表需要人工声明）</td></tr> : null}
        </tbody>
      </table>
      {roles.has("steward") || roles.has("admin") ? (
        <div className="av-panel__bd" style={{ display: "flex", flexDirection: "column", gap: 8, borderTop: "1px solid var(--line-subtle)" }}>
          <span className="av-section-label">声明粒度</span>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {a.columns.map((c) => (
              <button key={c.name} type="button" className="av-btn av-btn--secondary av-btn--sm av-mono" aria-pressed={cols.includes(c.name)}
                style={cols.includes(c.name) ? { borderColor: "var(--accent)", background: "var(--accent-subtle)" } : undefined}
                onClick={() => setCols(cols.includes(c.name) ? cols.filter((x) => x !== c.name) : [...cols, c.name])}>{c.name}</button>
            ))}
          </div>
          <div className="av-formrow">
            <label className="av-field"><span className="av-label">依据</span><input className="av-input" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            <button type="button" className="av-btn av-btn--secondary" disabled={!cols.length || !reason}
              onClick={() => declare.mutate(undefined, { onSuccess: () => { setCols([]); setReason(""); setMessage({ ok: true, text: "已声明，可点“核验”用只读查询确认唯一性" }); }, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>声明</button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function Links({ a }: { a: AssetDetailT }) {
  const links = [...(a.identity_link ? [a.identity_link] : []), ...a.linked_from];
  return (
    <>
      {links.map((l) => {
        const match = l.column_match as { matched?: string[]; missing_in_source?: string[]; type_mismatch?: unknown[]; type_notes?: { column: string; external: string; source: string }[] };
        return (
          <section key={l.id} className="av-panel">
            <div className="av-panel__hd"><h2 className="av-panel__title"><Icon name="link" />同一张表 <Status value={l.status} /></h2></div>
            <div className="av-panel__bd" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div className="av-chain">
                <span className="av-chain__item"><span className="av-plat">SR</span><span>{l.source}</span></span>
                <span className="av-chain__link" />
                <span className="av-chain__item">
                  <span className="av-plat">MY</span>
                  {l.target ? <Link to={`/catalog/assets/${l.target.id}`}>{l.target.qualified}</Link> : <span className="av-muted">未匹配</span>}
                </span>
              </div>
              <dl className="av-kv">
                <dt>端点</dt><dd className="av-mono">{String(l.evidence.endpoint ?? "—")} {l.evidence.via ? `· ${String(l.evidence.via)}` : ""}</dd>
                <dt>数据源</dt><dd>{String(l.evidence.datasource ?? "—")}</dd>
                <dt>实例</dt><dd className="av-mono">{String(l.evidence.server_uuid ?? "—")}</dd>
                <dt>列匹配</dt><dd>{match.matched?.length ?? 0} 列一致{match.missing_in_source?.length ? `，缺少 ${match.missing_in_source.join(", ")}` : ""}{match.type_mismatch?.length ? `，${match.type_mismatch.length} 列类型不一致` : ""}</dd>
                {l.evidence.reason ? <><dt>原因</dt><dd>{String(l.evidence.reason)}</dd></> : null}
              </dl>
              {match.type_notes?.length ? (
                <table className="av-table"><thead><tr><th>列</th><th>外部表类型</th><th>源表类型</th></tr></thead>
                  <tbody>{match.type_notes.map((n) => <tr key={n.column}><td className="av-mono">{n.column}</td><td className="av-mono">{n.external}</td><td className="av-mono">{n.source}</td></tr>)}</tbody></table>
              ) : null}
            </div>
          </section>
        );
      })}
      {!links.length ? <span className="av-muted">没有身份映射</span> : null}
    </>
  );
}

function Versions({ a }: { a: AssetDetailT }) {
  const tz = useTimeZone();
  return (
    <section className="av-panel">
      <table className="av-table">
        <thead><tr><th style={{ paddingLeft: 16 }}>版本</th><th>名称</th><th>首次观察</th><th>最后观察</th><th>状态</th></tr></thead>
        <tbody>{a.versions.map((v) => (
          <tr key={v.id}>
            <td style={{ paddingLeft: 16 }}><span className="av-tag">v{v.version_no}</span></td>
            <td className="av-mono">{v.name}</td>
            <td className="av-mono">{formatTime(v.observed_at, tz)}</td>
            <td className="av-mono">{formatTime(v.last_seen_at, tz)}</td>
            <td>{v.superseded_at ? <span className="av-muted">已被取代 · {formatTime(v.superseded_at, tz)}</span> : <Status value="active" label="当前" />}</td>
          </tr>))}</tbody>
      </table>
    </section>
  );
}
