// 通知订阅（ADR 0018）：把治理事件推送到外部系统。管理员配置 Webhook、查看投递记录、重投死信。
// 签名密钥创建或轮换时只显示一次；请求体只含事件契约字段，验签方法见 docs/events.md。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useTimeZone } from "../../api/hooks";
import {
  Banner, ConfirmDialog, Count, Drawer, Empty, Hint, PaneHeader, Pill, QueryState, Select, Switch, useMessage,
} from "../../components/ui";
import { formatTime } from "../../format";
import { t } from "../../i18n";
import { SettingsTabs, useRoles } from "../../layout/Shell";

type Subscription = Schemas["SubscriptionOut"];
type EventType = Schemas["EventType"];

export const EVENT_TYPE_LABEL: Record<EventType | "webhook.test", string> = {
  "metric_version.submitted": t("口径提交复核"), "metric_version.published": t("口径发布"),
  "metric_version.rejected": t("口径驳回"), "metric_version.affected": t("口径受影响"),
  "metric_version.invalidated": t("口径失效"), "join_path.approved": t("关联路径批准"),
  "join_path.affected": t("关联路径受影响"), "collect.completed": t("采集完成"), "collect.failed": t("采集失败"),
  "collect.schema_changed": t("采集发现结构变化"), "lineage_import.completed": t("血缘导入完成"),
  "lineage_import.failed": t("血缘导入失败"), "attribution.unavailable": t("变更归因不可用"),
  "auth.login_failure_threshold": t("登录失败超过阈值"),
  // 只由“发送测试”产生，不能订阅
  "webhook.test": t("测试事件"),
};
const GROUPS: { label: string; prefix: string }[] = [
  { label: t("口径"), prefix: "metric_version." }, { label: t("关联路径"), prefix: "join_path." },
  { label: t("采集"), prefix: "collect." }, { label: t("血缘导入"), prefix: "lineage_import." },
  { label: t("其他"), prefix: "" },
];
const STATUS_LABEL: Record<string, string> = { pending: t("待投递"), delivered: t("已投递"), dead: t("已放弃") };
const groupOf = (type: string) => GROUPS.find((g) => g.prefix && type.startsWith(g.prefix)) ?? GROUPS[GROUPS.length - 1];

export function WebhooksPage() {
  const isAdmin = useRoles().has("admin");
  const subs = useQuery({ queryKey: ["webhook-subscriptions"], enabled: isAdmin,
    queryFn: () => unwrap(api.GET("/api/webhook-subscriptions")) });
  const [selected, setSelected] = useState<string>();
  const [editing, setEditing] = useState<Subscription | "new" | null>(null);
  const [secret, setSecret] = useState<{ name: string; secret: string } | null>(null);
  const [message, setMessage] = useMessage();
  const current = subs.data?.find((s) => s.id === selected) ?? subs.data?.[0];

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("设置")}</h1><SettingsTabs current="webhooks" /></div>
        {isAdmin ? <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" onClick={() => setEditing("new")}>{t("新建订阅")}</button>
        </div> : null}
      </div>
      {!isAdmin ? <Empty text={t("只有管理员可以配置通知订阅")} /> : <div className="av-workspace av-webhooks">
        <section className="av-pane av-webhooks__list">
          <PaneHeader title={<>{t("订阅")} <Hint text={t("治理事件按订阅推送到外部地址（JSON，HMAC-SHA256 签名）；投递失败会退避重试，不影响 Avelis 里的操作")} /></>} count={subs.data?.length} />
          <div className="av-pane__body">
            <QueryState query={subs} isEmpty={(d) => !d.length} emptyText={t("还没有订阅")}>
              {(items) => <div className="av-list">{items.map((s) => (
                <button key={s.id} type="button" className={`av-row${s.id === current?.id ? " is-active" : ""}`} onClick={() => setSelected(s.id)}
                  style={{ "--row-cols": "minmax(0, 1fr) auto", minHeight: 58, width: "100%", border: "none", borderBottom: "1px solid var(--line-subtle)", background: s.id === current?.id ? "var(--bg-active)" : "transparent", textAlign: "left", cursor: "pointer" } as React.CSSProperties}>
                  <span className="av-row__main"><span className="av-row__title">{s.name}</span>
                    <span className="av-row__meta av-mono">{s.url}</span></span>
                  {s.enabled ? <Pill tone="ok">{t("启用")}</Pill> : <Pill>{t("停用")}</Pill>}
                </button>))}</div>}
            </QueryState>
          </div>
        </section>
        <section className="av-pane av-webhooks__detail">
          {current ? <SubscriptionDetail key={current.id} sub={current} onEdit={() => setEditing(current)}
            onSecret={(value) => setSecret({ name: current.name, secret: value })} onMessage={setMessage} />
            : <Empty text={t("新建订阅后，这里显示它的投递记录")} />}
          <div style={{ padding: "0 16px" }}><Banner message={message} /></div>
        </section>
      </div>}
      {editing ? <SubscriptionEditor sub={editing === "new" ? null : editing} onClose={() => setEditing(null)}
        onSaved={(sub, created) => {
          setEditing(null); setSelected(sub.id);
          if (created) setSecret({ name: sub.name, secret: created });
          setMessage({ ok: true, text: t("已保存") });
        }} /> : null}
      {secret ? <SecretDialog {...secret} onClose={() => setSecret(null)} /> : null}
    </div>
  );
}

