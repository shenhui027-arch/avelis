// 数据源：连接、采集、凭据按任务分组；配置项来自引擎契约。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useParams } from "react-router";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction, useDatasources, useEngines, useTimeZone } from "../../api/hooks";
import { formatTime } from "../../format";
import { SettingsTabs, useRoles } from "../../layout/Shell";
import {
  Banner, Drawer, Empty, Hint, Icon, PaneHeader, Pill, Plat, QueryState, Select, Status, Switch, useMessage,
} from "../../components/ui";
import { t } from "../../i18n";

type Datasource = Schemas["DatasourceOut"];
type CredentialPurpose = "collect" | "query";
type EngineOption = {
  key: string; label: string; type: "list" | "text" | "choice"; required: boolean; hint: string; choices: string[]; default: string;
};

// 能力说明：只列适配器声明的能力，未声明的明确标“不支持”，不做猜测。
const CAPABILITY_LABEL: Record<string, string> = {
  metadata: t("元数据采集"), view_lineage: t("视图血缘"), query: t("问数执行"), live_check: t("执行前核对"),
  external_tables: t("外部表追溯"), identity_target: t("外部表追溯目标"), ddl_audit: t("DDL 审计归因"),
};
const CORE_CAPABILITIES = ["metadata", "view_lineage", "query"];

// EngineOut 暂以扩展字段返回 options，先核对结构，避免提交不完整配置。
function engineOptions(engine?: Schemas["EngineOut"]): EngineOption[] | undefined {
  const raw = engine?.options;
  if (!Array.isArray(raw) || !raw.every((item): item is EngineOption => {
    if (!item || typeof item !== "object") return false;
    const option = item as Record<string, unknown>;
    return typeof option.key === "string" && typeof option.label === "string"
      && (option.type === "list" || option.type === "text" || option.type === "choice")
      && typeof option.required === "boolean" && typeof option.hint === "string"
      && (option.choices === undefined || (Array.isArray(option.choices) && option.choices.every((c) => typeof c === "string")))
      && (option.default === undefined || typeof option.default === "string")
      && (option.type !== "choice" || (Array.isArray(option.choices) && option.choices.length > 0));
  })) return undefined;
  return raw.map((option) => ({ ...option, choices: option.choices ?? [], default: option.default ?? "" }));
}

/** 当前配置下采集账号是否可不填（如 Hive 认证方式 NONE）。 */
function credentialOptional(engine: Schemas["EngineOut"] | undefined, value: (key: string) => string): boolean {
  const free = engine?.credential_free;
  return !!free && free.values.includes(value(free.option));
}

function Capabilities({ engine }: { engine: Schemas["EngineOut"] }) {
  const extra = engine.capabilities.filter((c) => !CORE_CAPABILITIES.includes(c));
  return <span className="av-datasources__capabilities">
    {CORE_CAPABILITIES.map((c) => engine.capabilities.includes(c)
      ? <Pill key={c} tone="ok">{CAPABILITY_LABEL[c]}</Pill>
      : <Pill key={c} title={t("适配器未声明该能力")}>{t("{{capability}}：不支持", { capability: CAPABILITY_LABEL[c] })}</Pill>)}
    {extra.map((c) => <Pill key={c} tone="ok">{CAPABILITY_LABEL[c] ?? c}</Pill>)}
    {engine.note ? <Hint text={engine.note} /> : null}
  </span>;
}

function optionValue(option: EngineOption, input: string): string | string[] {
  if (option.type === "choice") return input || option.default;
  return option.type === "list" ? input.split(",").map((part) => part.trim()).filter(Boolean) : input.trim();
}

function optionDefault(engine: Schemas["EngineOut"] | undefined, key: string): string {
  return engineOptions(engine)?.find((option) => option.key === key)?.default ?? "";
}

function validPort(value: string): boolean {
  return /^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= 65535;
}

