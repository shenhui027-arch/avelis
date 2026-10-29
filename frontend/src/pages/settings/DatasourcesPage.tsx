// 数据源：目录模式 —— 数据源列表 | 连接、凭据、端点别名、采集批次。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useDatasources, useTimeZone } from "../../api/hooks";
import { formatTime } from "../../format";
import { SettingsTabs, useRoles } from "../../layout/Shell";
import {
  Banner, Drawer, Empty, Hint, Icon, PaneHeader, Pill, Plat, Select, Status, useMessage,
} from "../../components/ui";

type Datasource = Schemas["DatasourceOut"];

export function DatasourcesPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const roles = useRoles();
  const sources = useDatasources();
  const [adding, setAdding] = useState(false);
  const list = sources.data ?? [];
  const selected = list.find((d) => d.id === id) ?? list[0];
  const canEdit = roles.has("admin") || roles.has("steward");

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>设置</h1><SettingsTabs current="datasources" /></div>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" disabled={!canEdit} onClick={() => setAdding(true)}>
            <Icon name="plus" />添加数据源
          </button>
        </div>
      </div>
      <div className="av-workspace" style={{ gridTemplateColumns: "280px minmax(0, 1fr)" }}>
        <section className="av-pane" aria-label="数据源">
          <PaneHeader title="数据源" count={list.length} />
          <div className="av-scroll">
            {list.map((d) => (
              <button key={d.id} type="button" className="av-provider" aria-current={d.id === selected?.id}
                onClick={() => navigate(`/settings/datasources/${d.id}`)}>
                <span className="av-row__main">
                  <span className="av-row__title">{d.name}</span>
                  <span className="av-row__meta av-mono">{d.host}:{d.port}</span>
                </span>
                <span><Plat kind={d.kind} /> <Status value={d.last_run?.status ?? "never"} /></span>
              </button>
            ))}
            {sources.isSuccess && !list.length ? <Empty text="还没有数据源" /> : null}
          </div>
        </section>
        <section className="av-pane" aria-label="数据源详情">
          {selected ? <Detail key={selected.id} ds={selected} canEdit={canEdit} isAdmin={roles.has("admin")} /> : null}
        </section>
      </div>
      {adding ? <AddDatasource onClose={() => setAdding(false)} onCreated={(d) => { setAdding(false); navigate(`/settings/datasources/${d.id}`); }} /> : null}
    </div>
  );
}

