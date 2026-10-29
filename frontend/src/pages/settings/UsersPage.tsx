// 用户与角色：列表、新建、编辑角色与启用状态、重置口令（管理员）。业务域成员在“目录 / 业务域”中维护。
import { useState } from "react";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useTimeZone, useUsers } from "../../api/hooks";
import { formatTime } from "../../format";
import { SettingsTabs, useRoles } from "../../layout/Shell";
import { Banner, Drawer, Icon, PaneHeader, Pill, Switch, useMessage } from "../../components/ui";
import type { Schemas } from "../../api/client";

type User = Schemas["UserOut"];

const ROLE_LABEL: Record<string, string> = { admin: "管理员", steward: "维护者", reviewer: "复核者", analyst: "查询者" };
const DOMAIN_ROLE: Record<string, string> = { owner: "负责人", maintainer: "维护者", viewer: "可查询" };

export function UsersPage() {
  const users = useUsers();
  const roles = useRoles();
  const tz = useTimeZone();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);
  const [resetting, setResetting] = useState<User | null>(null);
  const isAdmin = roles.has("admin");
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
              <thead><tr><th style={{ paddingLeft: 16 }}>用户</th><th>账号</th><th>角色</th><th>业务域</th><th>最近登录</th><th>状态</th><th className="end" style={{ paddingRight: 16 }} /></tr></thead>
              <tbody>
                {list.map((u) => (
                  <tr key={u.id}>
                    <td style={{ paddingLeft: 16 }}><span className="av-crumb"><span className="av-avatar">{u.display_name.slice(0, 1)}</span><b>{u.display_name}</b></span></td>
                    <td className="av-mono">{u.username}</td>
                    <td>{u.roles.map((r) => <span key={r} className="av-tag" style={{ marginRight: 4 }}>{ROLE_LABEL[r] ?? r}</span>)}</td>
                    <td>{u.domains.map((d) => <span key={d.id} className="av-tag" style={{ marginRight: 4 }}>{d.name} · {DOMAIN_ROLE[d.role] ?? d.role}</span>)}</td>
                    <td className="av-mono">{u.last_login_at ? formatTime(u.last_login_at, tz) : "—"}</td>
                    <td>{!u.is_active ? <Pill>停用</Pill> : u.must_change_password ? <Pill tone="warn">待改口令</Pill> : <Pill tone="ok">启用</Pill>}</td>
                    <td className="end" style={{ paddingRight: 16 }}>
                      {isAdmin ? (
                        <span className="av-actions">
                          <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditing(u)}>编辑</button>
                          <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setResetting(u)}>重置口令</button>
                        </span>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </div>
      {adding ? <AddUser onClose={() => setAdding(false)} /> : null}
      {editing ? <EditUser user={editing} onClose={() => setEditing(null)} /> : null}
      {resetting ? <ResetPassword user={resetting} onClose={() => setResetting(null)} /> : null}
    </div>
  );
}

function AddUser({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState({ username: "", display_name: "", password: "", roles: ["analyst"] as string[],
    must_change_password: true });
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
      <div className="av-field av-field--inline"><span className="av-label">首次登录须修改口令</span>
        <Switch label="首次登录须修改口令" checked={form.must_change_password} onChange={(v) => setForm({ ...form, must_change_password: v })} /></div>
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


function RoleChecks({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  return (
    <div className="av-field"><span className="av-label">角色</span>
      {Object.entries(ROLE_LABEL).map(([r, label]) => (
        <label key={r} className="av-option"><input type="checkbox" checked={value.includes(r)}
          onChange={() => onChange(value.includes(r) ? value.filter((x) => x !== r) : [...value, r])} />
          <span className="av-option__title">{label}</span></label>
      ))}
    </div>
  );
}

function EditUser({ user, onClose }: { user: User; onClose: () => void }) {
  const [form, setForm] = useState({ display_name: user.display_name, roles: user.roles, is_active: user.is_active });
  const [message, setMessage] = useMessage();
  const save = useAction(() => unwrap(api.PATCH("/api/users/{user_id}", { params: { path: { user_id: user.id } },
    body: form })), ["users", "me"]);
  return (
    <Drawer title={`编辑用户 · ${user.username}`} onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.display_name || save.isPending}
        onClick={() => save.mutate(undefined, { onSuccess: onClose, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>保存</button>
    </>}>
      <label className="av-field"><span className="av-label">姓名</span><input className="av-input" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
      <RoleChecks value={form.roles} onChange={(roles) => setForm({ ...form, roles })} />
      <div className="av-field av-field--inline"><span className="av-label">启用</span>
        <Switch label="启用" checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} /></div>
      {!form.is_active && user.is_active ? <div className="av-banner av-banner--warn">停用后该用户的登录立即失效</div> : null}
      <Banner message={message} />
    </Drawer>
  );
}

function ResetPassword({ user, onClose }: { user: User; onClose: () => void }) {
  const [form, setForm] = useState({ next: "", confirm: "" });
  const [message, setMessage] = useMessage();
  const reset = useAction(() => unwrap(api.POST("/api/users/{user_id}/password", { params: { path: { user_id: user.id } },
    body: { new_password: form.next } })), ["users"]);
  const valid = form.next.length >= 8 && form.next === form.confirm;
  return (
    <Drawer title={`重置口令 · ${user.username}`} onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!valid || reset.isPending}
        onClick={() => reset.mutate(undefined, { onSuccess: onClose, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>重置</button>
    </>}>
      <label className="av-field"><span className="av-label">新口令（至少 8 位）</span><input className="av-input" type="password" autoComplete="new-password" value={form.next} onChange={(e) => setForm({ ...form, next: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">确认新口令</span><input className="av-input" type="password" autoComplete="new-password" value={form.confirm} onChange={(e) => setForm({ ...form, confirm: e.target.value })} /></label>
      <div className="av-banner av-banner--warn">该用户当前的登录立即失效，下次登录须设置新口令</div>
      <Banner message={message} />
    </Drawer>
  );
}