function SubscriptionDetail({ sub, onEdit, onSecret, onMessage }: {
  sub: Subscription; onEdit: () => void; onSecret: (secret: string) => void; onMessage: (m: { ok: boolean; text: string }) => void;
}) {
  const tz = useTimeZone();
  const [status, setStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [confirm, setConfirm] = useState<"delete" | "rotate" | null>(null);
  const PAGE = 50;
  // 投递由 worker 异步完成：有待投递条目时每 5 秒刷新，否则每 30 秒，页面开着就能看到结果和新事件
  const outbox = useQuery({ queryKey: ["event-outbox", sub.id, status, offset],
    refetchInterval: (query) => (query.state.data?.items.some((d) => d.status === "pending") ? 5_000 : 30_000),
    queryFn: () => unwrap(api.GET("/api/event-outbox", { params: { query: {
      subscription_id: sub.id, ...(status ? { status: status as "pending" | "delivered" | "dead" } : {}), limit: PAGE, offset } } })) });
  const toggle = useAction((enabled: boolean) => unwrap(api.PATCH("/api/webhook-subscriptions/{subscription_id}",
    { params: { path: { subscription_id: sub.id } }, body: { enabled } })), ["webhook-subscriptions"]);
  const test = useAction(() => unwrap(api.POST("/api/webhook-subscriptions/{subscription_id}/test",
    { params: { path: { subscription_id: sub.id } } })), ["event-outbox"]);
  const redeliver = useAction((id: string) => unwrap(api.POST("/api/event-outbox/{delivery_id}/redeliver",
    { params: { path: { delivery_id: id } } })), ["event-outbox"]);
  const remove = useAction(() => unwrap(api.DELETE("/api/webhook-subscriptions/{subscription_id}",
    { params: { path: { subscription_id: sub.id } } })), ["webhook-subscriptions", "event-outbox"]);
  const rotate = useAction(() => unwrap(api.POST("/api/webhook-subscriptions/{subscription_id}/rotate-secret",
    { params: { path: { subscription_id: sub.id } } })), ["webhook-subscriptions"]);
  const fail = (e: unknown) => onMessage({ ok: false, text: errorText(e) });

  return <>
    <PaneHeader title={sub.name}>
      <Switch label={t("启用订阅 {{name}}", { name: sub.name })} checked={sub.enabled} disabled={toggle.isPending}
        onChange={(v) => toggle.mutate(v, { onError: fail })} />
      <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!sub.enabled || test.isPending}
        title={sub.enabled ? undefined : t("停用的订阅不能发送测试事件")}
        onClick={() => test.mutate(undefined, { onSuccess: () => onMessage({ ok: true, text: t("已排入测试事件，稍后在投递记录里查看结果") }), onError: fail })}>{t("发送测试")}</button>
      <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={onEdit}>{t("编辑")}</button>
      <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setConfirm("rotate")}>{t("轮换密钥")}</button>
      <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => setConfirm("delete")}>{t("删除")}</button>
    </PaneHeader>
    <div className="av-pane__body av-webhooks__body">
      <dl className="av-kv av-webhooks__kv">
        <dt>{t("地址")}</dt><dd className="av-mono">{sub.url}</dd>
        <dt>{t("事件")}</dt><dd className="av-webhooks__tags">{sub.event_types.map((e) => <span key={e} className="av-tag" title={e}>{EVENT_TYPE_LABEL[e] ?? e}</span>)}</dd>
        <dt>{t("结果")}</dt><dd>{!sub.outcomes || sub.outcomes.length === 2 ? t("成功与失败") : sub.outcomes[0] === "success" ? t("仅成功") : t("仅失败")}</dd>
      </dl>
      <div className="av-webhooks__records-hd">
        <h3>{t("投递记录")} <Count n={outbox.data?.total ?? 0} /></h3>
        <Select label={t("投递状态")} value={status} style={{ width: 140 }} onChange={(v) => { setStatus(v); setOffset(0); }}>
          <option value="">{t("全部状态")}</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </Select>
      </div>
      <QueryState query={outbox} isEmpty={(d) => !d.items.length} emptyText={t("还没有投递记录")}>
        {(page) => <>
          <table className="av-table">
            <thead><tr><th style={{ paddingLeft: 16 }}>{t("事件")}</th><th>{t("状态")}</th><th>{t("尝试")}</th><th>{t("时间")}</th><th className="end" /></tr></thead>
            <tbody>{page.items.map((d) => <tr key={d.id}>
              <td style={{ paddingLeft: 16 }}>{EVENT_TYPE_LABEL[d.payload.type as keyof typeof EVENT_TYPE_LABEL] ?? d.payload.type}
                {d.payload.outcome === "failure" ? <> <Pill tone="danger">{t("失败事件")}</Pill></> : null}
                <div className="av-caption av-mono">{d.payload.object.name ?? `${d.payload.object.type} ${(d.payload.object.id ?? "").slice(0, 8)}`}</div></td>
              <td><Pill tone={d.status === "delivered" ? "ok" : d.status === "dead" ? "danger" : "warn"}>{STATUS_LABEL[d.status] ?? d.status}</Pill>
                {d.last_error ? <div className="av-caption" title={d.last_error}>{d.last_error}</div> : null}</td>
              <td className="av-mono">{d.attempts}</td>
              <td className="av-mono">{formatTime(d.delivered_at ?? d.created_at, tz)}</td>
              <td className="end">{d.status === "dead" ? <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!sub.enabled || redeliver.isPending}
                onClick={() => redeliver.mutate(d.id, { onSuccess: () => onMessage({ ok: true, text: t("已重新排入投递") }), onError: fail })}>{t("重投")}</button> : null}</td>
            </tr>)}</tbody>
          </table>
          {page.total > PAGE ? <div className="av-actions av-webhooks__pager">
            <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>{t("上一页")}</button>
            <span className="av-caption av-mono">{offset + 1}–{Math.min(offset + PAGE, page.total)} / {page.total}</span>
            <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>{t("下一页")}</button>
          </div> : null}
        </>}
      </QueryState>
    </div>
    {confirm === "delete" ? <ConfirmDialog danger title={t("删除订阅 {{name}}？", { name: sub.name })}
      body={t("删除后不再投递新事件，未投递的记录一并删除。")} confirm={t("删除")} onCancel={() => setConfirm(null)}
      onConfirm={async () => { await remove.mutateAsync(undefined); setConfirm(null); onMessage({ ok: true, text: t("已删除") }); }} /> : null}
    {confirm === "rotate" ? <ConfirmDialog title={t("轮换 {{name}} 的签名密钥？", { name: sub.name })}
      body={t("旧密钥立即失效，接收方需要换成新密钥才能验签。")} confirm={t("轮换")} onCancel={() => setConfirm(null)}
      onConfirm={async () => { const out = await rotate.mutateAsync(undefined); setConfirm(null); onSecret(out.secret); }} /> : null}
  </>;
}

