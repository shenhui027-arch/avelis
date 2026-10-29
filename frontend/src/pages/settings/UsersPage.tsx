// 用户与角色：列表 + 新建（管理员）。业务域成员在“目录 / 业务域”中维护。
import { useState } from "react";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useUsers } from "../../api/hooks";
import { SettingsTabs, useRoles } from "../../layout/Shell";
import { Banner, Drawer, Icon, PaneHeader, Pill, useMessage } from "../../components/ui";

const ROLE_LABEL: Record<string, string> = { admin: "管理员", steward: "维护者", reviewer: "复核者", analyst: "查询者" };
const DOMAIN_ROLE: Record<string, string> = { owner: "负责人", maintainer: "维护者", viewer: "可查询" };

export function UsersPage() {
  const users = useUsers();
  const roles = useRoles();
  const [adding, setAdding] = useState(false);
  const list = users.data ?? [];
  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>设置</h1><SettingsTabs current="users" /></div>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" disabled={!roles.has("admin")} onClick={() => setAdding(true)}>
            <Icon name="plus" />新建用户
          </button>
        </div>
      </div>
      <div className="av-workspace">
        <section className="av-pane">
          <PaneHeader title="用户" count={list.length} />
          <div className="av-scroll">
            <table className="av-table">
              <thead><tr><th style={{ paddingLeft: 16 }}>用户</th><th>账号</th><th>角色</th><th>业务域</th><th>状态</th></tr></thead>
              <tbody>
                {list.map((u) => (
                  <tr key={u.id}>
                    <td style={{ paddingLeft: 16 }}><span className="av-crumb"><span className="av-avatar">{u.display_name.slice(0, 1)}</span><b>{u.display_name}</b></span></td>
                    <td className="av-mono">{u.username}</td>
                    <td>{u.roles.map((r) => <span key={r} className="av-tag" style={{ marginRight: 4 }}>{ROLE_LABEL[r] ?? r}</span>)}</td>
                    <td>{u.domains.map((d) => <span key={d.id} className="av-tag" style={{ marginRight: 4 }}>{d.name} · {DOMAIN_ROLE[d.role] ?? d.role}</span>)}</td>
                    <td>{u.is_active ? <Pill tone="ok">启用</Pill> : <Pill>停用</Pill>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
      {adding ? <AddUser onClose={() => setAdding(false)} /> : null}
    </div>
  );
}

function AddUser({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState({ username: "", display_name: "", password: "", roles: ["analyst"] as string[] });
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/users", { body: form })), ["users"]);
  const toggle = (r: string) => setForm({ ...form, roles: form.roles.includes(r) ? form.roles.filter((x) => x !== r) : [...form.roles, r] });
  return (
    <Drawer title="新建用户" onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.username || !form.display_name || form.password.length < 8}
        onClick={() => create.mutate(undefined, { onSuccess: onClose, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>创建</button>
    </>}>
      <label className="av-field"><span className="av-label">账号</span><input className="av-input av-mono" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">姓名</span><input className="av-input" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">初始口令（至少 8 位）</span><input className="av-input" type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
      <div className="av-field"><span className="av-label">角色</span>
        {Object.entries(ROLE_LABEL).map(([r, label]) => (
          <label key={r} className="av-option"><input type="checkbox" checked={form.roles.includes(r)} onChange={() => toggle(r)} />
            <span className="av-option__title">{label}</span></label>
        ))}
      </div>
      <Banner message={message} />
    </Drawer>
  );
}
