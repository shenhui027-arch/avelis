// 模型配置：提供方导航与连接、默认用途、数据外发分区。
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { api, ApiError, unwrap, type Schemas } from "../../api/client";
import { Hint, Icon } from "../../components/Icon";
import { Banner, Drawer, Empty, QueryState, Select, Switch } from "../../components/ui";
import { formatTime } from "../../format";
import { SettingsTabs, useMe } from "../../layout/Shell";
import { t } from "../../i18n";

type Provider = Schemas["ProviderOut"];
type Settings = Schemas["SettingsOut"];
type PresetsOut = Schemas["PresetsOut"];
type Message = { ok: boolean; text: string } | null;
type Effort = NonNullable<Schemas["Target"]["reasoning_effort"]>;

const PLAT: Record<string, string> = { openai_compatible: "OAI", anthropic: "ANT" };

function errorText(e: unknown) {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : t("操作失败");
}

function useBusinessTimeZone(): string {
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => unwrap(api.GET("/api/settings")) as Promise<{ business_timezone?: string }>,
  });
  return settings.data?.business_timezone ?? "Asia/Shanghai";
}

function providerStatus(p: Provider): { cls: string; text: string } {
  if (!p.enabled) return { cls: "", text: t("已停用") };
  if (!p.has_api_key) return { cls: "av-status--warn", text: t("缺少密钥") };
  if (p.last_check?.ok === true) return { cls: "av-status--ok", text: t("已连接") };
  if (p.last_check?.ok === false) return { cls: "av-status--danger", text: t("检测失败") };
  return { cls: "", text: t("未检测") };
}

function ModelFeedback({ message, busy }: { message: Message; busy?: boolean }) {
  return (
    <div className="av-models__feedback" role={message?.ok === false && !busy ? "alert" : "status"} aria-busy={busy}>
      {busy ? <span className="av-status">{t("正在保存…")}</span> : <Banner message={message} />}
    </div>
  );
}

export function ModelSettings() {
  const qc = useQueryClient();
  const me = useMe();
  const isAdmin = !!me.data?.roles.includes("admin");
  const providers = useQuery({ queryKey: ["model-providers"], queryFn: () => unwrap(api.GET("/api/model-providers")) });
  const settings = useQuery({ queryKey: ["model-settings"], queryFn: () => unwrap(api.GET("/api/model-settings")) });
  const presets = useQuery({ queryKey: ["model-presets"], queryFn: () => unwrap(api.GET("/api/model-providers/presets")) });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const list = providers.data ?? [];
  const selected = list.find((p) => p.id === selectedId) ?? list[0];

  const refresh = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["model-providers"] }),
      qc.invalidateQueries({ queryKey: ["model-settings"] }),
    ]);
  };
  const addAction = isAdmin ? (
    <button type="button" className="av-btn av-btn--secondary" onClick={() => setAdding(true)}>{t("添加提供方")}</button>
  ) : undefined;

  return (
    <div className="av-fill av-models">
      <div className="av-pagehd">
        <div className="av-pagehd__title">
          <h1>{t("设置")}</h1>
          <SettingsTabs current="models" />
          {settings.isSuccess ? (
            settings.data.mode === "configured"
              ? <span className="av-pill av-pill--ok">{t("已配置")}</span>
              : <span className="av-pill av-pill--warn" title={settings.data.note ?? ""}>{t("演示模型")}</span>
          ) : null}
        </div>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" disabled={!isAdmin} onClick={() => setAdding(true)}>
            <Icon name="plus" />{t("添加提供方")}
          </button>
        </div>
      </div>

      <div className="av-workspace av-models__workspace">
        <section className="av-pane av-models__providers" aria-label={t("提供方")}>
          <div className="av-pane-hd"><h2>{t("提供方")} {providers.isSuccess ? <span className="av-count">{list.length}</span> : null}</h2></div>
          <div className="av-scroll">
            <QueryState query={providers} isEmpty={(items) => items.length === 0} emptyText={t("还没有模型提供方")} emptyAction={addAction} compact>
              {(items) => items.map((p) => {
                const status = providerStatus(p);
                return (
                  <button key={p.id} type="button" className="av-provider av-models__provider" aria-current={p.id === selected?.id}
                    onClick={() => setSelectedId(p.id)}>
                    <span className="av-row__main">
                      <span className="av-row__title">{p.name}</span>
                      <span className="av-row__meta av-mono">{t("{{count}} 个模型 ·", { count: p.models.filter((m) => m.enabled).length })} {p.code}</span>
                    </span>
                    <span className="av-models__provider-status">
                      <span className="av-plat">{PLAT[p.kind]}</span>
                      <span className={`av-status ${status.cls}`}>{status.text}</span>
                    </span>
                  </button>
                );
              })}
            </QueryState>
          </div>
        </section>

        <section className="av-pane av-models__main" aria-label={t("模型配置")}>
          <div className="av-scroll av-models__sections">
            <section className="av-panel" aria-label={t("连接与模型")}>
              <QueryState query={providers} isEmpty={(items) => items.length === 0} emptyText={t("添加提供方，开始配置连接与模型")} emptyAction={addAction} rows={5} compact>
                {() => selected ? <ProviderDetail key={selected.id} provider={selected} isAdmin={isAdmin} onChanged={refresh} /> : null}
              </QueryState>
            </section>
            <section className="av-panel" aria-label={t("默认用途与数据外发")}>
              <QueryState query={settings} rows={4}>
                {(data) => (
                  <QueryState query={providers} rows={4}>
                    {(items) => <DefaultModels settings={data} providers={items} isAdmin={isAdmin} onSaved={refresh} />}
                  </QueryState>
                )}
              </QueryState>
            </section>
          </div>
        </section>
      </div>

      {adding ? (
        <AddProvider query={presets} onClose={() => setAdding(false)}
          onCreated={async (p) => { setSelectedId(p.id); setAdding(false); await refresh(); }} />
      ) : null}
    </div>
  );
}

