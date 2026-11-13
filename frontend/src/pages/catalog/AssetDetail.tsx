// 资产详情：档案（默认，ADR 0022）、字段（推导校验码、业务含义确认）、定义与解析、粒度、同一张表、版本。
import { useState } from "react";
import { Link } from "react-router";

import { api, ApiError, unwrap } from "../../api/client";
import { errorText, useAction, useAsset, useDomains, useDossier, useTimeZone, type AssetDetail as AssetDetailT } from "../../api/hooks";
import {
  Banner, Code, CommentDoc, Crumbs, Empty, Hint, Icon, KIND_LABEL, Pill, Plat, QueryState, Select, Skeleton, Status, Tabs, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";
import { useRoles } from "../../layout/Shell";
import { t } from "../../i18n";
import { AssetDossier } from "./dossier/AssetDossier";
import { FlowBand } from "./dossier/FlowBand";

type Tab = "dossier" | "columns" | "definition" | "grain" | "link" | "versions";

export function AssetDetail({ assetId }: { assetId: string }) {
  const asset = useAsset(assetId);
  if (asset.error instanceof ApiError && asset.error.status === 404) {
    return <Empty text={t("资产不存在")} action={<Link className="av-btn av-btn--secondary" to="/catalog/assets">{t("返回资产目录")}</Link>} />;
  }
  return <QueryState query={asset}>{(a) => <AssetContent key={a.id} a={a} />}</QueryState>;
}

function AssetContent({ a }: { a: AssetDetailT }) {
  const [tab, setTab] = useState<Tab>("dossier");
  const [message, setMessage] = useMessage();
  const dossier = useDossier(a.id);
  const copyQualified = async () => {
    try {
      if (!navigator.clipboard) throw new Error(t("当前浏览器不支持复制，请选择表名后复制"));
      await navigator.clipboard.writeText(a.qualified);
      setMessage({ ok: true, text: t("已复制表名 {{name}}", { name: a.qualified }) });
    } catch (e) { setMessage({ ok: false, text: errorText(e) }); }
  };
  const external = a.kind === "external_table" || a.kind === "catalog_table" || a.linked_from.length > 0;
  return (
    <div className="av-pane__body">
      <div className="av-page av-asset-detail av-dossier-page">
        <div className="av-asset-heading">
          <div className="av-pagehd">
            <Crumbs back={{ label: t("资产"), to: "/catalog/assets" }}
              items={[<><Plat kind={a.datasource.kind} /> {a.datasource.name}</>, <span className="av-mono">{a.qualified.split(".").slice(0, -1).join(".")}</span>]} />
            <div className="av-actions">
              <DomainSelect asset={a} />
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={copyQualified}>{t("复制表名")}</button>
              <Link className="av-btn av-btn--secondary av-btn--sm" to={`/lineage?asset=${a.id}`}><Icon name="lineage" small />{t("看血缘图")}</Link>
            </div>
          </div>
          {dossier.data ? <FlowBand a={a} d={dossier.data} onMessage={setMessage} /> : dossier.isPending ? <Skeleton rows={1} height={160} /> : (
            <>
              <div className="av-pagehd__title">
                <h1 className="av-mono">{a.qualified.split(".").pop()}</h1>
                <span className="av-tag">{KIND_LABEL[a.kind] ?? a.kind}</span>
                {a.version ? <span className="av-tag">v{a.version.version_no}</span> : null}
                {a.status !== "active" ? <Status value={a.status} /> : null}
                {a.datasource.is_test_adapter ? <Pill tone="warn">{t("测试适配器")}</Pill> : null}
              </div>
              {a.version?.comment ? <span className="av-caption">{a.version.comment}</span> : null}
            </>
          )}
        </div>
        <Banner message={message} />
        <Tabs value={tab} onChange={setTab} options={[
          { value: "dossier", label: t("档案") },
          { value: "columns", label: <>{t("字段")} <span className="av-count">{a.columns.length}</span></> },
          ...(a.definition ? [{ value: "definition" as Tab, label: t("定义") }] : []),
          { value: "grain", label: t("粒度") },
          ...(external ? [{ value: "link" as Tab, label: t("同一张表") }] : []),
          { value: "versions", label: <>{t("版本")} <span className="av-count">{a.versions.length}</span></> },
        ]} />
        {tab === "dossier" ? <AssetDossier a={a} dossier={dossier} /> : null}
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
    <span className="av-asset-domain" title={message?.text}>
      <Select value={asset.domain_id ?? ""} label={t("业务域")} disabled={patch.isPending || (!roles.has("steward") && !roles.has("admin"))}
        onChange={(v) => patch.mutate(v || null, { onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
        <option value="">{t("未归属业务域")}</option>
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
      <div className="av-table-wrap"><table className="av-table av-asset-table av-asset-table--columns">
        <thead><tr><th>{t("字段")} <Hint text={t("字段含义以数据库列注释为准；缺注释请在数据库中补齐")} /></th><th>{t("类型")}</th><th>{t("推导")} <Hint text={t("根据上游字段、表达式、过滤与关联条件算出的校验码；加工方式变了它就会变，之前的确认与口径需要重新确认")} /></th><th>{t("业务含义")}</th><th className="end" /></tr></thead>
        <tbody>
          {a.columns.map((c) => {
            const valid = c.confirmations.find((x) => x.valid);
            const stale = c.confirmations.find((x) => !x.valid);
            return (
              <tr key={c.name}>
                <td className="av-mono">{c.name}{c.is_key ? <span className="av-tag av-asset-inline-gap">{t("键")}</span> : null}
                  <div><CommentDoc doc={c.doc} /></div></td>
                <td className="av-mono av-muted">{c.data_type}</td>
                <td><span data-tip={[c.fingerprint_reason, c.fingerprint ? t("校验码 {{code}}", { code: c.fingerprint.slice(0, 8) }) : null].filter(Boolean).join(" · ") || undefined}>
                  <Status value={c.fingerprint_status ?? "unknown"} /></span></td>
                <td>
                  {confirming === c.name ? (
                    <label className="av-field"><span className="av-label">{t("业务含义")}</span><input className="av-input" autoFocus value={meaning} placeholder={t("例如：这一列在业务上表示什么")} onChange={(e) => setMeaning(e.target.value)} /></label>
                  ) : valid ? <span>{valid.meaning}</span>
                    : stale ? <span className="av-status av-status--warn" title={stale.meaning}>{t("推导已变化，原确认不再生效")}</span>
                      : <span className="av-muted">{t("未确认")}</span>}
                </td>
                <td className="end">
                  {confirming === c.name ? (
                    <div className="av-actions">
                      <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setConfirming(null)}>{t("取消")}</button>
                      <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={!meaning.trim() || confirm.isPending}
                        onClick={() => confirm.mutate(c.name, {
                          onSuccess: () => { setConfirming(null); setMeaning(""); setMessage({ ok: true, text: t("已确认 {{name}} 的业务含义", { name: c.name }) }); },
                          onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                        })}>{t("确认")}</button>
                    </div>
                  ) : <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => { setConfirming(c.name); setMeaning(valid?.meaning ?? ""); }}>{t("确认含义")}</button>}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table></div>
    </section>
  );
}

function Definition({ a }: { a: AssetDetailT }) {
  const parse = a.parse;
  const details = (parse?.details ?? {}) as { columns?: { name: string; expression: string; relation: string | null }[]; joins?: unknown[] };
  return (
    <>
      <section className="av-panel">
        <div className="av-panel__hd"><h2 className="av-panel__title">{t("视图定义")} <Hint text={t("（数据库返回的原文）")} /></h2>
          {parse ? <Status value={parse.status} /> : null}</div>
        <div className="av-panel__bd av-asset-stack">
          <Code sql={a.definition?.raw_sql ?? ""} />
          {parse?.reason ? <span className="av-status av-status--warn">{parse.reason}</span> : null}
          <span className="av-caption av-mono">{t("定义校验码")} {a.definition?.canonical_hash.slice(0, 16)} · {parse?.parser_version}</span>
        </div>
      </section>
      {details.columns?.length ? (
        <section className="av-panel">
          <div className="av-panel__hd"><h2 className="av-panel__title">{t("输出列")}</h2></div>
          <div className="av-table-wrap"><table className="av-table av-asset-table">
            <thead><tr><th>{t("列")}</th><th>{t("取值")}</th><th>{t("表达式")}</th></tr></thead>
            <tbody>{details.columns.map((c) => (
              <tr key={c.name}><td className="av-mono">{c.name}</td>
                <td>{c.relation ? <span className="av-tag">{({ value_direct: t("直接"), value_computed: t("计算"), value_aggregated: t("聚合") } as Record<string, string>)[c.relation]}</span> : "—"}</td>
                <td className="av-mono av-muted">{c.expression}</td></tr>))}</tbody>
          </table></div>
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
  const origin: Record<string, string> = { observed: t("数据库键"), parsed: t("定义解析"), declared: t("人工声明"), linked: t("同一张表继承"), passthrough: t("透传") };
  return (
    <section className="av-panel">
      <div className="av-panel__hd"><h2 className="av-panel__title">{t("粒度")} <Hint text={t("唯一确定一行的列。关联时右侧关联列覆盖其粒度为 N:1，否则可能放大")} /></h2></div>
      <Banner message={message} />
      <div className="av-table-wrap"><table className="av-table av-asset-table av-asset-table--grains">
        <thead><tr><th>{t("键")}</th><th>{t("来源")}</th><th>{t("状态")}</th><th>{t("说明")}</th><th className="end" /></tr></thead>
        <tbody>
          {a.grains.map((g, i) => (
            <tr key={i}>
              <td className="av-mono">{g.columns.join(", ")}</td>
              <td>{origin[g.origin] ?? g.origin}</td>
              <td><Status value={g.status} /></td>
              <td className="av-caption">{g.note ?? ""}</td>
              <td className="end">{g.origin === "declared" && g.grain_id ? (
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={verify.isPending}
                  onClick={() => verify.mutate(g.grain_id!, {
                    onSuccess: (r) => setMessage({ ok: r.status === "verified", text: r.status === "verified" ? t("核验通过：键唯一") : t("核验结果：{{status}}{{v2}}", { status: r.status, v2: (r.verify_detail as { duplicate_example?: string[] })?.duplicate_example ? t("，重复示例 {{v1}}", { v1: (r.verify_detail as { duplicate_example: string[] }).duplicate_example.join(", ") }) : "" }) }),
                    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                  })}>{t("核验")}</button>) : null}</td>
            </tr>
          ))}
          {!a.grains.length ? <tr><td colSpan={5} className="av-muted">{t("没有已知粒度（明细模型表、外部表需要人工声明）")}</td></tr> : null}
        </tbody>
      </table></div>
      {roles.has("steward") || roles.has("admin") ? (
        <div className="av-panel__bd av-asset-stack av-asset-grain-form">
          <span className="av-section-label">{t("声明粒度")}</span>
          <div className="av-asset-choices">
            {a.columns.map((c) => (
              <button key={c.name} type="button" className="av-btn av-btn--secondary av-btn--sm av-mono" aria-pressed={cols.includes(c.name)}
                onClick={() => setCols(cols.includes(c.name) ? cols.filter((x) => x !== c.name) : [...cols, c.name])}>{c.name}</button>
            ))}
          </div>
          <div className="av-formrow">
            <label className="av-field"><span className="av-label">{t("依据")}</span><input className="av-input" value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            <button type="button" className="av-btn av-btn--secondary" disabled={!cols.length || !reason.trim() || declare.isPending}
              onClick={() => declare.mutate(undefined, { onSuccess: () => { setCols([]); setReason(""); setMessage({ ok: true, text: t("已声明，可点“核验”用只读查询确认唯一性") }); }, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>{t("声明")}</button>
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
        const match = l.column_match as {
          matched?: string[]; missing_in_source?: string[]; source_only?: string[];
          type_mismatch?: { column: string; external: string; source: string }[];
          type_notes?: { column: string; external: string; source: string }[];
        };
        const missing = match.missing_in_source ?? [];
        const mismatch = match.type_mismatch ?? [];
        const notes = match.type_notes ?? [];
        const sourceOnly = match.source_only ?? [];
        const columns = [...new Set([...missing, ...mismatch.map((n) => n.column), ...notes.map((n) => n.column), ...sourceOnly, ...(match.matched ?? [])])];
        const hasComparison = Array.isArray(match.matched);
        return (
          <section key={l.id} className="av-panel">
            <div className="av-panel__hd"><h2 className="av-panel__title"><Icon name="link" />{t("同一张表")} <Status value={l.status} /></h2></div>
            <div className="av-panel__bd av-asset-link-body">
              <div className="av-asset-diff-summary" role="status">
                {hasComparison ? <>
                  {missing.length ? <Pill tone="danger">{t("源表缺少 {{count}} 列", { count: missing.length })}</Pill> : null}
                  {mismatch.length ? <Pill tone="danger">{t("{{count}} 列类型不一致", { count: mismatch.length })}</Pill> : null}
                  {notes.length ? <Pill tone="warn">{t("{{count}} 列类型规格不同", { count: notes.length })}</Pill> : null}
                  {!missing.length && !mismatch.length ? <span>{t("映射字段核验通过")}</span> : null}
                  <span className="av-caption">{t("{{count}} 列同名", { count: match.matched!.length })}{sourceOnly.length ? t(" · 源表另有 {{count}} 列", { count: sourceOnly.length }) : ""}</span>
                </> : <span className="av-status av-status--warn">{t("尚无字段核验结果")}</span>}
              </div>
              {l.evidence.reason ? <span className="av-caption">{String(l.evidence.reason)}</span> : null}
              <div className="av-asset-link-endpoints">
                <span><span className="av-label">{t("外表")}</span><span className="av-mono">{l.source}</span></span>
                <span><span className="av-label">{t("源表")}</span>{l.target ? <Link className="av-mono" to={`/catalog/assets/${l.target.id}`}>{l.target.qualified}</Link> : <span className="av-muted">{t("未匹配")}</span>}</span>
              </div>
              {columns.length ? <div className="av-table-wrap"><table className="av-table av-asset-table av-asset-diff-table">
                <thead><tr><th>{t("字段")}</th><th>{t("外表")}</th><th>{t("源表")}</th><th>{t("差异")}</th></tr></thead>
                <tbody>{columns.map((name) => {
                  const hard = mismatch.find((n) => n.column === name);
                  const note = notes.find((n) => n.column === name);
                  const detail = hard ?? note;
                  const currentType = a.columns.find((c) => c.name === name)?.data_type;
                  const externalType = detail?.external ?? (l.source === a.qualified ? currentType : undefined);
                  const sourceType = detail?.source ?? (l.target?.id === a.id ? currentType : undefined);
                  return <tr key={name}>
                    <td className="av-mono">{name}</td>
                    <td className="av-mono">{sourceOnly.includes(name) ? t("未映射") : externalType ?? t("存在（类型未提供）")}</td>
                    <td className="av-mono">{missing.includes(name) ? t("缺失") : sourceType ?? t("存在（类型未提供）")}</td>
                    <td>{missing.includes(name) ? <Status value="mismatch" label={t("源表缺失")} /> : hard ? <Status value="mismatch" label={t("类型不一致")} /> : note ? <Status value="candidate" label={t("规格不同，基础类型一致")} /> : sourceOnly.includes(name) ? t("仅源表存在") : t("同名字段")}</td>
                  </tr>;
                })}</tbody>
              </table></div> : null}
              <div className="av-actions">
                <Link className="av-btn av-btn--secondary av-btn--sm" to={`/settings/datasources/${l.target?.datasource_id ?? a.datasource.id}`}>{t("查看数据源")}</Link>
                {l.target ? <Link className="av-btn av-btn--secondary av-btn--sm" to={`/catalog/assets/${l.target.id}`}>{t("查看源表")}</Link> : null}
                <Link className="av-btn av-btn--secondary av-btn--sm" to={`/lineage?asset=${a.id}`}>{t("查看血缘")}</Link>
              </div>
              <details className="av-asset-evidence"><summary>{t("核验依据")}</summary><dl className="av-kv">
                <dt>{t("端点")}</dt><dd className="av-mono">{String(l.evidence.endpoint ?? t("未提供"))} {l.evidence.via ? `· ${String(l.evidence.via)}` : ""}</dd>
                <dt>{t("数据源")}</dt><dd>{String(l.evidence.datasource ?? t("未提供"))}</dd>
                <dt>{t("实例 UUID")}</dt><dd className="av-mono">{String(l.evidence.server_uuid ?? t("未确认"))}</dd>
                <dt>{t("匹配方法")}</dt><dd className="av-mono">{l.method}</dd>
              </dl></details>
            </div>
          </section>
        );
      })}
      {!links.length ? <Empty compact text={t("没有身份映射")} /> : null}
    </>
  );
}
function Versions({ a }: { a: AssetDetailT }) {
  const tz = useTimeZone();
  return (
    <section className="av-panel">
      <div className="av-table-wrap"><table className="av-table av-asset-table av-asset-table--versions">
        <thead><tr><th>{t("版本")}</th><th>{t("名称")}</th><th>{t("首次观察")}</th><th>{t("最后观察")}</th><th>{t("状态")}</th></tr></thead>
        <tbody>{a.versions.map((v) => (
          <tr key={v.id}>
            <td><span className="av-tag">v{v.version_no}</span></td>
            <td className="av-mono">{v.name}</td>
            <td className="av-mono">{formatTime(v.observed_at, tz)}</td>
            <td className="av-mono">{formatTime(v.last_seen_at, tz)}</td>
            <td>{v.superseded_at ? <span className="av-muted">{t("已被取代 ·")} {formatTime(v.superseded_at, tz)}</span> : <Status value="active" label={t("当前")} />}</td>
          </tr>))}</tbody>
      </table></div>
    </section>
  );
}
