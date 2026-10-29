// 关联路径：基数由两侧粒度推出；每个涉及的业务域由负责人批准。
import { useState } from "react";

import { api, unwrap } from "../../api/client";
import {
  errorText, useAction, useAsset, useAssets, useDomains, useJoinPaths, useQueryableDatasourceIds,
} from "../../api/hooks";
import { Banner, CARD_LABEL, Drawer, Hint, Icon, Select, Status, shortName, useMessage } from "../../components/ui";
import { useMe, useRoles } from "../../layout/Shell";

export function JoinPaths() {
  const paths = useJoinPaths();
  const domains = useDomains();
  const me = useMe();
  const roles = useRoles();
  const [adding, setAdding] = useState(false);
  const [message, setMessage] = useMessage();
  const decide = useAction((v: { id: string; domain_id: string; decision: "approved" | "rejected" }) => unwrap(api.POST("/api/join-paths/{path_id}/decisions", {
    params: { path: { path_id: v.id } }, body: { domain_id: v.domain_id, decision: v.decision, note: null } })), ["join-paths"]);
  const domainName = (id: string) => domains.data?.find((d) => d.id === id)?.name ?? id;
  return (
    <div className="av-pane__body">
      <div className="av-page">
        <div className="av-pagehd"><div className="av-pagehd__title"><h1>关联路径</h1>
          <Hint text="问数和指标只能使用已批准的关联路径；跨域路径要两个域的负责人都批准" /></div>
          <div className="av-actions"><button type="button" className="av-btn av-btn--primary" disabled={!roles.has("steward") && !roles.has("admin")} onClick={() => setAdding(true)}><Icon name="plus" />新建路径</button></div></div>
        <Banner message={message} />
        <section className="av-panel av-table-wrap">
          <table className="av-table">
            <thead><tr><th style={{ paddingLeft: 16 }}>路径</th><th>左侧</th><th>右侧</th><th>基数</th><th>审批</th><th>状态</th><th className="end" /></tr></thead>
            <tbody>{(paths.data ?? []).map((p) => {
              const approvals = new Map(p.approvals.map((a) => [String(a.domain_id), String(a.decision)]));
              const mine = p.required_domains.filter((d) => domains.data?.find((x) => x.id === d)?.owner_id === me.data?.id && !approvals.has(d));
              return (
                <tr key={p.id}>
                  <td style={{ paddingLeft: 16 }}><b>{p.name}</b><div className="av-caption av-mono">{p.code}</div></td>
                  <td className="av-mono wrap">{shortName(String(p.left.qualified))}.{(p.left.columns as string[]).join(",")}</td>
                  <td className="av-mono wrap">{shortName(String(p.right.qualified))}.{(p.right.columns as string[]).join(",")}</td>
                  <td><span className={`av-tag${["1n", "nn", "unknown"].includes(p.cardinality) ? " av-edge-tag--warn" : ""}`} style={{ position: "static" }}>{CARD_LABEL[p.cardinality]}</span></td>
                  <td className="wrap">{p.required_domains.map((d) => <span key={d} className="av-tag" style={{ marginRight: 4 }}>{domainName(d)} · {approvals.get(d) === "approved" ? "已批准" : approvals.get(d) === "rejected" ? "已驳回" : "待批准"}</span>)}</td>
                  <td><Status value={p.status} /></td>
                  <td className="end"><div className="av-actions">{mine.map((d) => (
                    <button key={d} type="button" className="av-btn av-btn--secondary av-btn--sm"
                      onClick={() => decide.mutate({ id: p.id, domain_id: d, decision: "approved" }, { onSuccess: () => setMessage({ ok: true, text: `已代表${domainName(d)}批准` }), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
                      批准（{domainName(d)}）</button>))}</div></td>
                </tr>
              );
            })}</tbody>
          </table>
        </section>
      </div>
      {adding ? <NewPath onClose={() => setAdding(false)} /> : null}
    </div>
  );
}

function NewPath({ onClose }: { onClose: () => void }) {
  const assets = useAssets();
  const [form, setForm] = useState({ code: "", name: "", left_asset_id: "", left_columns: "", right_asset_id: "", right_columns: "" });
  const left = useAsset(form.left_asset_id || undefined);
  const right = useAsset(form.right_asset_id || undefined);
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/join-paths", { body: {
    ...form, left_columns: form.left_columns.split(",").map((s) => s.trim()).filter(Boolean),
    right_columns: form.right_columns.split(",").map((s) => s.trim()).filter(Boolean),
  } })), ["join-paths"]);
  const queryable = useQueryableDatasourceIds();
  const assetOptions = (assets.data ?? []).filter((a) => queryable.has(a.datasource.id));
  return (
    <Drawer title="新建关联路径" width={520} onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.code || !form.left_asset_id || !form.right_asset_id}
        onClick={() => create.mutate(undefined, { onSuccess: (p) => setMessage({ ok: true, text: `已创建，基数 ${CARD_LABEL[p.cardinality]}，等待负责人批准` }), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>创建</button>
    </>}>
      <div className="av-formrow">
        <label className="av-field"><span className="av-label">名称</span><input className="av-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
        <label className="av-field"><span className="av-label">编码</span><input className="av-input av-mono" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></label>
      </div>
      {(["left", "right"] as const).map((side) => (
        <div key={side} className="av-field">
          <span className="av-label">{side === "left" ? "左侧（明细侧）" : "右侧（被关联）"}</span>
          <Select value={form[`${side}_asset_id`]} onChange={(v) => setForm({ ...form, [`${side}_asset_id`]: v })}>
            <option value="">选择资产</option>
            {assetOptions.map((a) => <option key={a.id} value={a.id}>{a.qualified}</option>)}
          </Select>
          <input className="av-input av-mono" placeholder="关联列，逗号分隔" list={`${side}-cols`} value={form[`${side}_columns`]}
            onChange={(e) => setForm({ ...form, [`${side}_columns`]: e.target.value })} />
          <datalist id={`${side}-cols`}>{((side === "left" ? left : right).data?.columns ?? []).map((c) => <option key={c.name} value={c.name} />)}</datalist>
          <span className="av-caption">粒度：{((side === "left" ? left : right).data?.grains ?? []).map((g) => g.columns.join(",")).join(" / ") || "未知"}</span>
        </div>
      ))}
      <Banner message={message} />
    </Drawer>
  );
}
