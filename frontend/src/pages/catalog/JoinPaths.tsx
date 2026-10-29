// 关联路径：基数由两侧粒度推出；每个涉及的业务域由负责人批准。
import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../../api/client";
import {
  errorText, useAction, useAsset, useAssets, useDatasources, useDomains, useJoinPaths, type JoinPath,
} from "../../api/hooks";
import { Banner, CARD_LABEL, Drawer, Empty, Hint, Icon, QueryState, Select, Status, useMessage } from "../../components/ui";
import { useMe, useRoles } from "../../layout/Shell";

function pendingDomains(path: JoinPath, domains: { id: string; owner_id?: string | null }[], userId?: string) {
  if (!userId || !["pending", "affected"].includes(path.status)) return [];
  const decided = new Set(path.approvals.map((a) => String(a.domain_id)));
  return path.required_domains.filter((id) => domains.find((d) => d.id === id)?.owner_id === userId && !decided.has(id));
}

function JoinEndpoints({ path }: { path: JoinPath }) {
  return <div className="av-join-endpoints">{(["left", "right"] as const).map((side) => {
    const endpoint = path[side];
    return <div key={side}><span className="av-caption">{side === "left" ? "左" : "右"}</span><span className="av-mono">
      {typeof endpoint.asset_id === "string" ? <Link to={`/catalog/assets/${endpoint.asset_id}`}>{String(endpoint.qualified ?? "未提供")}</Link> : String(endpoint.qualified ?? "未提供")}
      <span className="av-caption av-mono">{Array.isArray(endpoint.columns) ? endpoint.columns.map(String).join(", ") : "字段未提供"}</span>
    </span></div>;
  })}</div>;
}

export function JoinPaths() {
  const paths = useJoinPaths();
  const domains = useDomains();
  const me = useMe();
  const roles = useRoles();
  const [adding, setAdding] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [review, setReview] = useState<JoinPath | null>(null);
  const [message, setMessage] = useMessage();
  const canCreate = roles.has("steward") || roles.has("admin");
  const domainName = (id: string) => domains.data?.find((d) => d.id === id)?.name ?? id;
  useEffect(() => {
    if (selectedId) document.getElementById(`join-path-${selectedId}`)?.scrollIntoView({ block: "nearest" });
  }, [selectedId, paths.data]);
  const add = canCreate ? <button type="button" className="av-btn av-btn--primary" onClick={() => setAdding(true)}><Icon name="plus" />新建路径</button> : undefined;
  return (
    <div className="av-pane__body av-join-root">
      <div className="av-page av-join-page">
        <div className="av-pagehd"><div className="av-pagehd__title"><h1>关联路径</h1>
          <Hint text="问数和指标只能使用已批准的关联路径；跨域路径需要各域负责人批准" /></div>
          <div className="av-actions">{add}</div></div>
        <Banner message={message} />
        <QueryState query={paths} isEmpty={(rows) => !rows.length} emptyText="暂无关联路径" emptyAction={add}>
          {(rows) => <section className="av-panel av-table-wrap av-join-list">
            <table className="av-table av-join-table">
              <thead><tr><th>路径</th><th>关联字段</th><th>基数</th><th>审批</th><th className="av-join-status">状态</th><th className="av-join-actions end">操作</th></tr></thead>
              <tbody>{rows.map((p) => {
                const approvals = new Map(p.approvals.map((a) => [String(a.domain_id), String(a.decision)]));
                const mine = pendingDomains(p, domains.data ?? [], me.data?.id);
                return <tr key={p.id} id={`join-path-${p.id}`} data-selected={p.id === selectedId}>
                  <td><b>{p.name}</b><div className="av-caption av-mono">{p.code}</div></td>
                  <td><JoinEndpoints path={p} /></td>
                  <td><span className={`av-tag${["1n", "nn", "unknown"].includes(p.cardinality) ? " av-edge-tag--warn" : ""}`}>{CARD_LABEL[p.cardinality] ?? p.cardinality}</span></td>
                  <td><div className="av-join-approvals">{p.required_domains.map((d) => <span key={d} className="av-tag">{domainName(d)} · {approvals.get(d) === "approved" ? "已批准" : approvals.get(d) === "rejected" ? "已驳回" : "待批准"}</span>)}{!p.required_domains.length ? <span className="av-muted">无待审业务域</span> : null}</div></td>
                  <td className="av-join-status"><Status value={p.status} /></td>
                  <td className="av-join-actions end"><button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => { setSelectedId(p.id); setReview(p); }}>{mine.length ? "审核" : "查看"}</button></td>
                </tr>;
              })}</tbody>
            </table>
          </section>}
        </QueryState>
      </div>
      {adding ? <NewPath onClose={() => setAdding(false)} onCreated={(p) => {
        setAdding(false); setSelectedId(p.id); setReview(p);
        setMessage({ ok: true, text: `已创建 ${p.name}，基数 ${CARD_LABEL[p.cardinality] ?? p.cardinality}，等待负责人审核` });
      }} /> : null}
      {review ? <JoinReview key={review.id} path={review} onClose={() => setReview(null)} onUpdated={setReview} /> : null}
    </div>
  );
}

