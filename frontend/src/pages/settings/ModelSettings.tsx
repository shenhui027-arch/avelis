// 模型配置：工作台模式三栏 —— 提供方列表 | 连接与模型 | 默认模型与数据外发。
// 交互参考 LobeChat / Cherry Studio（提供方列表、检测、获取模型列表）、Open WebUI（模型白名单）、
// Dify（按用途的默认模型）。密钥只写不读，界面只显示末四位掩码。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { api, ApiError, unwrap, type Schemas } from "../../api/client";
import { Hint, Icon } from "../../components/Icon";
import { formatTime } from "../../format";
import { SettingsTabs, useMe } from "../../layout/Shell";

type Provider = Schemas["ProviderOut"];
type Settings = Schemas["SettingsOut"];
type PresetsOut = Schemas["PresetsOut"];

const PLAT: Record<string, string> = { openai_compatible: "OAI", anthropic: "ANT" };

function errorText(e: unknown) {
  return e instanceof ApiError ? e.message : e instanceof Error ? e.message : "操作失败";
}

function useBusinessTimeZone(): string {
  const settings = useQuery({
    queryKey: ["settings"],
    queryFn: () => unwrap(api.GET("/api/settings")) as Promise<{ business_timezone?: string }>,
  });
  return settings.data?.business_timezone ?? "Asia/Shanghai";
}

function providerStatus(p: Provider): { cls: string; text: string } {
  if (!p.enabled) return { cls: "", text: "已停用" };
  if (!p.has_api_key) return { cls: "av-status--warn", text: "缺少密钥" };
  if (p.last_check?.ok === true) return { cls: "av-status--ok", text: "已连接" };
  if (p.last_check?.ok === false) return { cls: "av-status--danger", text: "检测失败" };
  return { cls: "", text: "未检测" };
}

function Switch({ checked, onChange, disabled, label }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} className="av-switch"
      disabled={disabled} onClick={() => onChange(!checked)} />
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

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["model-providers"] });
    qc.invalidateQueries({ queryKey: ["model-settings"] });
  };

  return (
    <div className="av-fill">
      <div className="av-pagehd">
        <div className="av-pagehd__title">
          <h1>设置</h1>
          <SettingsTabs current="models" />
          {settings.data ? (
            settings.data.mode === "configured"
              ? <span className="av-pill av-pill--ok">已配置</span>
              : <span className="av-pill av-pill--warn" title={settings.data.note ?? ""}>演示模型</span>
          ) : null}
        </div>
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--primary" disabled={!isAdmin} onClick={() => setAdding(true)}>
            <Icon name="plus" />添加提供方
          </button>
        </div>
      </div>

      <div className="av-workspace ms-workspace">
        <section className="av-pane" aria-label="提供方">
          <div className="av-pane-hd"><h2>提供方 <span className="av-count">{list.length}</span></h2></div>
          <div className="av-scroll">
            {providers.isError ? <div className="av-stack"><div className="av-banner av-banner--danger">{errorText(providers.error)}</div></div> : null}
            {list.map((p) => {
              const status = providerStatus(p);
              return (
                <button key={p.id} type="button" className="av-provider" aria-current={p.id === selected?.id}
                  onClick={() => setSelectedId(p.id)}>
                  <span className="av-row__main">
                    <span className="av-row__title">{p.name}</span>
                    <span className="av-row__meta av-mono">{p.models.filter((m) => m.enabled).length} 个模型 · {p.code}</span>
                  </span>
                  <span>
                    <span className="av-plat">{PLAT[p.kind]}</span>{" "}
                    <span className={`av-status ${status.cls}`}>{status.text}</span>
                  </span>
                </button>
              );
            })}
            {providers.isSuccess && list.length === 0 ? (
              <div className="av-empty" style={{ padding: "var(--space-6)" }}>
                <span>还没有模型提供方</span>
                <button type="button" className="av-btn av-btn--secondary" disabled={!isAdmin} onClick={() => setAdding(true)}>添加</button>
              </div>
            ) : null}
          </div>
        </section>

        <section className="av-pane" aria-label="连接与模型">
          {selected ? <ProviderDetail key={selected.id} provider={selected} isAdmin={isAdmin} onChanged={refresh} /> : (
            <div className="av-empty">选择或添加一个提供方</div>
          )}
        </section>

        <section className="av-pane av-pane--rail" aria-label="默认模型">
          {settings.data ? (
            <DefaultModels key={JSON.stringify(settings.data)} settings={settings.data} providers={list}
              isAdmin={isAdmin} onSaved={refresh} />
          ) : null}
        </section>
      </div>

      {adding && presets.data ? (
        <AddProvider presets={presets.data} onClose={() => setAdding(false)}
          onCreated={(p) => { setAdding(false); setSelectedId(p.id); refresh(); }} />
      ) : null}
    </div>
  );
}

