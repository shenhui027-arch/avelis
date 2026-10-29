// 业务域：负责人、成员（负责人维护）、域内资产与指标。
import { useState } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useAssets, useDomains, useMetrics, useUsers } from "../../api/hooks";
import { Banner, Drawer, Icon, PaneHeader, Select, Status, useMessage } from "../../components/ui";
import { useMe } from "../../layout/Shell";

const ROLE: Record<string, string> = { owner: "负责人", maintainer: "维护者", viewer: "可查询" };

export function DomainDetail({ domainId }: { domainId: string }) {
  const domains = useDomains();
  const users = useUsers();
  const assets = useAssets();
  const metrics = useMetrics();
  const me = useMe();
  const d = domains.data?.find((x) => x.id === domainId);
  const [member, setMember] = useState({ user_id: "", role: "viewer" });
  const [message, setMessage] = useMessage();
  const add = useAction(() => unwrap(api.PUT("/api/domains/{domain_id}/members", {
    params: { path: { domain_id: domainId } }, body: member })), ["domains", "users"]);
  if (!d) return null;
  const owner = users.data?.find((u) => u.id === d.owner_id);
  const isOwner = me.data?.id === d.owner_id || me.data?.roles.includes("admin");
  const domainAssets = (assets.data ?? []).filter((a) => a.domain_id === d.id);
  const domainMetrics = (metrics.data ?? []).filter((m) => m.domain_id === d.id);
  return (
    <div className="av-pane__body">
      <div className="av-page">
        <div className="av-pagehd"><div className="av-pagehd__title"><h1>{d.name}</h1><span className="av-tag">{d.code}</span></div>
          <div className="av-actions"><Link className="av-btn av-btn--secondary av-btn--sm" to={`/lineage?domains=${d.id}`}><Icon name="lineage" small />域血缘</Link></div></div>
        <dl className="av-kv"><dt>负责人</dt><dd>{owner?.display_name ?? "—"}</dd><dt>说明</dt><dd>{d.description ?? "—"}</dd></dl>
        <Banner message={message} />
        <section className="av-panel">
          <PaneHeader title="成员" count={d.members.length} />
          <table className="av-table"><tbody>{d.members.map((m) => (
            <tr key={m.user_id}><td style={{ paddingLeft: 16 }}><span className="av-crumb"><span className="av-avatar">{m.display_name.slice(0, 1)}</span><b>{m.display_name}</b></span></td>
              <td className="av-mono">{m.username}</td><td><span className="av-tag">{ROLE[m.role] ?? m.role}</span></td></tr>))}</tbody></table>
          {isOwner ? (
            <div className="av-panel__bd av-formrow" style={{ borderTop: "1px solid var(--line-subtle)" }}>
              <Select value={member.user_id} label="用户" onChange={(v) => setMember({ ...member, user_id: v })}>
                <option value="">选择用户</option>
                {(users.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.display_name}（{u.username}）</option>)}
              </Select>
              <Select value={member.role} label="角色" style={{ width: 140 }} onChange={(v) => setMember({ ...member, role: v })}>
                <option value="viewer">可查询</option><option value="maintainer">维护者</option>
              </Select>
              <button type="button" className="av-btn av-btn--secondary" disabled={!member.user_id}
                onClick={() => add.mutate(undefined, { onSuccess: () => setMessage({ ok: true, text: "已更新成员" }), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
                <Icon name="plus" />添加</button>
            </div>
          ) : null}
        </section>
        <section className="av-panel">
          <PaneHeader title="指标" count={domainMetrics.length} />
          <table className="av-table"><tbody>{domainMetrics.map((m) => (
            <tr key={m.id}><td style={{ paddingLeft: 16 }}><Link to={`/catalog/metrics/${m.id}`}>{m.name}</Link></td>
              <td>{m.versions[0] ? <Status value={m.versions[0].status} /> : null}</td><td>{m.owner.display_name}</td></tr>))}</tbody></table>
        </section>
        <section className="av-panel">
          <PaneHeader title="资产" count={domainAssets.length} />
          <table className="av-table"><tbody>{domainAssets.map((a) => (
            <tr key={a.id}><td className="av-mono" style={{ paddingLeft: 16 }}><Link to={`/catalog/assets/${a.id}`}>{a.qualified}</Link></td><td>{a.datasource.name}</td></tr>))}</tbody></table>
        </section>
      </div>
    </div>
  );
}

export function NewDomain({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const users = useUsers();
  const [form, setForm] = useState({ code: "", name: "", owner_id: "", description: "" });
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/domains", { body: { ...form, description: form.description || null } })), ["domains"]);
  return (
    <Drawer title="新建业务域" onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.code || !form.name || !form.owner_id}
        onClick={() => create.mutate(undefined, { onSuccess: (d) => onCreated(d.id), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>创建</button>
    </>}>
      <label className="av-field"><span className="av-label">名称</span><input className="av-input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">编码</span><input className="av-input av-mono" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">负责人</span>
        <Select value={form.owner_id} onChange={(v) => setForm({ ...form, owner_id: v })}>
          <option value="">选择负责人</option>
          {(users.data ?? []).map((u) => <option key={u.id} value={u.id}>{u.display_name}（{u.username}）</option>)}
        </Select></label>
      <label className="av-field"><span className="av-label">说明</span><input className="av-input" value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} /></label>
      <Banner message={message} />
    </Drawer>
  );
}