function JoinReview({ path, onClose, onUpdated }: { path: JoinPath; onClose: () => void; onUpdated: (path: JoinPath) => void }) {
  const domains = useDomains();
  const me = useMe();
  const [domain, setDomain] = useState("");
  const [note, setNote] = useState("");
  const [message, setMessage] = useMessage();
  const mine = pendingDomains(path, domains.data ?? [], me.data?.id);
  const selectedDomain = mine.includes(domain) ? domain : mine[0] ?? "";
  const decide = useAction((decision: "approved" | "rejected") => unwrap(api.POST("/api/join-paths/{path_id}/decisions", {
    params: { path: { path_id: path.id } }, body: { domain_id: selectedDomain, decision, note: note.trim() || null },
  })), ["join-paths"]);
  const submit = (decision: "approved" | "rejected") => {
    if (!selectedDomain || decide.isPending) return;
    decide.mutate(decision, {
      onSuccess: (updated) => { onUpdated(updated); setNote(""); setMessage({ ok: true, text: decision === "approved" ? "已批准" : "已驳回" }); },
      onError: (e) => setMessage({ ok: false, text: errorText(e) }),
    });
  };
  return <Drawer title={`关联路径 · ${path.name}`} onClose={() => { if (!decide.isPending) onClose(); }} footer={<div className="av-join-footer" aria-busy={decide.isPending}>
    <button type="button" className="av-btn av-btn--secondary" disabled={decide.isPending} onClick={onClose}>关闭</button>
    {mine.length ? <><button type="button" className="av-btn av-btn--danger" disabled={decide.isPending} onClick={() => submit("rejected")}>驳回</button>
      <button type="button" className="av-btn av-btn--primary" disabled={decide.isPending} onClick={() => submit("approved")}>{decide.isPending ? "提交中…" : "批准"}</button></> : null}
  </div>}>
    <div className="av-join-review-title"><span className="av-mono av-caption">{path.code}</span><Status value={path.status} /></div>
    <JoinEndpoints path={path} />
    <dl className="av-kv"><dt>关联基数</dt><dd>{CARD_LABEL[path.cardinality] ?? path.cardinality}</dd></dl>
    <QueryState query={me}>{() => <QueryState query={domains}>{(rows) => <>
      <div className="av-join-approvals">{path.required_domains.map((id) => {
        const approval = path.approvals.find((a) => a.domain_id === id);
        return <span key={id} className="av-tag">{rows.find((d) => d.id === id)?.name ?? id} · {approval?.decision === "approved" ? "已批准" : approval?.decision === "rejected" ? "已驳回" : "待批准"}</span>;
      })}</div>
      {mine.length ? <>
        <label className="av-field"><span className="av-label">代表业务域</span><Select value={selectedDomain} onChange={setDomain} disabled={decide.isPending} label="代表业务域">
          {mine.map((id) => <option key={id} value={id}>{rows.find((d) => d.id === id)?.name ?? id}</option>)}
        </Select></label>
        <label className="av-field"><span className="av-label">审核意见（选填）</span><textarea className="av-input" rows={3} value={note} disabled={decide.isPending} onChange={(e) => setNote(e.target.value)} /></label>
      </> : <span className="av-caption">当前没有你需要审核的业务域</span>}
      {path.approvals.length ? <details className="av-asset-evidence"><summary>审核记录</summary><div className="av-join-history">{path.approvals.map((approval, i) => <dl className="av-kv" key={i}>
        <dt>业务域</dt><dd>{rows.find((d) => d.id === approval.domain_id)?.name ?? String(approval.domain_id)}</dd>
        <dt>结论</dt><dd><Status value={String(approval.decision)} /></dd>
        <dt>意见</dt><dd>{String(approval.note ?? "未填写")}</dd>
        <dt>时间</dt><dd className="av-mono">{String(approval.at ?? "未提供")}</dd>
      </dl>)}</div></details> : null}
    </>}</QueryState>}</QueryState>
    <Banner message={message} />
    <details className="av-asset-evidence"><summary>基数核验依据</summary><pre className="av-code av-join-evidence">{JSON.stringify(path.cardinality_basis, null, 2)}</pre></details>
  </Drawer>;
}