export function DatasourcesPage() {
  const { id } = useParams();
  const navigate = useNavigate();
  const roles = useRoles();
  const sources = useDatasources();
  const [adding, setAdding] = useState(false);
  const canEdit = roles.has("admin") || roles.has("steward");
  const addButton = canEdit ? <button type="button" className="av-btn av-btn--primary" onClick={() => setAdding(true)}>
    <Icon name="plus" />{t("添加数据源")}
  </button> : undefined;

  return (
    <div className="av-fill av-datasources">
      <div className="av-pagehd">
        <div className="av-pagehd__title"><h1>{t("设置")}</h1><SettingsTabs current="datasources" /></div>
        <div className="av-actions">{addButton}</div>
      </div>
      <div className="av-workspace av-datasources__workspace">
        <section className="av-pane" aria-label={t("数据源列表")}>
          <PaneHeader title={t("数据源")} count={sources.isSuccess ? sources.data.length : undefined} />
          <div className="av-scroll">
            <QueryState query={sources} isEmpty={(list) => !list.length} emptyText={t("还没有数据源")} emptyAction={addButton} compact>
              {(list) => list.map((d) => (
                <button key={d.id} type="button" className="av-provider" aria-current={d.id === (id ?? list[0]?.id)}
                  onClick={() => navigate(`/settings/datasources/${d.id}`)}>
                  <span className="av-row__main">
                    <span className="av-row__title">{d.name}</span>
                    <span className="av-row__meta av-mono">{d.host}:{d.port}</span>
                  </span>
                  <span className="av-datasources__list-status"><Plat kind={d.kind} /><Status value={d.last_run?.status ?? "never"} /></span>
                </button>
              ))}
            </QueryState>
          </div>
        </section>
        <section className="av-pane" aria-label={t("数据源详情")}>
          <QueryState query={sources} rows={6}>
            {(list) => {
              const selected = id ? list.find((d) => d.id === id) : list[0];
              if (selected) return <Detail key={selected.id} ds={selected} canEdit={canEdit} isAdmin={roles.has("admin")} />;
              return <Empty text={id ? t("数据源不存在") : t("添加数据源后查看连接与采集状态")} compact action={id
                ? <button type="button" className="av-btn av-btn--secondary" onClick={() => navigate("/settings/datasources")}>{t("返回数据源列表")}</button>
                : addButton} />;
            }}
          </QueryState>
        </section>
      </div>
      {adding ? <AddDatasource onClose={() => setAdding(false)} onCreated={(d) => {
        setAdding(false); navigate(`/settings/datasources/${d.id}`);
      }} /> : null}
    </div>
  );
}