function ProviderDetail({ provider, isAdmin, onChanged }: {
  provider: Provider; isAdmin: boolean; onChanged: () => Promise<void>;
}) {
  const timeZone = useBusinessTimeZone();
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  const [newKey, setNewKey] = useState("");
  const [editingKey, setEditingKey] = useState(!provider.has_api_key);
  const [modelInput, setModelInput] = useState("");
  const [fetched, setFetched] = useState<string[]>([]);
  const [message, setMessage] = useState<Message>(null);
  const [checking, setChecking] = useState<string | null>(null);

  const patch = useMutation({
    mutationFn: (body: Schemas["ProviderUpdate"]) =>
      unwrap(api.PATCH("/api/model-providers/{provider_id}", { params: { path: { provider_id: provider.id } }, body })),
    onSuccess: async (_, body) => {
      setMessage({ ok: true, text: t("已保存") });
      if (body.api_key !== undefined) { setNewKey(""); setEditingKey(false); }
      await onChanged();
    },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/model-providers/{provider_id}", { params: { path: { provider_id: provider.id } } })),
    onSuccess: onChanged,
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const fetchModels = useMutation({
    mutationFn: () => unwrap(api.POST("/api/model-providers/{provider_id}/fetch-models", { params: { path: { provider_id: provider.id } } })),
    onSuccess: (r) => { setFetched(r.models); setMessage({ ok: true, text: t("获取到 {{count}} 个模型", { count: r.models.length }) }); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const busy = patch.isPending || remove.isPending || fetchModels.isPending || checking !== null;
  const disabled = !isAdmin || busy;

  async function check(modelId: string) {
    if (disabled) return;
    setChecking(modelId);
    setMessage(null);
    try {
      const r = await unwrap(api.POST("/api/model-providers/{provider_id}/check", {
        params: { path: { provider_id: provider.id } }, body: { model_id: modelId },
      }));
      setMessage(r.ok
        ? { ok: true, text: t("{{modelId}} 连接正常 · {{latency_ms}} ms · {{v3}}", { modelId, latency_ms: r.latency_ms, v3: r.tool_calling ? t("支持工具调用") : t("未发生工具调用") }) }
        : { ok: false, text: r.error ?? t("检测失败") });
      await onChanged();
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setChecking(null);
    }
  }

  const models = provider.models;
  const setModels = (next: Schemas["ModelEntry"][]) => { if (!disabled) patch.mutate({ models: next }); };
  const addable = fetched.filter((id) => !models.some((m) => m.id === id));
  const last = provider.last_check;
  const connectionDirty = baseUrl.trim() !== provider.base_url || !!newKey.trim();

  return (
    <>
      <div className="av-pane-hd av-models__object-header">
        <h2>{provider.name}<span className="av-plat">{PLAT[provider.kind]}</span></h2>
        <div className="av-actions">
          <span className="av-caption">{t("启用")}</span>
          <Switch label={t("启用提供方 {{name}}", { name: provider.name })} checked={provider.enabled} disabled={disabled}
            onChange={(v) => { if (!disabled) patch.mutate({ enabled: v }); }} />
          <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label={t("删除提供方 {{name}}", { name: provider.name })}
            disabled={disabled} onClick={() => { if (!disabled && confirm(t("删除提供方 {{name}}？", { name: provider.name }))) remove.mutate(); }}>
            <Icon name="trash" />
          </button>
        </div>
      </div>
      <div className="av-stack">
        <ModelFeedback message={message} busy={patch.isPending || remove.isPending} />
        <form className="av-models__connection" aria-label={t("连接配置")} aria-busy={patch.isPending}
          onSubmit={(e) => {
            e.preventDefault();
            if (disabled || !connectionDirty || !baseUrl.trim()) return;
            patch.mutate({ base_url: baseUrl.trim(), ...(newKey.trim() ? { api_key: newKey.trim() } : {}) });
          }}>
          <h3 className="av-models__section-title">{t("连接")}</h3>
          <label className="av-field">
            <span className="av-label">{t("接口地址")} <Hint text={t("填写根地址，下方显示实际请求端点")} /></span>
            <input className="av-input av-mono" value={baseUrl} required disabled={disabled} onChange={(e) => setBaseUrl(e.target.value)} />
          </label>
          <span className="av-caption av-mono av-models__endpoint">{provider.endpoint}</span>
          <div className="av-formrow">
            <label className="av-field">
              <span className="av-label">{t("API 密钥")} <Hint text={t("加密存储，只写不读；界面仅显示末四位")} /></span>
              {editingKey ? (
                <input className="av-input av-mono" type="password" autoComplete="new-password" placeholder={t("输入新密钥")} value={newKey}
                  disabled={disabled} onChange={(e) => setNewKey(e.target.value)} />
              ) : <input className="av-input av-mono" value={provider.api_key ?? t("未设置")} disabled readOnly />}
            </label>
            {!editingKey ? (
              <button type="button" className="av-btn av-btn--secondary" disabled={disabled}
                onClick={() => setEditingKey(true)}><Icon name="key" />{t("更换")}</button>
            ) : provider.has_api_key ? (
              <button type="button" className="av-btn av-btn--secondary" disabled={disabled}
                onClick={() => { setEditingKey(false); setNewKey(""); }}>{t("取消更换")}</button>
            ) : null}
          </div>
          <div className="av-actions">
            <button type="submit" className="av-btn av-btn--primary" disabled={disabled || !connectionDirty || !baseUrl.trim()}>
              {patch.isPending ? t("保存中…") : t("保存连接")}
            </button>
          </div>
        </form>

        <section className="av-models__models" aria-label={t("模型列表")}>
          <div className="av-models__section-header">
            <h3 className="av-models__section-title">{t("模型")} <span className="av-count">{models.length}</span><Hint text={t("启用与移除即时保存；问数理解模型需要通过工具调用检测")} /></h3>
            <div className="av-actions">
              <span className="av-caption">{t("即时保存")}</span>
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={disabled || !provider.has_api_key}
                aria-busy={fetchModels.isPending} onClick={() => { if (!disabled) fetchModels.mutate(); }}>
                <Icon name="refresh" small />{fetchModels.isPending ? t("获取中…") : t("获取模型列表")}
              </button>
            </div>
          </div>
          {models.length === 0 && addable.length === 0 ? <Empty text={t("还没有模型，请获取列表或手动添加")} compact /> : (
            <div className="av-table-wrap">
              <table className="av-table av-models__table">
                <thead><tr><th>{t("模型")}</th><th>{t("工具调用")}</th><th>{t("上下文窗口")} <Hint text={t("模型一次能接收的最多 token 数，问数页据此显示上下文用量；提供方接口不提供，需按文档填写，不填只显示用量")} /></th><th>{t("启用")}</th><th className="end">{t("操作")}</th></tr></thead>
                <tbody>
                  {models.map((m) => (
                    <tr key={m.id} aria-busy={checking === m.id}>
                      <td className="av-mono">{m.id}</td>
                      <td>{m.tool_calling === true ? <span className="av-status av-status--ok">{t("支持")}</span>
                        : m.tool_calling === false ? <span className="av-status av-status--danger">{t("不支持")}</span>
                          : <span className="av-status">{t("未检测")}</span>}</td>
                      <td><input className="av-input av-mono av-models__ctx" type="number" min={1024} max={10000000} step={1024}
                        aria-label={t("{{id}} 的上下文窗口（token）", { id: m.id })} placeholder={t("未填")} disabled={disabled}
                        defaultValue={m.context_window ?? ""} key={`${m.id}-${m.context_window ?? ""}`}
                        onBlur={(e) => {
                          const v = e.currentTarget.value.trim() ? Number(e.currentTarget.value) : null;
                          if (v !== (m.context_window ?? null)) setModels(models.map((x) => x.id === m.id ? { ...x, context_window: v } : x));
                        }} /></td>
                      <td><Switch label={t("启用模型 {{id}}", { id: m.id })} checked={m.enabled} disabled={disabled}
                        onChange={(v) => setModels(models.map((x) => x.id === m.id ? { ...x, enabled: v } : x))} /></td>
                      <td className="end"><div className="av-actions">
                        <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={disabled || !provider.has_api_key}
                          onClick={() => check(m.id)}>{checking === m.id ? t("检测中…") : t("检测")}</button>
                        <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label={t("移除模型 {{id}}", { id: m.id })} disabled={disabled}
                          onClick={() => setModels(models.filter((x) => x.id !== m.id))}><Icon name="trash" small /></button>
                      </div></td>
                    </tr>
                  ))}
                  {addable.map((id) => (
                    <tr key={`f-${id}`}>
                      <td className="av-mono av-muted">{id}</td><td><span className="av-caption">{t("可添加")}</span></td><td /><td />
                      <td className="end"><button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={disabled}
                        onClick={() => setModels([...models, { id, enabled: true, tool_calling: null }])}>{t("添加")}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <form className="av-formrow" aria-label={t("手动添加模型")} onSubmit={(e) => {
            e.preventDefault();
            if (disabled || !modelInput.trim() || models.some((m) => m.id === modelInput.trim())) return;
            patch.mutate({ models: [...models, { id: modelInput.trim(), enabled: true, tool_calling: null }] }, { onSuccess: () => setModelInput("") });
          }}>
            <label className="av-field">
              <span className="av-label">{t("模型 ID")}</span>
              <input className="av-input av-mono" value={modelInput} disabled={disabled} placeholder={t("例如 deepseek-flash")}
                onChange={(e) => setModelInput(e.target.value)} />
            </label>
            <button type="submit" className="av-btn av-btn--secondary" disabled={disabled || !modelInput.trim() || models.some((m) => m.id === modelInput.trim())}>
              <Icon name="plus" />{t("添加")}
            </button>
          </form>
        </section>
        {last?.checked_at ? (
          <dl className="av-kv">
            <dt>{t("最近检测")}</dt><dd><span className={`av-status ${last.ok ? "av-status--ok" : "av-status--danger"}`}>{last.ok ? t("正常") : t("失败")}</span>
              {" · "}<span className="av-mono">{last.model_id}</span>{" · "}<span className="av-mono">{last.latency_ms} ms</span></dd>
            <dt>{t("时间")}</dt><dd className="av-mono">{formatTime(last.checked_at, timeZone)}</dd>
            {last.error ? <><dt>{t("原因")}</dt><dd className="av-mono">{last.error}</dd></> : null}
          </dl>
        ) : null}
      </div>
    </>
  );
}

function DefaultModels({ settings, providers, isAdmin, onSaved }: {
  settings: Settings; providers: Provider[]; isAdmin: boolean; onSaved: () => Promise<void>;
}) {
  const purposes = Object.entries(settings.assignments);
  const initial = useMemo(() => Object.fromEntries(Object.entries(settings.assignments).map(([k, v]) =>
    [k, v.provider_id && v.model_id ? `${v.provider_id}|${v.model_id}` : ""])), [settings.assignments]);
  const initialEffort = useMemo(() => Object.fromEntries(Object.entries(settings.assignments).map(([k, v]) =>
    [k, v.reasoning_effort])), [settings.assignments]);
  const [choice, setChoice] = useState<Record<string, string>>(initial);
  const [effort, setEffort] = useState<Record<string, string>>(initialEffort);
  const [policy, setPolicy] = useState(settings.policy);
  const [message, setMessage] = useState<Message>(null);
  useEffect(() => setChoice(initial), [initial]);
  useEffect(() => setEffort(initialEffort), [initialEffort]);
  useEffect(() => setPolicy(settings.policy), [settings.policy]);
  const options = providers.filter((p) => p.enabled && p.has_api_key)
    .flatMap((p) => p.models.filter((m) => m.enabled).map((m) => ({ value: `${p.id}|${m.id}`, label: `${p.name} · ${m.id}`,
      tools: m.tool_calling, efforts: m.efforts ?? [] })));
  // 思考强度（ADR 0012）：只列所选模型支持的档位；所选档位不在其中时按该用途的默认档位
  const effortOf = (key: string) => {
    const efforts = options.find((o) => o.value === choice[key])?.efforts ?? [];
    const wanted = effort[key] ?? settings.assignments[key]?.default_effort;
    return efforts.find((e) => e.value === wanted) ?? efforts.find((e) => e.value === settings.assignments[key]?.default_effort);
  };
  const save = useMutation({
    mutationFn: () => unwrap(api.PUT("/api/model-settings", {
      body: {
        assignments: Object.fromEntries(Object.entries(choice).map(([k, v]) => {
          const [provider_id, model_id] = v.split("|");
          return [k, v ? { provider_id, model_id, reasoning_effort: (effortOf(k)?.value ?? null) as Effort | null } : null];
        })),
        policy,
      },
    })),
    onSuccess: async () => { setMessage({ ok: true, text: t("已保存") }); await onSaved(); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const dirty = JSON.stringify(choice) !== JSON.stringify(initial) || JSON.stringify(policy) !== JSON.stringify(settings.policy)
    || JSON.stringify(effort) !== JSON.stringify(initialEffort);
  const disabled = !isAdmin || save.isPending;

  return (
    <form aria-label={t("默认用途与数据外发")} aria-busy={save.isPending} onSubmit={(e) => { e.preventDefault(); if (!disabled && dirty) save.mutate(); }}>
      <div className="av-pane-hd"><h2>{t("默认用途")}</h2><span className="av-caption">{t("与数据外发统一保存")}</span></div>
      <div className="av-stack">
        <div className="av-models__assignments">
          {purposes.map(([key, p]) => {
            const picked = options.find((o) => o.value === choice[key]);
            return (
              <label key={key} className="av-field">
                <span className="av-label">{p.label} <Hint text={p.hint} /></span>
                <Select value={choice[key] ?? ""} disabled={disabled} onChange={(value) => setChoice({ ...choice, [key]: value })}>
                  <option value="">{key === "explain" ? t("同问数理解模型") : t("演示模型（规则）")}</option>
                  {choice[key] && !picked ? <option value={choice[key]} disabled>{t("已配置模型当前不可用")}</option> : null}
                  {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </Select>
                {choice[key] && !picked ? <span className="av-status av-status--warn">{t("请选择可用模型")}</span> : null}
                {p.needs_tools && picked && picked.tools !== true ? <span className="av-status av-status--warn">{t("工具调用未检测通过")}</span> : null}
                {picked && picked.efforts.length ? <span className="av-models__effort">
                  <Select value={effortOf(key)?.value ?? ""} disabled={disabled} label={t("{{label}}的思考强度", { label: p.label })}
                    onChange={(value) => setEffort({ ...effort, [key]: value })}>
                    {picked.efforts.map((e) => <option key={e.value} value={e.value}>
                      {t("思考强度：")}{e.label}{e.effective !== e.value ? t("（实际按“{{effective_label}}”执行）", { effective_label: e.effective_label }) : ""}</option>)}
                  </Select>
                </span> : null}
              </label>
            );
          })}
        </div>
        <section className="av-models__policy" aria-labelledby="model-policy-title">
          <h3 id="model-policy-title" className="av-models__section-title">{t("数据外发")}</h3>
          <div className="av-toggle-row">
            <span className="av-toggle-row__text"><span>{t("发送查询结果")} <Hint text={t("问数结果发给结果解释模型与分析模型；关闭时使用模板解释，分析模式不可用")} /></span></span>
            <Switch label={t("发送查询结果")} checked={policy.send_results} disabled={disabled} onChange={(v) => setPolicy({ ...policy, send_results: v })} />
          </div>
          <div className="av-toggle-row">
            <span className="av-toggle-row__text"><span>{t("发送代码片段")} <Hint text={t("未能解析的代码脱敏后发给导入血缘推断模型；脱敏是尽力而为，敏感环境请使用本地模型；关闭时使用演示规则")} /></span></span>
            <Switch label={t("发送代码片段")} checked={policy.send_code} disabled={disabled} onChange={(v) => setPolicy({ ...policy, send_code: v })} />
          </div>
          <p className="av-caption av-models__policy-note"><Icon name="shield" small />{t("默认发送查询结果与代码片段，分析模式需要它们；需保密请接入本地模型（OpenAI 兼容接口）")}</p>
        </section>
        <ModelFeedback message={message} busy={save.isPending} />
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--secondary" disabled={disabled || !dirty}
            onClick={() => { setChoice(initial); setEffort(initialEffort); setPolicy(settings.policy); setMessage(null); }}>{t("还原")}</button>
          <button type="submit" className="av-btn av-btn--primary" disabled={disabled || !dirty}>{save.isPending ? t("保存中…") : t("保存配置")}</button>
        </div>
      </div>
    </form>
  );
}

function AddProvider({ query, onClose, onCreated }: {
  query: UseQueryResult<PresetsOut>; onClose: () => void; onCreated: (p: Provider) => Promise<void>;
}) {
  const [preset, setPreset] = useState("");
  const [code, setCode] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [message, setMessage] = useState<Message>(null);
  useEffect(() => {
    const first = query.data && Object.keys(query.data.presets)[0];
    if (first && !preset) {
      setPreset(first); setCode(first === "custom" ? "" : first); setBaseUrl(query.data!.presets[first].base_url);
    }
  }, [query.data, preset]);
  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/model-providers", {
      body: { code: code.trim(), preset: preset as Schemas["ProviderCreate"]["preset"], base_url: baseUrl.trim() || null, api_key: apiKey.trim() || null, models: [] },
    })),
    onSuccess: onCreated,
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const validCode = /^[a-z0-9_-]+$/.test(code.trim());
  const ready = query.isSuccess && !!query.data.presets[preset] && validCode && !!baseUrl.trim();

  return (
    <Drawer title={t("添加提供方")} onClose={onClose} footer={<>
      <button type="button" className="av-btn av-btn--secondary" disabled={create.isPending} onClick={onClose}>{t("取消")}</button>
      <button type="submit" form="model-provider-create" className="av-btn av-btn--primary" disabled={!ready || create.isPending}>
        {create.isPending ? t("添加中…") : t("添加")}
      </button>
    </>}>
      <QueryState query={query} isEmpty={(data) => Object.keys(data.presets).length === 0} emptyText={t("暂无可用提供方预设")}
        emptyAction={<button type="button" className="av-btn av-btn--secondary" onClick={() => query.refetch()}>{t("重新加载")}</button>} rows={5} compact>
        {(presets) => (
          <form id="model-provider-create" className="av-models__add-form" aria-busy={create.isPending} onSubmit={(e) => {
            e.preventDefault(); if (ready && !create.isPending) create.mutate();
          }}>
            <fieldset className="av-models__presets" disabled={create.isPending}>
              <legend className="av-label">{t("提供方类型")}</legend>
              {Object.keys(presets.presets).map((k) => (
                <label key={k} className="av-option">
                  <input type="radio" name="preset" checked={preset === k} onChange={() => {
                    setPreset(k); setCode(k === "custom" ? "" : k); setBaseUrl(presets.presets[k].base_url); setMessage(null);
                  }} />
                  <span><span className="av-option__title">{presets.presets[k].name}</span><br />
                    <span className="av-option__meta av-mono">{presets.presets[k].base_url || t("自填接口地址")}</span></span>
                </label>
              ))}
            </fieldset>
            <label className="av-field">
              <span className="av-label">{t("编码")} <Hint text={t("小写字母、数字、- 或 _，用于区分同类提供方")} /></span>
              <input className="av-input av-mono" value={code} required pattern="[a-z0-9_-]+" disabled={create.isPending}
                aria-invalid={!!code && !validCode} aria-describedby={code && !validCode ? "model-code-error" : undefined}
                onChange={(e) => setCode(e.target.value)} />
              {code && !validCode ? <span id="model-code-error" className="av-status av-status--danger">{t("仅支持小写字母、数字、- 和 _")}</span> : null}
            </label>
            <label className="av-field"><span className="av-label">{t("接口地址")}</span>
              <input className="av-input av-mono" value={baseUrl} required disabled={create.isPending} onChange={(e) => setBaseUrl(e.target.value)} /></label>
            <label className="av-field"><span className="av-label">{t("API 密钥")}</span>
              <input className="av-input av-mono" type="password" autoComplete="new-password" value={apiKey} disabled={create.isPending} onChange={(e) => setApiKey(e.target.value)} /></label>
            <ModelFeedback message={message} />
          </form>
        )}
      </QueryState>
    </Drawer>
  );
}