type NewPathForm = { code: string; name: string; left_asset_id: string; left_columns: string[]; right_asset_id: string; right_columns: string[] };
function NewPath({ onClose, onCreated }: { onClose: () => void; onCreated: (path: JoinPath) => void }) {
  const assets = useAssets();
  const sources = useDatasources();
  const [form, setForm] = useState<NewPathForm>({ code: "", name: "", left_asset_id: "", left_columns: [], right_asset_id: "", right_columns: [] });
  const left = useAsset(form.left_asset_id || undefined);
  const right = useAsset(form.right_asset_id || undefined);
  const [submitted, setSubmitted] = useState(false);
  const [message, setMessage] = useMessage();
  const errors = {
    name: !form.name.trim() ? "请输入名称" : "",
    code: !form.code.trim() ? "请输入编码" : "",
    left_asset_id: !form.left_asset_id ? "请选择左侧资产" : "",
    right_asset_id: !form.right_asset_id ? "请选择右侧资产" : "",
    left_columns: !form.left_columns.length ? "请选择左侧关联字段" : form.left_columns.some((name) => !left.data?.columns.some((c) => c.name === name)) ? "左侧字段已失效，请重新选择" : "",
    right_columns: !form.right_columns.length ? "请选择右侧关联字段" : form.left_columns.length !== form.right_columns.length ? "两侧关联字段数量须一致" : form.right_columns.some((name) => !right.data?.columns.some((c) => c.name === name)) ? "右侧字段已失效，请重新选择" : "",
  };
  const create = useAction(() => unwrap(api.POST("/api/join-paths", { body: { ...form, name: form.name.trim(), code: form.code.trim() } })), ["join-paths"]);
  const submit = () => {
    setSubmitted(true);
    if (create.isPending || Object.values(errors).some(Boolean) || !assets.isSuccess || !sources.isSuccess || !left.isSuccess || !right.isSuccess) return;
    create.mutate(undefined, { onSuccess: onCreated, onError: (e) => setMessage({ ok: false, text: errorText(e) }) });
  };
  const fieldError = (key: keyof typeof errors) => submitted && errors[key] ? <span id={`join-error-${key}`} className="av-join-field-error" role="alert">{errors[key]}</span> : null;
  return <Drawer title="新建关联路径" onClose={() => { if (!create.isPending) onClose(); }} footer={<div className="av-join-footer" aria-busy={create.isPending}>
    <button type="button" className="av-btn av-btn--secondary" disabled={create.isPending} onClick={onClose}>取消</button>
    <button type="submit" form="join-new-path" className="av-btn av-btn--primary" disabled={create.isPending}>{create.isPending ? "创建中…" : "创建"}</button>
  </div>}>
    <form id="join-new-path" className="av-join-form" noValidate onSubmit={(e) => { e.preventDefault(); submit(); }} aria-busy={create.isPending}>
      <div className="av-formrow">{(["name", "code"] as const).map((key) => <label key={key} className="av-field" htmlFor={`join-${key}`}><span className="av-label">{key === "name" ? "名称" : "编码"} *</span>
        <input id={`join-${key}`} className={`av-input${key === "code" ? " av-mono" : ""}`} required value={form[key]} disabled={create.isPending} aria-invalid={submitted && !!errors[key]} aria-describedby={submitted && errors[key] ? `join-error-${key}` : undefined} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
        {fieldError(key)}</label>)}</div>
      <QueryState query={sources}>{(datasources) => <QueryState query={assets}>{(allAssets) => {
        const queryable = new Set(datasources.filter((d) => d.engine.capabilities.includes("query")).map((d) => d.id));
        const options = allAssets.filter((a) => queryable.has(a.datasource.id));
        return options.length ? <>{(["left", "right"] as const).map((side) => {
          const assetKey = `${side}_asset_id` as const;
          const columnsKey = `${side}_columns` as const;
          const detail = side === "left" ? left : right;
          return <fieldset key={side} className="av-join-side" disabled={create.isPending}><legend>{side === "left" ? "左侧（明细侧）" : "右侧（被关联）"}</legend>
            <label className="av-field" htmlFor={`join-${side}-asset`}><span className="av-label">资产 *</span>
              <select id={`join-${side}-asset`} className="av-select" required value={form[assetKey]} aria-invalid={submitted && !!errors[assetKey]} aria-describedby={submitted && errors[assetKey] ? `join-error-${assetKey}` : undefined} onChange={(e) => setForm({ ...form, [assetKey]: e.target.value, [columnsKey]: [] })}>
                <option value="">选择资产</option>{options.map((a) => <option key={a.id} value={a.id}>{a.qualified}</option>)}
              </select>{fieldError(assetKey)}</label>
            {form[assetKey] ? <QueryState query={detail}>{(asset) => <>
              <label className="av-field" htmlFor={`join-${side}-columns`}><span className="av-label">关联字段 *（按对应顺序添加）</span>
                <select id={`join-${side}-columns`} className="av-select av-mono" value="" aria-invalid={submitted && !!errors[columnsKey]} aria-describedby={submitted && errors[columnsKey] ? `join-error-${columnsKey}` : undefined} onChange={(e) => { if (e.target.value) setForm({ ...form, [columnsKey]: [...form[columnsKey], e.target.value] }); }}>
                  <option value="">选择字段</option>{asset.columns.filter((c) => !form[columnsKey].includes(c.name)).map((c) => <option key={c.name} value={c.name}>{c.name} · {c.data_type}</option>)}
                </select>{fieldError(columnsKey)}</label>
              <div className="av-join-column-list">{form[columnsKey].map((name, i) => <button type="button" key={name} className="av-btn av-btn--secondary av-btn--sm av-mono" aria-label={`移除${side === "left" ? "左" : "右"}侧字段 ${name}`} onClick={() => setForm({ ...form, [columnsKey]: form[columnsKey].filter((c) => c !== name) })}>{i + 1}. {name}<Icon name="close" small /></button>)}</div>
              <span className="av-caption">粒度：{asset.grains.map((g) => g.columns.join(", ")).join(" / ") || "未知"}</span>
            </>}</QueryState> : fieldError(columnsKey)}
          </fieldset>;
        })}</> : <Empty compact text="暂无支持查询的资产" action={<Link className="av-btn av-btn--secondary" to="/settings/datasources">查看数据源</Link>} />;
      }}</QueryState>}</QueryState>
      <Banner message={message} />
    </form>
  </Drawer>;
}