function Detail({ ds, canEdit, isAdmin }: { ds: Datasource; canEdit: boolean; isAdmin: boolean }) {
  const tz = useTimeZone();
  const [message, setMessage] = useMessage();
  const runs = useRuns(ds.id);
  const [editingCredential, setEditingCredential] = useState<CredentialPurpose | null>(null);
  const collect = useAction(() => unwrap(api.POST("/api/datasources/{datasource_id}/collect", {
    params: { path: { datasource_id: ds.id }, query: { wait: true } },
  })), ["datasources", "runs", "assets", "change-sets", "graph"]);
  const [alias, setAlias] = useState({ host: "", port: "" });
  const addAlias = useAction(() => unwrap(api.POST("/api/endpoint-aliases", {
    body: { datasource_id: ds.id, host: alias.host.trim(), port: Number(alias.port), note: null },
  })), ["datasources"]);
  const credentialPurposes: CredentialPurpose[] = ds.engine.capabilities.includes("query") ? ["collect", "query"] : ["collect"];
  const exploration = useAction((allow: boolean) => unwrap(api.PUT("/api/datasources/{datasource_id}/exploration", {
    params: { path: { datasource_id: ds.id } }, body: { allow },
  })), ["datasources"]);

  return (
    <>
      <PaneHeader title={<><span className="av-datasources__name">{ds.name}</span><Plat kind={ds.kind} /><Pill tone={ds.is_test_adapter ? "warn" : "ok"}>{ds.source_label}</Pill></>}>
        <button type="button" className="av-btn av-btn--primary" disabled={!canEdit || collect.isPending}
          aria-busy={collect.isPending} onClick={() => collect.mutate(undefined, {
            onSuccess: (r) => setMessage({ ok: r.status === "succeeded", text: r.status === "succeeded"
              ? t("采集完成：新版本 {{v1}}，变更 {{v2}} 项", { v1: String(r.stats?.new_versions ?? 0), v2: eventCount(r.stats) })
              : r.error ?? t("采集失败") }),
            onError: (e) => setMessage({ ok: false, text: errorText(e) }),
          })}>
          <Icon name="refresh" small />{collect.isPending ? t("采集中…") : t("重新采集")}
        </button>
      </PaneHeader>
      <div className="av-scroll">
        <div className="av-datasources__detail">
          <Banner message={message} />
          <div className="av-datasources__summary">
            <span>{t("当前采集")} <Status value={collect.isPending ? "running" : ds.last_run?.status ?? "never"} /></span>
            <QueryState query={runs} rows={1}>
              {(history) => {
                const lastSuccess = history.find((run) => run.status === "succeeded");
                return <span>{t("最近成功")} <span className="av-mono">{lastSuccess ? formatTime(lastSuccess.finished_at ?? lastSuccess.started_at, tz)
                  : history.length ? t("最近 {{count}} 次无成功记录", { count: history.length }) : t("暂无采集记录")}</span></span>;
              }}
            </QueryState>
            {ds.last_run?.error ? <a className="av-btn av-btn--ghost av-btn--sm" href="#datasource-runs">{t("查看采集错误")}</a> : null}
          </div>

          <section className="av-datasources__section" aria-labelledby="datasource-connection">
            <h3 id="datasource-connection">{t("连接")}</h3>
            <dl className="av-kv">
              <dt>{t("引擎")}</dt><dd>{ds.engine.label}</dd>
              <dt>{t("能力")}</dt><dd><Capabilities engine={ds.engine} /></dd>
              <dt>{t("地址")}</dt><dd className="av-mono">{ds.host}:{ds.port}</dd>
              <dt>{t("实例")}</dt><dd className="av-mono">{Object.entries(ds.server_identity).filter(([, v]) => v).map(([k, v]) => `${k}=${String(v)}`).join(" · ") || "—"}</dd>
            </dl>
            <PaneHeader title={<>{t("端点别名")} <Hint text={t("同一实例在其他系统中的地址，用于追溯外部表")} /></>} count={ds.aliases.length} />
            {ds.aliases.length ? <div className="av-table-wrap"><table className="av-table">
              <thead><tr><th>{t("地址")}</th><th>{t("说明")}</th></tr></thead>
              <tbody>{ds.aliases.map((a) => <tr key={a.id}><td className="av-mono">{a.host}:{a.port}</td><td>{a.note ?? "—"}</td></tr>)}</tbody>
            </table></div> : <Empty compact text={t("还没有端点别名")} />}
            {canEdit ? <form className="av-alias-form" aria-busy={addAlias.isPending} onSubmit={(e) => {
              e.preventDefault();
              if (addAlias.isPending || !alias.host.trim() || !validPort(alias.port)) return;
              addAlias.mutate(undefined, {
                onSuccess: () => { setAlias({ host: "", port: "" }); setMessage({ ok: true, text: t("已登记端点别名") }); },
                onError: (error) => setMessage({ ok: false, text: errorText(error) }),
              });
            }}>
              <label className="av-field"><span className="av-label">{t("主机")}</span><input className="av-input av-mono" required maxLength={255} disabled={addAlias.isPending}
                value={alias.host} onChange={(e) => setAlias({ ...alias, host: e.target.value })} /></label>
              <label className="av-field"><span className="av-label">{t("端口")}</span><input className="av-input av-mono" type="number" min={1} max={65535} required disabled={addAlias.isPending}
                value={alias.port} onChange={(e) => setAlias({ ...alias, port: e.target.value })} /></label>
              <button type="submit" className="av-btn av-btn--secondary" disabled={!alias.host.trim() || !validPort(alias.port) || addAlias.isPending}>
                {addAlias.isPending ? t("添加中…") : t("添加")}
              </button>
            </form> : null}
          </section>

          <section className="av-datasources__section" aria-labelledby="datasource-collection">
            <h3 id="datasource-collection">{t("采集")}</h3>
            <dl className="av-kv">
              <dt>{t("范围")}</dt><dd className="av-mono">{scope(ds)}</dd>
              <dt>{t("间隔")}</dt><dd>{t("每 {{count}} 分钟", { count: ds.collect_interval_minutes })} <Hint text={t("轮询采集；未发现变更不代表没有变更")} /></dd>
            </dl>
            <div id="datasource-runs" tabIndex={-1}><PaneHeader title={<>{t("采集批次")} <Hint text={t("展示最近 50 次采集记录")} /></>} count={runs.isSuccess ? runs.data.length : undefined} /></div>
            <QueryState query={runs} isEmpty={(history) => !history.length} emptyText={t("还没有采集记录")} compact>
              {(history) => <div className="av-table-wrap"><table className="av-table av-datasources__runs">
                <thead><tr><th>{t("状态")}</th><th>{t("开始")}</th><th>{t("触发")}</th><th className="num">{t("观察")}</th><th className="num">{t("新版本")}</th><th className="num">{t("变更")}</th><th>{t("说明")}</th></tr></thead>
                <tbody>{history.map((r) => {
                  const stats = r.stats ?? {};
                  return <tr key={r.id}>
                    <td><Status value={r.status} /></td><td className="av-mono">{formatTime(r.started_at, tz)}</td>
                    <td>{({ manual: t("手动"), schedule: t("定时"), test: t("测试") } as Record<string, string>)[r.trigger] ?? r.trigger}</td>
                    <td className="num">{String(stats.assets_observed ?? "—")}</td><td className="num">{String(stats.new_versions ?? "—")}</td>
                    <td className="num">{r.status === "succeeded" || Object.keys(stats).length ? eventCount(stats) : "—"}</td>
                    <td className="av-muted av-datasources__run-note">{r.error ?? (Array.isArray(stats.warnings) ? stats.warnings.join(t("；")) : "—")}</td>
                  </tr>;
                })}</tbody>
              </table></div>}
            </QueryState>
          </section>

          <CommentCoverage ds={ds} />

          <section className="av-datasources__section" aria-labelledby="datasource-exploration">
            <h3 id="datasource-exploration">{t("探索查询")} <Hint text={t("分析智能体可以按血缘与字段注释，在当前用户可见的已采集表上写只读 SELECT。结果标为“探索查询（未经口径审核）”")} /></h3>
            {ds.exploration_supported ? <dl className="av-kv">
              <dt>{t("状态")}</dt><dd className="av-datasources__switch">
                <Switch label={t("允许探索查询")} checked={!!ds.allow_exploration} disabled={!isAdmin || exploration.isPending}
                  onChange={(value: boolean) => exploration.mutate(value, { onError: (e) => setMessage({ ok: false, text: errorText(e) }) })} />
                <span className="av-muted">{ds.allow_exploration ? t("已开启") : t("已关闭")}{isAdmin ? "" : t("（只有管理员可以修改）")}</span></dd>
            </dl> : <span className="av-muted">{t("该引擎尚不支持探索查询（第一版支持 StarRocks 与 PostgreSQL）")}</span>}
          </section>

          <section className="av-datasources__section" aria-labelledby="datasource-credentials">
            <h3 id="datasource-credentials">{t("凭据")} <Hint text={t("密码只写不读，更换后使用新凭据连接")} /></h3>
            {credentialPurposes.map((purpose) => {
              const credential = ds.credentials.find((c) => c.purpose === purpose);
              return <div className="av-datasources__credential" key={purpose}>
                <span className="av-row__main"><span className="av-row__title">{purpose === "collect" ? t("采集账号") : t("只读查询账号")}</span>
                  <span className="av-row__meta">{credential ? <><span className="av-mono">{credential.username}</span> {t("· 密码已设置")}</>
                    : purpose === "collect" && credentialOptional(ds.engine, (key) => String(ds.options[key] ?? optionDefault(ds.engine, key)))
                      ? t("未设置（当前认证方式可不填）") : t("未设置")}</span>
                  {credential ? <span className="av-caption">{t("更新于")} {formatTime(credential.updated_at, tz)}</span> : null}
                </span>
                {isAdmin ? <button type="button" className="av-btn av-btn--secondary" onClick={() => setEditingCredential(purpose)}>{credential ? t("更换") : t("设置")}</button> : null}
              </div>;
            })}
          </section>
        </div>
      </div>
      {editingCredential ? <DatasourceCredential ds={ds} purpose={editingCredential} onClose={() => setEditingCredential(null)} onSaved={() => {
        setEditingCredential(null); setMessage({ ok: true, text: t("凭据已更新") });
      }} /> : null}
    </>
  );
}