function SubscriptionEditor({ sub, onClose, onSaved }: {
  sub: Subscription | null; onClose: () => void; onSaved: (sub: Subscription, secret?: string) => void;
}) {
  const types = useQuery({ queryKey: ["event-types"], queryFn: () => unwrap(api.GET("/api/events/types")) });
  const [form, setForm] = useState({
    name: sub?.name ?? "", url: sub?.url ?? "", event_types: new Set<EventType>(sub?.event_types ?? []),
    outcome: !sub?.outcomes || sub.outcomes.length === 2 ? "both" : sub.outcomes[0],
  });
  const [message, setMessage] = useMessage();
  const body = { name: form.name.trim(), url: form.url.trim(), event_types: [...form.event_types],
    outcomes: form.outcome === "both" ? null : [form.outcome as "success" | "failure"] };
  const save = useAction(async () => sub
    ? { sub: await unwrap(api.PATCH("/api/webhook-subscriptions/{subscription_id}", { params: { path: { subscription_id: sub.id } }, body })), secret: undefined }
    : await unwrap(api.POST("/api/webhook-subscriptions", { body: { ...body, enabled: true } })).then((out) => ({ sub: out as Subscription, secret: out.secret })),
  ["webhook-subscriptions"]);
  const valid = !!body.name && /^https?:\/\//i.test(body.url) && body.event_types.length > 0;
  const toggle = (type: EventType) => setForm((f) => {
    const next = new Set(f.event_types);
    if (next.has(type)) next.delete(type); else next.add(type);
    return { ...f, event_types: next };
  });
  return <Drawer title={sub ? t("编辑订阅") : t("新建订阅")} onClose={() => { if (!save.isPending) onClose(); }} footer={<div className="av-actions">
    <button type="button" className="av-btn av-btn--ghost" disabled={save.isPending} onClick={onClose}>{t("取消")}</button>
    <button type="button" className="av-btn av-btn--primary" disabled={!valid || save.isPending} onClick={() => save.mutate(undefined, {
      onSuccess: (out) => onSaved(out.sub, out.secret), onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
      {save.isPending ? t("保存中…") : t("保存")}</button></div>}>
    <div className="av-webhooks__form">
      <label className="av-field"><span className="av-label">{t("名称")}</span>
        <input className="av-input" value={form.name} maxLength={128} onChange={(e) => setForm({ ...form, name: e.target.value })} /></label>
      <label className="av-field"><span className="av-label">{t("接收地址")} <Hint text={t("只允许 http / https；默认不允许内网与本机地址")} /></span>
        <input className="av-input av-mono" value={form.url} placeholder="https://" onChange={(e) => setForm({ ...form, url: e.target.value })} /></label>
      <div className="av-field"><span className="av-label">{t("结果")}</span>
        <Select label={t("结果")} value={form.outcome} onChange={(v) => setForm({ ...form, outcome: v })}>
          <option value="both">{t("成功与失败")}</option><option value="success">{t("仅成功")}</option><option value="failure">{t("仅失败")}</option>
        </Select></div>
      <fieldset className="av-webhooks__types"><legend className="av-label">{t("事件类型")}</legend>
        <QueryState query={types} rows={3}>{(items) => <>{GROUPS.map((g) => {
          const inGroup = items.filter((i) => groupOf(i.type) === g);
          return inGroup.length ? <div key={g.label} className="av-webhooks__group"><span className="av-caption">{g.label}</span>
            {inGroup.map((i) => <label key={i.type} className="av-webhooks__check">
              <input type="checkbox" checked={form.event_types.has(i.type)} onChange={() => toggle(i.type)} />
              <span>{EVENT_TYPE_LABEL[i.type] ?? i.type}</span><span className="av-caption av-mono">{i.type}</span></label>)}</div> : null;
        })}</>}</QueryState>
      </fieldset>
      <Banner message={message} />
    </div>
  </Drawer>;
}

function SecretDialog({ name, secret, onClose }: { name: string; secret: string; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  return <ConfirmDialog title={t("{{name}} 的签名密钥", { name })} confirm={t("我已保存")} onCancel={onClose} onConfirm={onClose}
    body={t("密钥只显示这一次。接收方用它按 HMAC-SHA256 校验 X-Avelis-Signature；丢失后只能轮换。")}>
    <div className="av-webhooks__secretrow"><code className="av-code av-mono av-webhooks__secret">{secret}</code>
      <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => navigator.clipboard?.writeText(secret).then(() => setCopied(true))}>
        {copied ? t("已复制") : t("复制")}</button></div>
  </ConfirmDialog>;
}