function Detail({ ds, canEdit, isAdmin }: { ds: Datasource; canEdit: boolean; isAdmin: boolean }) {
  const tz = useTimeZone();
  const [message, setMessage] = useMessage();
  const runs = useRuns(ds.id);
  const collect = useAction(() => unwrap(api.POST("/api/datasources/{datasource_id}/collect", {
    params: { path: { datasource_id: ds.id }, query: { wait: true } } })), ["datasources", "runs", "assets", "change-sets", "graph"]);
  const [alias, setAlias] = useState({ host: "", port: "" });
  const addAlias = useAction(() => unwrap(api.POST("/api/endpoint-aliases", {
    body: { datasource_id: ds.id, host: alias.host, port: Number(alias.port), note: null } })), ["datasources"]);

  return (
    <>
      <PaneHeader title={<>{ds.name}<Plat kind={ds.kind} /><Pill tone={ds.is_test_adapter ? "warn" : "ok"}>{ds.source_label}</Pill></>}>
        <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={!canEdit || collect.isPending}
          onClick={() => collect.mutate(undefined, {
            onSuccess: (r) => setMessage({ ok: r.status === "succeeded", text: r.status === "succeeded"
              ? `采集完成：新版本 ${String(r.stats?.new_versions ?? 0)}，变更 ${Object.entries(r.stats ?? {}).filter(([k]) => k.startsWith("event:")).reduce((n, [, v]) => n + Number(v), 0)} 项`
              : r.error ?? "采集失败" }),
            onError: (e) => setMessage({ ok: false, text: errorText(e) }),
          })}>
          <Icon name="refresh" small />{collect.isPending ? "采集中" : "重新采集"}
        </button>
      </PaneHeader>
      <div className="av-scroll">
        <div className="av-stack">
          <Banner message={message} />
          <dl className="av-kv">
            <dt>地址</dt><dd className="av-mono">{ds.host}:{ds.port}</dd>
            <dt>采集范围</dt><dd className="av-mono">{scope(ds.options)}</dd>
            <dt>采集间隔</dt><dd>每 {ds.collect_interval_minutes} 分钟 <Hint text="轮询采集，最长延迟一个间隔：未发现变更不代表没有变更" /></dd>
            <dt>实例</dt><dd className="av-mono">{Object.entries(ds.server_identity).filter(([, v]) => v).map(([k, v]) => `${k}=${String(v)}`).join(" · ") || "—"}</dd>
            <dt>凭据</dt>
            <dd>{ds.credentials.length ? ds.credentials.map((c) => (
              <span key={c.purpose} className="av-tag" style={{ marginRight: 6 }}>{c.purpose === "collect" ? "采集" : "只读查询"} · {c.username} · 口令已设置</span>
            )) : "未设置"}{isAdmin ? <Hint text="口令只写不读；更换口令请联系管理员在此处重新录入" /> : null}</dd>
          </dl>

          <PaneHeader title={<>端点别名 <Hint text="同一个库在其他系统里看到的地址（如 StarRocks 中的 shop-mysql:3306），用于把外部表接到这张真实表" /></>} count={ds.aliases.length} />
          <div className="av-table-wrap">
            <table className="av-table">
              <thead><tr><th>地址</th><th>说明</th></tr></thead>
              <tbody>
                {ds.aliases.map((a) => <tr key={a.id}><td className="av-mono">{a.host}:{a.port}</td><td>{a.note ?? ""}</td></tr>)}
              </tbody>
            </table>
          </div>
          {canEdit ? (
            <div className="av-formrow">
              <label className="av-field"><span className="av-label">主机</span>
                <input className="av-input av-mono" value={alias.host} onChange={(e) => setAlias({ ...alias, host: e.target.value })} /></label>
              <label className="av-field" style={{ maxWidth: 120 }}><span className="av-label">端口</span>
                <input className="av-input av-mono" value={alias.port} onChange={(e) => setAlias({ ...alias, port: e.target.value })} /></label>
              <button type="button" className="av-btn av-btn--secondary" disabled={!alias.host || !alias.port}
                onClick={() => addAlias.mutate(undefined, {
                  onSuccess: () => { setAlias({ host: "", port: "" }); setMessage({ ok: true, text: "已登记端点别名" }); },
                  onError: (e) => setMessage({ ok: false, text: errorText(e) }),
                })}><Icon name="plus" />添加</button>
            </div>
          ) : null}

          <PaneHeader title="采集批次" count={runs.data?.length ?? 0} />
          <div className="av-table-wrap">
            <table className="av-table">
              <thead><tr><th>状态</th><th>开始</th><th>触发</th><th className="num">观察</th><th className="num">新版本</th><th className="num">变更</th><th>说明</th></tr></thead>
              <tbody>
                {(runs.data ?? []).map((r) => {
                  const stats = r.stats ?? {};
                  const events = Object.entries(stats).filter(([k]) => k.startsWith("event:")).reduce((n, [, v]) => n + Number(v), 0);
                  return (
                    <tr key={r.id}>
                      <td><Status value={r.status} /></td>
                      <td className="av-mono">{formatTime(r.started_at, tz)}</td>
                      <td>{({ manual: "手动", schedule: "定时", test: "测试" } as Record<string, string>)[r.trigger] ?? r.trigger}</td>
                      <td className="num">{String(stats.assets_observed ?? "—")}</td>
                      <td className="num">{String(stats.new_versions ?? 0)}</td>
                      <td className="num">{events}</td>
                      <td className="av-muted" style={{ whiteSpace: "normal" }}>{r.error ?? ((stats.warnings as string[] | undefined)?.join("；") || "")}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </>
  );
}

function scope(options: Record<string, unknown>): string {
  const dbs = (options.databases as string[] | undefined) ?? [];
  const catalogs = (options.catalogs as string[] | undefined) ?? [];
  return [...dbs, ...catalogs.map((c) => `catalog:${c}`)].join(", ") || "—";
}

function useRuns(id: string) {
  return useQuery({
    queryKey: ["runs", id],
    queryFn: () => unwrap(api.GET("/api/datasources/{datasource_id}/runs", { params: { path: { datasource_id: id } } })),
  });
}

function AddDatasource({ onClose, onCreated }: { onClose: () => void; onCreated: (d: Datasource) => void }) {
  const [form, setForm] = useState({
    kind: "starrocks", name: "", host: "127.0.0.1", port: "9030", databases: "", catalogs: "",
    collectUser: "", collectPassword: "", queryUser: "", queryPassword: "",
  });
  const [message, setMessage] = useMessage();
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });
  const create = useAction(() => unwrap(api.POST("/api/datasources", {
    body: {
      name: form.name, kind: form.kind as "starrocks" | "mysql", host: form.host, port: Number(form.port),
      options: {
        databases: form.databases.split(",").map((s) => s.trim()).filter(Boolean),
        ...(form.catalogs ? { catalogs: form.catalogs.split(",").map((s) => s.trim()).filter(Boolean) } : {}),
      },
      collect_interval_minutes: 15,
      collect: form.collectUser ? { username: form.collectUser, password: form.collectPassword } : null,
      query: form.queryUser ? { username: form.queryUser, password: form.queryPassword } : null,
    },
  })), ["datasources"]);
  return (
    <Drawer title="添加数据源" onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!form.name || !form.databases || create.isPending}
        onClick={() => create.mutate(undefined, { onSuccess: onCreated, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>添加</button>
    </>}>
      <label className="av-field"><span className="av-label">类型</span>
        <Select value={form.kind} onChange={(v) => setForm({ ...form, kind: v, port: v === "mysql" ? "3306" : "9030" })}>
          <option value="starrocks">StarRocks</option><option value="mysql">MySQL（只采集元数据）</option>
        </Select></label>
      <label className="av-field"><span className="av-label">名称</span><input className="av-input" value={form.name} onChange={set("name")} /></label>
      <div className="av-formrow">
        <label className="av-field"><span className="av-label">主机</span><input className="av-input av-mono" value={form.host} onChange={set("host")} /></label>
        <label className="av-field" style={{ maxWidth: 110 }}><span className="av-label">端口</span><input className="av-input av-mono" value={form.port} onChange={set("port")} /></label>
      </div>
      <label className="av-field"><span className="av-label">采集的库 <Hint text="逗号分隔" /></span><input className="av-input av-mono" value={form.databases} onChange={set("databases")} /></label>
      {form.kind === "starrocks" ? (
        <label className="av-field"><span className="av-label">Catalog <Hint text="逗号分隔；只接 JDBC MySQL Catalog" /></span><input className="av-input av-mono" value={form.catalogs} onChange={set("catalogs")} /></label>
      ) : null}
      <div className="av-formrow">
        <label className="av-field"><span className="av-label">采集账号</span><input className="av-input av-mono" value={form.collectUser} onChange={set("collectUser")} /></label>
        <label className="av-field"><span className="av-label">口令</span><input className="av-input" type="password" autoComplete="new-password" value={form.collectPassword} onChange={set("collectPassword")} /></label>
      </div>
      {form.kind === "starrocks" ? (
        <div className="av-formrow">
          <label className="av-field"><span className="av-label">只读查询账号</span><input className="av-input av-mono" value={form.queryUser} onChange={set("queryUser")} /></label>
          <label className="av-field"><span className="av-label">口令</span><input className="av-input" type="password" autoComplete="new-password" value={form.queryPassword} onChange={set("queryPassword")} /></label>
        </div>
      ) : null}
      <Banner message={message} />
    </Drawer>
  );
}