// --- 中栏：连接与模型 ---------------------------------------------------------------

function ProviderDetail({ provider, isAdmin, onChanged }: { provider: Provider; isAdmin: boolean; onChanged: () => void }) {
  const timeZone = useBusinessTimeZone();
  const [baseUrl, setBaseUrl] = useState(provider.base_url);
  const [newKey, setNewKey] = useState("");
  const [editingKey, setEditingKey] = useState(!provider.has_api_key);
  const [modelInput, setModelInput] = useState("");
  const [fetched, setFetched] = useState<string[]>([]);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const [checking, setChecking] = useState<string | null>(null);

  const patch = useMutation({
    mutationFn: (body: Schemas["ProviderUpdate"]) =>
      unwrap(api.PATCH("/api/model-providers/{provider_id}", { params: { path: { provider_id: provider.id } }, body })),
    onSuccess: () => { setMessage({ ok: true, text: "已保存" }); setNewKey(""); setEditingKey(false); onChanged(); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/model-providers/{provider_id}", { params: { path: { provider_id: provider.id } } })),
    onSuccess: onChanged,
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });
  const fetchModels = useMutation({
    mutationFn: () => unwrap(api.POST("/api/model-providers/{provider_id}/fetch-models", { params: { path: { provider_id: provider.id } } })),
    onSuccess: (r) => { setFetched(r.models); setMessage({ ok: true, text: `获取到 ${r.models.length} 个模型` }); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });

  async function check(modelId: string) {
    setChecking(modelId);
    setMessage(null);
    try {
      const r = await unwrap(api.POST("/api/model-providers/{provider_id}/check", {
        params: { path: { provider_id: provider.id } }, body: { model_id: modelId } }));
      setMessage(r.ok
        ? { ok: true, text: `${modelId} 连接正常 · ${r.latency_ms} ms · ${r.tool_calling ? "支持工具调用" : "未发生工具调用"}` }
        : { ok: false, text: r.error ?? "检测失败" });
      onChanged();
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setChecking(null);
    }
  }

  const models = provider.models;
  const setModels = (next: Schemas["ModelEntry"][]) => patch.mutate({ models: next });
  const addable = fetched.filter((id) => !models.some((m) => m.id === id));
  const last = provider.last_check;

  return (
    <>
      <div className="av-pane-hd">
        <h2>{provider.name}<span className="av-plat">{PLAT[provider.kind]}</span></h2>
        <div className="av-actions">
          <Switch label="启用" checked={provider.enabled} disabled={!isAdmin || patch.isPending}
            onChange={(v) => patch.mutate({ enabled: v })} />
          <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" title="删除提供方"
            disabled={!isAdmin} onClick={() => { if (confirm(`删除提供方 ${provider.name}？`)) remove.mutate(); }}>
            <Icon name="trash" />
          </button>
        </div>
      </div>
      <div className="av-scroll">
        <div className="av-stack">
          {message ? <div className={`av-banner ${message.ok ? "av-banner--ok" : "av-banner--danger"}`}>{message.text}</div> : null}

          <div className="av-formrow">
            <label className="av-field">
              <span className="av-label">接口地址 <Hint text="填根地址即可；下方显示实际请求的端点" /></span>
              <input className="av-input av-mono" value={baseUrl} disabled={!isAdmin} onChange={(e) => setBaseUrl(e.target.value)} />
            </label>
            <button type="button" className="av-btn av-btn--secondary" disabled={!isAdmin || baseUrl === provider.base_url}
              onClick={() => patch.mutate({ base_url: baseUrl })}>保存</button>
          </div>
          <span className="av-caption av-mono">{provider.endpoint}</span>

          <div className="av-formrow">
            <label className="av-field">
              <span className="av-label">API 密钥 <Hint text="加密存储，只写不读；界面只显示末四位" /></span>
              {editingKey ? (
                <input className="av-input av-mono" type="password" autoComplete="off" placeholder="sk-…" value={newKey}
                  disabled={!isAdmin} onChange={(e) => setNewKey(e.target.value)} />
              ) : (
                <input className="av-input av-mono" value={provider.api_key ?? "未设置"} disabled readOnly />
              )}
            </label>
            {editingKey ? (
              <button type="button" className="av-btn av-btn--secondary" disabled={!isAdmin || !newKey}
                onClick={() => patch.mutate({ api_key: newKey })}>保存</button>
            ) : (
              <button type="button" className="av-btn av-btn--secondary" disabled={!isAdmin}
                onClick={() => setEditingKey(true)}><Icon name="key" />更换</button>
            )}
          </div>

          <div className="av-pane-hd" style={{ margin: "0 calc(-1 * var(--space-4))", borderTop: "1px solid var(--line-subtle)" }}>
            <h2>模型 <span className="av-count">{models.length}</span> <Hint text="只有启用的模型能被指定用途；问数理解需要通过工具调用检测" /></h2>
            <div className="av-actions">
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!isAdmin || !provider.has_api_key || fetchModels.isPending}
                onClick={() => fetchModels.mutate()}><Icon name="refresh" small />获取模型列表</button>
            </div>
          </div>

          <div className="av-table-wrap">
            <table className="av-table">
              <thead>
                <tr><th>模型</th><th>工具调用</th><th>启用</th><th className="end" /></tr>
              </thead>
              <tbody>
                {models.map((m) => (
                  <tr key={m.id}>
                    <td className="av-mono">{m.id}</td>
                    <td>
                      {m.tool_calling === true ? <span className="av-status av-status--ok">支持</span>
                        : m.tool_calling === false ? <span className="av-status av-status--danger">不支持</span>
                          : <span className="av-status">未检测</span>}
                    </td>
                    <td>
                      <Switch label={`启用 ${m.id}`} checked={m.enabled} disabled={!isAdmin}
                        onChange={(v) => setModels(models.map((x) => (x.id === m.id ? { ...x, enabled: v } : x)))} />
                    </td>
                    <td className="end">
                      <div className="av-actions">
                        <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!isAdmin || checking !== null || !provider.has_api_key}
                          onClick={() => check(m.id)}>{checking === m.id ? "检测中" : "检测"}</button>
                        <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" title="移除" disabled={!isAdmin}
                          onClick={() => setModels(models.filter((x) => x.id !== m.id))}><Icon name="trash" small /></button>
                      </div>
                    </td>
                  </tr>
                ))}
                {addable.map((id) => (
                  <tr key={`f-${id}`}>
                    <td className="av-mono av-muted">{id}</td>
                    <td><span className="av-caption">可添加</span></td>
                    <td />
                    <td className="end">
                      <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!isAdmin}
                        onClick={() => setModels([...models, { id, enabled: true, tool_calling: null }])}>添加</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="av-formrow">
            <label className="av-field">
              <span className="av-label">手动添加模型 ID</span>
              <input className="av-input av-mono" value={modelInput} disabled={!isAdmin} placeholder="例如 deepseek-flash"
                onChange={(e) => setModelInput(e.target.value)} />
            </label>
            <button type="button" className="av-btn av-btn--secondary" disabled={!isAdmin || !modelInput.trim() || models.some((m) => m.id === modelInput.trim())}
              onClick={() => { setModels([...models, { id: modelInput.trim(), enabled: true, tool_calling: null }]); setModelInput(""); }}>
              <Icon name="plus" />添加
            </button>
          </div>

          {last?.checked_at ? (
            <dl className="av-kv">
              <dt>最近检测</dt>
              <dd>
                <span className={`av-status ${last.ok ? "av-status--ok" : "av-status--danger"}`}>{last.ok ? "正常" : "失败"}</span>
                {" · "}<span className="av-mono">{last.model_id}</span>{" · "}<span className="av-mono">{last.latency_ms} ms</span>
              </dd>
              <dt>时间</dt>
              <dd className="av-mono">{formatTime(last.checked_at, timeZone)}</dd>
              {last.error ? (<><dt>原因</dt><dd className="av-mono">{last.error}</dd></>) : null}
            </dl>
          ) : null}
        </div>
      </div>
    </>
  );
}

// --- 右栏：默认模型与数据外发 ---------------------------------------------------------

function DefaultModels({ settings, providers, isAdmin, onSaved }: {
  settings: Settings; providers: Provider[]; isAdmin: boolean; onSaved: () => void;
}) {
  const purposes = Object.entries(settings.assignments);
  const initial = useMemo(() => Object.fromEntries(Object.entries(settings.assignments).map(([k, v]) =>
    [k, v.provider_id && v.model_id ? `${v.provider_id}|${v.model_id}` : ""])), [settings]);
  const [choice, setChoice] = useState<Record<string, string>>(initial);
  const [policy, setPolicy] = useState(settings.policy);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => setChoice(initial), [initial]);

  const options = providers.filter((p) => p.enabled && p.has_api_key)
    .flatMap((p) => p.models.filter((m) => m.enabled).map((m) => ({ value: `${p.id}|${m.id}`, label: `${p.name} · ${m.id}`, tools: m.tool_calling })));

  const save = useMutation({
    mutationFn: () => unwrap(api.PUT("/api/model-settings", {
      body: {
        assignments: Object.fromEntries(Object.entries(choice).map(([k, v]) => {
          const [provider_id, model_id] = v.split("|");
          return [k, v ? { provider_id, model_id } : null];
        })),
        policy,
      },
    })),
    onSuccess: () => { setMessage({ ok: true, text: "已保存" }); onSaved(); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });

  const dirty = JSON.stringify(choice) !== JSON.stringify(initial) || JSON.stringify(policy) !== JSON.stringify(settings.policy);

  return (
    <>
      <div className="av-pane-hd"><h2>默认模型</h2></div>
      <div className="av-scroll">
        <div className="av-stack">
          {purposes.map(([key, p]) => {
            const picked = options.find((o) => o.value === choice[key]);
            return (
              <label key={key} className="av-field">
                <span className="av-label">{p.label} <Hint text={p.hint} /></span>
                <span className="av-select-wrap" style={{ width: "100%" }}>
                  <select className="av-select" value={choice[key] ?? ""} disabled={!isAdmin}
                    onChange={(e) => setChoice({ ...choice, [key]: e.target.value })}>
                    <option value="">{key === "explain" ? "同问数理解模型" : "演示模型（规则）"}</option>
                    {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                  <Icon name="chevron" small />
                </span>
                {p.needs_tools && picked && picked.tools !== true ? (
                  <span className="av-status av-status--warn">工具调用未检测通过</span>
                ) : null}
              </label>
            );
          })}

          <div className="av-section-label" style={{ marginTop: "var(--space-2)" }}>数据外发</div>
          <div className="av-toggle-row">
            <span className="av-toggle-row__text">
              <span>发送查询结果 <Hint text="开启后，问数结果会发给“结果解释”模型生成说明；关闭时用模板解释" /></span>
            </span>
            <Switch label="发送查询结果" checked={policy.send_results} disabled={!isAdmin}
              onChange={(v) => setPolicy({ ...policy, send_results: v })} />
          </div>
          <div className="av-toggle-row">
            <span className="av-toggle-row__text">
              <span>发送代码片段 <Hint text="开启后，导入血缘中解析不了的代码片段（已脱敏）会发给“导入血缘推断”模型；关闭时用演示规则" /></span>
            </span>
            <Switch label="发送代码片段" checked={policy.send_code} disabled={!isAdmin}
              onChange={(v) => setPolicy({ ...policy, send_code: v })} />
          </div>
          <div className="av-toggle-row">
            <span className="av-toggle-row__text">
              <span className="av-caption"><Icon name="shield" small /> 始终只发送：口径元数据、用户问题、与原话匹配的维度取值</span>
            </span>
          </div>

          {message ? <div className={`av-banner ${message.ok ? "av-banner--ok" : "av-banner--danger"}`}>{message.text}</div> : null}
          <div className="av-actions" style={{ justifyContent: "flex-end" }}>
            <button type="button" className="av-btn av-btn--secondary" disabled={!dirty}
              onClick={() => { setChoice(initial); setPolicy(settings.policy); setMessage(null); }}>还原</button>
            <button type="button" className="av-btn av-btn--primary" disabled={!isAdmin || !dirty || save.isPending}
              onClick={() => save.mutate()}>保存</button>
          </div>
        </div>
      </div>
    </>
  );
}

// --- 添加提供方 -------------------------------------------------------------------

function AddProvider({ presets, onClose, onCreated }: {
  presets: PresetsOut; onClose: () => void; onCreated: (p: Provider) => void;
}) {
  const keys = Object.keys(presets.presets);
  const [preset, setPreset] = useState(keys[0]);
  const [code, setCode] = useState(keys[0]);
  const [baseUrl, setBaseUrl] = useState(presets.presets[keys[0]].base_url);
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => unwrap(api.POST("/api/model-providers", {
      body: { code, preset: preset as Schemas["ProviderCreate"]["preset"], base_url: baseUrl || null, api_key: apiKey || null, models: [] },
    })),
    onSuccess: onCreated,
    onError: (e) => setError(errorText(e)),
  });

  return (
    <>
      <div className="av-backdrop" onClick={onClose} />
      <aside className="av-drawer" role="dialog" aria-label="添加提供方">
        <div className="av-pane-hd"><h2>添加提供方</h2></div>
        <div className="av-drawer__bd">
          <div className="av-field">
            <span className="av-label">类型</span>
            {keys.map((k) => (
              <label key={k} className="av-option">
                <input type="radio" name="preset" checked={preset === k} onChange={() => {
                  setPreset(k);
                  setCode(k === "custom" ? "" : k);
                  setBaseUrl(presets.presets[k].base_url);
                }} />
                <span>
                  <span className="av-option__title">{presets.presets[k].name}</span><br />
                  <span className="av-option__meta av-mono">{presets.presets[k].base_url || "自填接口地址"}</span>
                </span>
              </label>
            ))}
          </div>
          <label className="av-field">
            <span className="av-label">编码 <Hint text="小写字母、数字、- 或 _，用于区分同类提供方" /></span>
            <input className="av-input av-mono" value={code} onChange={(e) => setCode(e.target.value)} />
          </label>
          <label className="av-field">
            <span className="av-label">接口地址</span>
            <input className="av-input av-mono" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </label>
          <label className="av-field">
            <span className="av-label">API 密钥</span>
            <input className="av-input av-mono" type="password" autoComplete="off" value={apiKey} onChange={(e) => setApiKey(e.target.value)} />
          </label>
          {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
        </div>
        <div className="av-drawer__ft">
          <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>取消</button>
          <button type="button" className="av-btn av-btn--primary" disabled={!code || create.isPending} onClick={() => create.mutate()}>添加</button>
        </div>
      </aside>
    </>
  );
}