const DOC_STATUS: Record<string, string> = { inherited: t("本列缺注释（上游有）"), missing: t("注释缺失") };

/** 字段注释完整度：字段含义以数据库列注释为准，缺口交给数据库负责人补齐（可导出清单）。 */
function CommentCoverage({ ds }: { ds: Datasource }) {
  const gaps = useQuery({
    queryKey: ["comment-gaps", ds.id],
    queryFn: () => unwrap(api.GET("/api/metadata/comment-gaps", { params: { query: { datasource_id: ds.id } } })),
  });
  const exportCsv = (items: Schemas["CommentGapOut"][]) => {
    const cell = (v: string | null | undefined) => `"${(v ?? "").replaceAll('"', '""')}"`;
    const lines = [[t("资产"), t("字段"), t("状态"), t("上游注释（可沿用）"), t("上游字段")].map(cell).join(","),
      ...items.map((x) => [x.asset, x.column, DOC_STATUS[x.status] ?? x.status, x.suggestion, x.source].map(cell).join(","))];
    const url = URL.createObjectURL(new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = t("{{name}}-注释缺失.csv", { name: ds.name });
    link.click();
    URL.revokeObjectURL(url);
  };
  return (
    <section className="av-datasources__section" aria-labelledby="datasource-comments">
      <h3 id="datasource-comments">{t("字段注释")} <Hint text={t("字段含义以数据库列注释为准。Avelis 不另设显示名，缺注释的列在问数和资产中如实标出，请在数据库中补齐")} /></h3>
      <QueryState query={gaps} compact>
        {(data) => {
          const summary = data.datasources.find((x) => x.datasource_id === ds.id);
          if (!summary || !summary.total) return <Empty text={t("还没有采集到字段")} compact />;
          const rate = summary.completeness == null ? "—" : `${Math.round(summary.completeness * 1000) / 10}%`;
          return <>
            <dl className="av-kv">
              <dt>{t("完整度")}</dt><dd><strong>{rate}</strong> <span className="av-muted">{summary.ok + summary.linked} / {t("{{count}} 列有注释", { count: summary.total })}</span></dd>
              <dt>{t("缺口")}</dt><dd className="av-coverage__gaps">{summary.gaps ? <>
                {summary.missing ? <Pill tone="warn">{t("{{count}} 列注释缺失", { count: summary.missing })}</Pill> : null}
                {summary.inherited ? <Pill tone="warn">{t("{{count}} 列可沿用上游", { count: summary.inherited })}</Pill> : null}
              </> : <Pill tone="ok">{t("无")}</Pill>}</dd>
            </dl>
            {data.items.length ? <>
              <PaneHeader title={t("缺注释的列")} count={data.items.length}>
                <button type="button" className="av-btn av-btn--secondary" onClick={() => exportCsv(data.items)}><Icon name="download" />{t("导出 CSV")}</button>
              </PaneHeader>
              <div className="av-table-wrap"><table className="av-table">
                <thead><tr><th>{t("资产")}</th><th>{t("字段")}</th><th>{t("状态")}</th><th>{t("上游注释（可沿用）")}</th></tr></thead>
                <tbody>{data.items.slice(0, 50).map((x) => <tr key={`${x.asset_id}.${x.column}`}>
                  <td className="av-mono">{x.asset}</td><td className="av-mono">{x.column}</td>
                  <td><Pill tone="warn">{DOC_STATUS[x.status] ?? x.status}</Pill></td>
                  <td title={x.source ?? undefined}>{x.suggestion ?? <span className="av-muted">—</span>}</td>
                </tr>)}</tbody>
              </table></div>
              {data.items.length > 50 ? <span className="av-caption">{t("只显示前 50 列，完整清单请导出")}</span> : null}
            </> : null}
          </>;
        }}
      </QueryState>
    </section>
  );
}

function eventCount(stats?: Record<string, unknown> | null): number {
  return Object.entries(stats ?? {}).filter(([key]) => key.startsWith("event:")).reduce((total, [, value]) => total + Number(value), 0);
}

function scope(ds: Datasource): string {
  const options = engineOptions(ds.engine);
  return Object.entries(ds.options).map(([key, value]) => `${options?.find((option) => option.key === key)?.label ?? key}：${optionText(value)}`).join(" · ") || "—";
}

// 选项值可能是列表或映射（如 catalog_databases：{catalog_a: ["db_a"]}）
function optionText(value: unknown): string {
  if (Array.isArray(value)) return value.map(optionText).join(", ");
  if (value && typeof value === "object") return Object.entries(value).map(([k, v]) => `${k}（${optionText(v)}）`).join(", ");
  return String(value);
}

function useRuns(id: string) {
  return useQuery({
    queryKey: ["runs", id],
    queryFn: () => unwrap(api.GET("/api/datasources/{datasource_id}/runs", { params: { path: { datasource_id: id } } })),
  });
}

function DatasourceCredential({ ds, purpose, onClose, onSaved }: {
  ds: Datasource; purpose: CredentialPurpose; onClose: () => void; onSaved: () => void;
}) {
  const [username, setUsername] = useState(ds.credentials.find((c) => c.purpose === purpose)?.username ?? "");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useMessage();
  const save = useAction(() => unwrap(api.PUT("/api/datasources/{datasource_id}/credentials/{purpose}", {
    params: { path: { datasource_id: ds.id, purpose } }, body: { username: username.trim(), password },
  })), ["datasources"]);
  return <Drawer title={t("更换{{v1}}凭据", { v1: purpose === "collect" ? t("采集") : t("只读查询") })} onClose={onClose} footer={<>
    <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
    <button type="submit" form="datasource-credential-form" className="av-btn av-btn--primary" disabled={!username.trim() || !password || save.isPending}>{save.isPending ? t("保存中…") : t("保存")}</button>
  </>}>
    <p className="av-datasources__form-context">{ds.name} · <span className="av-mono">{ds.host}:{ds.port}</span></p>
    <form id="datasource-credential-form" className="av-datasources__form" aria-busy={save.isPending} onSubmit={(e) => {
      e.preventDefault();
      if (!username.trim() || !password || save.isPending) return;
      save.mutate(undefined, { onSuccess: onSaved, onError: (error) => setMessage({ ok: false, text: errorText(error) }) });
    }}>
      <label className="av-field"><span className="av-label">{t("账号")}</span><input className="av-input av-mono" required maxLength={128} autoComplete="off" autoFocus disabled={save.isPending} value={username} onChange={(e) => setUsername(e.target.value)} /></label>
      <label className="av-field"><span className="av-label">{t("新密码")}</span><input className="av-input" required maxLength={512} type="password" autoComplete="new-password" disabled={save.isPending} value={password} onChange={(e) => setPassword(e.target.value)} /></label>
      <Banner message={message} />
    </form>
  </Drawer>;
}

function AddDatasource({ onClose, onCreated }: { onClose: () => void; onCreated: (d: Datasource) => void }) {
  const engines = useEngines();
  const [form, setForm] = useState({ kind: "", name: "", host: "", port: undefined as string | undefined, collectUser: "", collectPassword: "", queryUser: "", queryPassword: "" });
  const [configuration, setConfiguration] = useState<Record<string, string>>({});
  const [message, setMessage] = useMessage();
  const engine = engines.data?.find((item) => item.kind === form.kind) ?? (!form.kind ? engines.data?.[0] : undefined);
  const options = engineOptions(engine);
  const port = form.port ?? String(engine?.default_port ?? "");
  const canQuery = engine?.capabilities.includes("query") ?? false;
  const collectOptional = credentialOptional(engine, (key) => configuration[key] || optionDefault(engine, key));
  const valid = engines.isSuccess && !!engine && options !== undefined && !!form.name.trim() && !!form.host.trim() && validPort(port)
    && options.every((option) => !option.required || optionValue(option, configuration[option.key] ?? "").length > 0)
    && (Boolean(form.collectUser.trim()) === Boolean(form.collectPassword))
    && (!canQuery || (Boolean(form.queryUser.trim()) === Boolean(form.queryPassword)));
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [key]: e.target.value });
  const create = useAction(() => unwrap(api.POST("/api/datasources", {
    body: {
      name: form.name.trim(), kind: engine!.kind, host: form.host.trim(), port: Number(port),
      options: Object.fromEntries((options ?? []).map((option) => [option.key, optionValue(option, configuration[option.key] ?? "")])),
      collect: form.collectUser.trim() ? { username: form.collectUser.trim(), password: form.collectPassword } : null,
      query: canQuery && form.queryUser.trim() ? { username: form.queryUser.trim(), password: form.queryPassword } : null,
    },
  })), ["datasources"]);

  return <Drawer title={t("添加数据源")} onClose={onClose} footer={<>
    <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
    <button type="submit" form="add-datasource-form" className="av-btn av-btn--primary" disabled={!valid || create.isPending}>{create.isPending ? t("添加中…") : t("添加")}</button>
  </>}>
    <QueryState query={engines} isEmpty={(list) => !list.length} emptyText={t("暂无可用引擎")} compact>
      {(engineList) => <form id="add-datasource-form" className="av-datasources__form" aria-busy={create.isPending} onSubmit={(e) => {
        e.preventDefault();
        if (!valid || create.isPending) return;
        create.mutate(undefined, { onSuccess: onCreated, onError: (error) => setMessage({ ok: false, text: errorText(error) }) });
      }}>
        <fieldset disabled={create.isPending} className="av-datasources__form-fields">
          <label className="av-field"><span className="av-label">{t("类型")}{engine?.note ? <Hint text={engine.note} /> : null}</span>
            <Select label={t("数据源类型")} value={engine?.kind ?? ""} disabled={create.isPending} onChange={(kind) => {
              setForm({ ...form, kind, port: String(engineList.find((item) => item.kind === kind)?.default_port ?? "") });
              setConfiguration({}); setMessage(null);
            }}>
              {engineList.map((item) => <option key={item.kind} value={item.kind}>{item.label}</option>)}
            </Select>
            {engine ? <Capabilities engine={engine} /> : null}
          </label>
          <label className="av-field"><span className="av-label">{t("名称")}</span><input className="av-input" required maxLength={128} value={form.name} onChange={set("name")} /></label>
          <div className="av-datasources__connection-form">
            <label className="av-field"><span className="av-label">{t("主机")}</span><input className="av-input av-mono" required maxLength={255} value={form.host} onChange={set("host")} /></label>
            <label className="av-field"><span className="av-label">{t("端口")}</span><input className="av-input av-mono" type="number" required min={1} max={65535} value={port} onChange={set("port")} /></label>
          </div>
          {options === undefined ? <div className="av-query-error" role="alert"><Banner message={{ ok: false, text: t("引擎配置项缺失，请重试") }} />
            <button type="button" className="av-btn av-btn--secondary" onClick={() => engines.refetch()}>{t("重试")}</button></div>
            : options.map((option) => <label className="av-field" key={option.key}>
              <span className="av-label">{option.label}{option.required ? " *" : ""}</span>
              {option.type === "choice"
                ? <Select label={option.label} value={configuration[option.key] || option.default || option.choices[0]} disabled={create.isPending}
                  onChange={(value) => setConfiguration({ ...configuration, [option.key]: value })}>
                  {option.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}
                </Select>
                : <input className="av-input av-mono" required={option.required} aria-describedby={`datasource-option-${option.key}-hint`} value={configuration[option.key] ?? ""}
                  onChange={(e) => setConfiguration({ ...configuration, [option.key]: e.target.value })} />}
              <span className="av-help" id={`datasource-option-${option.key}-hint`}>{[option.type === "list" ? t("逗号分隔") : "", option.hint].filter(Boolean).join(t("；"))}</span>
            </label>)}
          <fieldset className="av-datasources__credential-fields"><legend>{collectOptional ? t("采集凭据（可不填）") : t("采集凭据")}</legend><div className="av-datasources__field-pair">
            <label className="av-field"><span className="av-label">{t("采集账号")}</span><input className="av-input av-mono" maxLength={128} autoComplete="off" value={form.collectUser} onChange={set("collectUser")} /></label>
            <label className="av-field"><span className="av-label">{t("密码")}</span><input className="av-input" maxLength={512} type="password" autoComplete="new-password" value={form.collectPassword} onChange={set("collectPassword")} /></label>
          </div></fieldset>
          {canQuery ? <fieldset className="av-datasources__credential-fields"><legend>{t("只读查询凭据")}</legend><div className="av-datasources__field-pair">
            <label className="av-field"><span className="av-label">{t("查询账号")}</span><input className="av-input av-mono" maxLength={128} autoComplete="off" value={form.queryUser} onChange={set("queryUser")} /></label>
            <label className="av-field"><span className="av-label">{t("密码")}</span><input className="av-input" maxLength={512} type="password" autoComplete="new-password" value={form.queryPassword} onChange={set("queryPassword")} /></label>
          </div></fieldset> : null}
          {Boolean(form.collectUser.trim()) !== Boolean(form.collectPassword) || (canQuery && Boolean(form.queryUser.trim()) !== Boolean(form.queryPassword))
            ? <span className="av-help av-help--danger" role="status">{t("账号和密码需一起填写")}</span> : null}
        </fieldset>
        <Banner message={message} />
      </form>}
    </QueryState>
  </Drawer>;
}
