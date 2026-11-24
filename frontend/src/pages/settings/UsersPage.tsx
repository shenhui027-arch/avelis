// 用户与角色：搜索、新建（防重复提交）、编辑角色与启用状态、一次性临时口令重置、强制下线（管理员）。业务域成员在“目录 / 业务域”中维护。
import { useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useTimeZone, useUsers } from "../../api/hooks";
import { useDebounced } from "../../components/AssetPicker";
import { Banner, ConfirmDialog, Crumbs, Drawer, Icon, PaneHeader, Pill, QueryState, Switch, useMessage } from "../../components/ui";
import { formatTime } from "../../format";
import { t } from "../../i18n";
import { SettingsTabs, useMe, useRoles } from "../../layout/Shell";

type User = Schemas["UserOut"];

const ROLE_LABEL: Record<string, string> = { admin: t("管理员"), steward: t("维护者"), reviewer: t("复核者"), analyst: t("查询者") };
const DOMAIN_ROLE: Record<string, string> = { owner: t("负责人"), maintainer: t("维护者", { context: "domain" }), viewer: t("可查询") };

export function UsersPage() {
  const [search, setSearch] = useState("");
  const debouncedSearch = useDebounced(search.trim());
  const users = useUsers(debouncedSearch || undefined);
  const me = useMe();
  const roles = useRoles();
  const tz = useTimeZone();
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);
  const [resetTarget, setResetTarget] = useState<User | null>(null);
  const [tempPasswordModal, setTempPasswordModal] = useState<{ username: string; password: string } | null>(null);
  const [copiedTemp, setCopiedTemp] = useState(false);
  const [kickTarget, setKickTarget] = useState<User | null>(null);
  const [anonTarget, setAnonTarget] = useState<User | null>(null);
  const [message, setMessage] = useMessage();

  const isAdmin = roles.has("admin");

  const resetPassword = useAction(
    (userId: string) => unwrap(api.POST("/api/users/{user_id}/reset-password", { params: { path: { user_id: userId } } })),
    ["users"],
  );
  const revokeSessions = useAction(
    (userId: string) => unwrap(api.POST("/api/users/{user_id}/revoke-sessions", { params: { path: { user_id: userId } } })),
    ["users"],
  );
  const anonymizeUser = useAction(
    (userId: string) => unwrap(api.POST("/api/users/{user_id}/anonymize", { params: { path: { user_id: userId } } })),
    ["users"],
  );

  return (
    <div className="av-fill">
      <Crumbs back={{ label: t("工作台"), to: "/" }} items={[t("设置"), t("用户")]} />
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("设置")}</h1><SettingsTabs current="users" /></div>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" disabled={!roles.has("admin")} onClick={() => setAdding(true)}>
            <Icon name="plus" />{t("新建用户")}
          </button>
        </div>
      </div>
      <div className="av-workspace">
        <section className="av-pane">
          {/* 加载完成后再显示计数，加载中与错误态统一接入 QueryState */}
          <PaneHeader title={t("用户")} count={users.isSuccess ? users.data.length : undefined}>
            <span className="av-search">
              <Icon name="search" small />
              <input
                type="search"
                aria-label={t("搜索用户")}
                placeholder={t("搜索姓名、账号或邮箱")}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </span>
          </PaneHeader>
          {message ? <div style={{ padding: "8px 16px 0" }}><Banner message={message} /></div> : null}
          <div className="av-scroll">
            <QueryState
              query={users}
              isEmpty={(list) => !list.length}
              emptyText={debouncedSearch ? t("没有匹配的用户") : t("暂无用户")}
              emptyAction={debouncedSearch ? (
                <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setSearch("")}>
                  {t("清除搜索")}
                </button>
              ) : undefined}
            >
              {(list) => (
                <table className="av-table">
                  <thead>
                    <tr>
                      <th style={{ paddingLeft: 16 }}>{t("用户", { context: "column" })}</th>
                      <th>{t("账号")}</th>
                      <th>{t("角色")}</th>
                      <th>{t("业务域")}</th>
                      <th>{t("最近登录")}</th>
                      <th>{t("状态")}</th>
                      <th className="end" style={{ paddingRight: 16 }}>{t("操作")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((u) => {
                      const isLocal = (u.auth_provider ?? "local") === "local";
                      const isSelf = me.data?.id === u.id;
                      return (
                        <tr key={u.id}>
                          <td style={{ paddingLeft: 16 }}>
                            <span className="av-crumb">
                              <span className="av-avatar">{u.display_name.slice(0, 1)}</span>
                              <b>{u.display_name}</b>
                            </span>
                          </td>
                          <td className="av-mono">{u.username}</td>
                          <td>{u.roles.map((r) => <span key={r} className="av-tag" style={{ marginRight: 4 }}>{ROLE_LABEL[r] ?? r}</span>)}</td>
                          <td>{u.domains.map((d) => <span key={d.id} className="av-tag" style={{ marginRight: 4 }}>{d.name} · {DOMAIN_ROLE[d.role] ?? d.role}</span>)}</td>
                          <td className="av-mono">{u.last_login_at ? formatTime(u.last_login_at, tz) : "—"}</td>
                          <td>{!u.is_active ? <Pill>{t("停用")}</Pill> : u.must_change_password ? <Pill tone="warn">{t("待改密码")}</Pill> : <Pill tone="ok">{t("启用")}</Pill>}</td>
                          <td className="end" style={{ paddingRight: 16 }}>
                            {isAdmin ? (
                              <span className="av-actions">
                                <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditing(u)}>{t("编辑")}</button>
                                <button
                                  type="button"
                                  className="av-btn av-btn--secondary av-btn--sm"
                                  disabled={!isLocal}
                                  title={!isLocal ? t("外部认证账号不支持重置本地密码") : undefined}
                                  onClick={() => setResetTarget(u)}
                                >
                                  {t("重置密码")}
                                </button>
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm"
                                  disabled={isSelf}
                                  title={isSelf ? t("当前正在使用的会话请通过退出登录结束") : undefined}
                                  onClick={() => setKickTarget(u)}
                                >
                                  {t("强制下线")}
                                </button>
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm"
                                  disabled={isSelf || (!u.is_active && u.username.startsWith("anonymized_"))}
                                  onClick={() => setAnonTarget(u)}
                                >
                                  {t("匿名化")}
                                </button>
                              </span>
                            ) : null}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </QueryState>
          </div>
        </section>
      </div>

      {adding ? <AddUser onClose={() => setAdding(false)} /> : null}
      {editing ? <EditUser user={editing} onClose={() => setEditing(null)} /> : null}

      {resetTarget ? (
        <ConfirmDialog
          danger
          title={t("重置 {{username}} 的密码？", { username: resetTarget.username })}
          body={t("系统将生成随机一次性临时口令，并立即吊销该用户所有已登录会话；下次登录时必须修改密码。")}
          confirm={t("生成临时口令")}
          onCancel={() => setResetTarget(null)}
          onConfirm={async () => {
            const out = await resetPassword.mutateAsync(resetTarget.id);
            setResetTarget(null);
            setCopiedTemp(false);
            setTempPasswordModal({ username: out.user.username, password: out.temporary_password });
          }}
        />
      ) : null}

      {tempPasswordModal ? (
        <ConfirmDialog
          title={t("已生成 {{username}} 的临时密码", { username: tempPasswordModal.username })}
          body={t("临时口令只显示这一次，请立即复制并通过安全渠道发送给该用户。")}
          confirm={t("我已保存")}
          onCancel={() => setTempPasswordModal(null)}
          onConfirm={() => setTempPasswordModal(null)}
        >
          <div className="av-webhooks__secretrow">
            <code className="av-code av-mono av-webhooks__secret">{tempPasswordModal.password}</code>
            <button
              type="button"
              className="av-btn av-btn--secondary av-btn--sm"
              onClick={() => navigator.clipboard?.writeText(tempPasswordModal.password).then(() => setCopiedTemp(true))}
            >
              {copiedTemp ? t("已复制") : t("复制")}
            </button>
          </div>
        </ConfirmDialog>
      ) : null}

      {kickTarget ? (
        <ConfirmDialog
          danger
          title={t("强制 {{username}} 下线？", { username: kickTarget.username })}
          body={t("该用户在所有设备上的登录凭证与刷新会话将立即失效，需重新登录。")}
          confirm={t("强制下线")}
          onCancel={() => setKickTarget(null)}
          onConfirm={async () => {
            await revokeSessions.mutateAsync(kickTarget.id);
            setKickTarget(null);
            setMessage({ ok: true, text: t("已强制 {{username}} 下线", { username: kickTarget.username }) });
          }}
        />
      ) : null}

      {anonTarget ? (
        <ConfirmDialog
          danger
          title={t("匿名化注销 {{username}}？", { username: anonTarget.username })}
          body={t("将不可逆地抹除账号名、邮箱与双因素密钥，并吊销全部访问令牌与会话，同时完整保留历史审计哈希链。")}
          confirm={t("确认匿名化")}
          onCancel={() => setAnonTarget(null)}
          onConfirm={async () => {
            await anonymizeUser.mutateAsync(anonTarget.id);
            setAnonTarget(null);
            setMessage({ ok: true, text: t("已完成用户 {{username}} 匿名化注销", { username: anonTarget.username }) });
          }}
        />
      ) : null}
    </div>
  );
}

function AddUser({ onClose }: { onClose: () => void }) {
  const [form, setForm] = useState({
    username: "",
    display_name: "",
    password: "",
    roles: ["analyst"] as string[],
    must_change_password: true,
  });
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/users", { body: form })), ["users"]);
  const toggle = (r: string) => setForm({ ...form, roles: form.roles.includes(r) ? form.roles.filter((x) => x !== r) : [...form.roles, r] });
  return (
    <Drawer
      title={t("新建用户")}
      onClose={() => { if (!create.isPending) onClose(); }}
      footer={(
        <>
          <button type="button" className="av-btn av-btn--secondary" disabled={create.isPending} onClick={onClose}>
            {t("取消")}
          </button>
          <button
            type="button"
            className="av-btn av-btn--primary"
            disabled={create.isPending || !form.username || !form.display_name || form.password.length < 8}
            onClick={() => create.mutate(undefined, {
              onSuccess: onClose,
              onError: (e) => setMessage({ ok: false, text: errorText(e) }),
            })}
          >
            {create.isPending ? t("创建中…") : t("创建")}
          </button>
        </>
      )}
    >
      <label className="av-field"><span className="av-label">{t("账号")}</span><input className="av-input av-mono" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">{t("姓名")}</span><input className="av-input" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">{t("初始密码（至少 8 位）")}</span><input className="av-input" type="password" autoComplete="new-password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} /></label>
      <div className="av-field av-field--inline"><span className="av-label">{t("首次登录须修改密码")}</span>
        <Switch label={t("首次登录须修改密码")} checked={form.must_change_password} onChange={(v) => setForm({ ...form, must_change_password: v })} /></div>
      <div className="av-field"><span className="av-label">{t("角色")}</span>
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
    <div className="av-field"><span className="av-label">{t("角色")}</span>
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
    <Drawer title={t("编辑用户 · {{username}}", { username: user.username })} onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.display_name || save.isPending}
        onClick={() => save.mutate(undefined, { onSuccess: onClose, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>{t("保存")}</button>
    </>}>
      <label className="av-field"><span className="av-label">{t("姓名")}</span><input className="av-input" value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></label>
      <RoleChecks value={form.roles} onChange={(roles) => setForm({ ...form, roles })} />
      <div className="av-field av-field--inline"><span className="av-label">{t("启用")}</span>
        <Switch label={t("启用")} checked={form.is_active} onChange={(v) => setForm({ ...form, is_active: v })} /></div>
      {!form.is_active && user.is_active ? <div className="av-banner av-banner--warn" role="alert">{t("停用后该用户的登录立即失效")}</div> : null}
      <Banner message={message} />
    </Drawer>
  );
}
