// 模型路由、备选容灾链、复杂度路由与数据外发策略面板。
import { useMutation } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, type BackupTarget, type ModelProvider, type ModelSettingsOut, type RoutingConfig } from "../../api/hooks";
import { Hint, Icon } from "../../components/Icon";
import { Banner, Select, Switch } from "../../components/ui";
import { t } from "../../i18n";

type Effort = NonNullable<Schemas["Target"]["reasoning_effort"]>;
type Message = { ok: boolean; text: string } | null;

type ModelOption = {
  value: string;
  label: string;
  tools: boolean | null | undefined;
  efforts: Schemas["EffortOut"][];
};

const MAX_BACKUPS = 5;

function parseTargetValue(v: string): { provider_id: string; model_id: string } | null {
  if (!v) return null;
  const idx = v.indexOf("|");
  if (idx <= 0) return null;
  return { provider_id: v.slice(0, idx), model_id: v.slice(idx + 1) };
}

function targetKey(t?: { provider_id?: string | null; model_id?: string | null } | null): string {
  return t?.provider_id && t?.model_id ? `${t.provider_id}|${t.model_id}` : "";
}

export function ModelRoutingPanel({
  settings,
  providers,
  isAdmin,
  onSaved,
}: {
  settings: ModelSettingsOut;
  providers: ModelProvider[];
  isAdmin: boolean;
  onSaved: () => Promise<void>;
}) {
  const purposes = Object.entries(settings.assignments);
  const options = useMemo<ModelOption[]>(
    () =>
      providers
        .filter((p) => p.enabled && p.has_api_key)
        .flatMap((p) =>
          p.models
            .filter((m) => m.enabled)
            .map((m) => ({
              value: `${p.id}|${m.id}`,
              label: `${p.name} · ${m.id}`,
              tools: m.tool_calling,
              efforts: m.efforts ?? [],
            })),
        ),
    [providers],
  );

  const initialChoice = useMemo(
    () =>
      Object.fromEntries(
        Object.entries(settings.assignments).map(([k, v]) => [k, v.provider_id && v.model_id ? `${v.provider_id}|${v.model_id}` : ""]),
      ),
    [settings.assignments],
  );
  const initialEffort = useMemo(
    () => Object.fromEntries(Object.entries(settings.assignments).map(([k, v]) => [k, v.reasoning_effort])),
    [settings.assignments],
  );
  const initialBackups = useMemo<Record<string, BackupTarget[]>>(
    () =>
      Object.fromEntries(
        Object.entries(settings.assignments).map(([k, v]) => [
          k,
          (v.backups ?? []).map((b) => ({
            provider_id: b.provider_id,
            model_id: b.model_id,
            reasoning_effort: b.reasoning_effort ?? null,
          })),
        ]),
      ),
    [settings.assignments],
  );
  const initialRouting = useMemo<RoutingConfig>(
    () => ({
      enabled: !!settings.routing?.enabled,
      simple_target: settings.routing?.simple_target ?? null,
      complex_target: settings.routing?.complex_target ?? null,
    }),
    [settings.routing],
  );

  const [choice, setChoice] = useState<Record<string, string>>(initialChoice);
  const [effort, setEffort] = useState<Record<string, string>>(initialEffort);
  const [backups, setBackups] = useState<Record<string, BackupTarget[]>>(initialBackups);
  const [routing, setRouting] = useState<RoutingConfig>(initialRouting);
  const [policy, setPolicy] = useState(settings.policy);
  const [backupPick, setBackupPick] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<Message>(null);

  useEffect(() => setChoice(initialChoice), [initialChoice]);
  useEffect(() => setEffort(initialEffort), [initialEffort]);
  useEffect(() => setBackups(initialBackups), [initialBackups]);
  useEffect(() => setRouting(initialRouting), [initialRouting]);
  useEffect(() => setPolicy(settings.policy), [settings.policy]);

  const effortOf = (targetVal: string, wanted?: string | null, fallback?: string) => {
    const efforts = options.find((o) => o.value === targetVal)?.efforts ?? [];
    if (!efforts.length) return undefined;
    return efforts.find((e) => e.value === wanted) ?? (fallback ? efforts.find((e) => e.value === fallback) : undefined) ?? efforts[0];
  };

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/api/model-settings", {
          body: {
            assignments: Object.fromEntries(
              Object.entries(choice).map(([k, v]) => {
                const parsed = parseTargetValue(v);
                if (!parsed) return [k, null];
                const resolvedEffort = effortOf(v, effort[k] ?? settings.assignments[k]?.default_effort, settings.assignments[k]?.default_effort);
                const normalizedBackups = (backups[k] ?? []).map((b) => {
                  const bk = `${b.provider_id}|${b.model_id}`;
                  const bEffort = effortOf(bk, b.reasoning_effort ?? undefined);
                  return {
                    provider_id: b.provider_id,
                    model_id: b.model_id,
                    reasoning_effort: (bEffort?.value ?? null) as Effort | null,
                  };
                });
                return [
                  k,
                  {
                    provider_id: parsed.provider_id,
                    model_id: parsed.model_id,
                    reasoning_effort: (resolvedEffort?.value ?? null) as Effort | null,
                    backups: normalizedBackups,
                  },
                ];
              }),
            ),
            policy,
            routing: {
              enabled: !!routing.enabled,
              simple_target: routing.simple_target
                ? {
                    provider_id: routing.simple_target.provider_id,
                    model_id: routing.simple_target.model_id,
                    reasoning_effort: (effortOf(targetKey(routing.simple_target), routing.simple_target.reasoning_effort)?.value ?? null) as Effort | null,
                  }
                : null,
              complex_target: routing.complex_target
                ? {
                    provider_id: routing.complex_target.provider_id,
                    model_id: routing.complex_target.model_id,
                    reasoning_effort: (effortOf(targetKey(routing.complex_target), routing.complex_target.reasoning_effort)?.value ?? null) as Effort | null,
                  }
                : null,
            },
          },
        }),
      ),
    onSuccess: async () => {
      setMessage({ ok: true, text: t("已保存") });
      await onSaved();
    },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }),
  });

  const dirty =
    JSON.stringify(choice) !== JSON.stringify(initialChoice) ||
    JSON.stringify(effort) !== JSON.stringify(initialEffort) ||
    JSON.stringify(backups) !== JSON.stringify(initialBackups) ||
    JSON.stringify(routing) !== JSON.stringify(initialRouting) ||
    JSON.stringify(policy) !== JSON.stringify(settings.policy);
  const disabled = !isAdmin || save.isPending;

  const moveBackup = (purpose: string, idx: number, dir: -1 | 1) => {
    const list = [...(backups[purpose] ?? [])];
    const nextIdx = idx + dir;
    if (nextIdx < 0 || nextIdx >= list.length) return;
    [list[idx], list[nextIdx]] = [list[nextIdx], list[idx]];
    setBackups({ ...backups, [purpose]: list });
  };

  const addBackup = (purpose: string, val: string) => {
    const parsed = parseTargetValue(val);
    if (!parsed) return;
    const list = backups[purpose] ?? [];
    if (list.length >= MAX_BACKUPS) return;
    const opt = options.find((o) => o.value === val);
    const defaultEff = (opt?.efforts[0]?.value ?? null) as Effort | null;
    setBackups({
      ...backups,
      [purpose]: [...list, { provider_id: parsed.provider_id, model_id: parsed.model_id, reasoning_effort: defaultEff }],
    });
    setBackupPick({ ...backupPick, [purpose]: "" });
  };

  const renderRoutingTarget = (
    tierKey: "simple_target" | "complex_target",
    label: string,
    hint: string,
  ) => {
    const current = routing[tierKey] ?? null;
    const val = targetKey(current);
    const picked = options.find((o) => o.value === val);
    return (
      <div className="av-routing__tier">
        <span className="av-label">
          {label} <Hint text={hint} />
        </span>
        <Select
          label={label}
          value={val}
          disabled={disabled}
          onChange={(nextVal) => {
            const parsed = parseTargetValue(nextVal);
            if (!parsed) {
              setRouting({ ...routing, [tierKey]: null });
              return;
            }
            const opt = options.find((o) => o.value === nextVal);
            setRouting({
              ...routing,
              [tierKey]: {
                provider_id: parsed.provider_id,
                model_id: parsed.model_id,
                reasoning_effort: (opt?.efforts[0]?.value ?? null) as Effort | null,
              },
            });
          }}
        >
          <option value="">{t("跟随用途默认模型")}</option>
          {val && !picked ? <option value={val} disabled>{t("已配置模型当前不可用")}</option> : null}
          {options.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </Select>
        {picked && picked.efforts.length ? (
          <Select
            label={t("{{label}}的思考强度", { label })}
            value={effortOf(val, current?.reasoning_effort)?.value ?? ""}
            disabled={disabled}
            onChange={(eff) => {
              if (!current) return;
              setRouting({
                ...routing,
                [tierKey]: { ...current, reasoning_effort: eff as Effort },
              });
            }}
          >
            {picked.efforts.map((e) => (
              <option key={e.value} value={e.value}>
                {t("思考强度：")}{e.label}
                {e.effective !== e.value ? t("（实际按“{{effective_label}}”执行）", { effective_label: e.effective_label }) : ""}
              </option>
            ))}
          </Select>
        ) : null}
      </div>
    );
  };

  return (
    <form
      className="av-routing"
      aria-label={t("默认、容灾与路由")}
      aria-busy={save.isPending}
      onSubmit={(e) => {
        e.preventDefault();
        if (!disabled && dirty) save.mutate();
      }}
    >
      <section className="av-panel" aria-label={t("默认用途与备选容灾链")}>
        <div className="av-pane-hd">
          <h2>
            {t("默认用途与备选容灾链")}{" "}
            <Hint text={t("主模型遇到限流、超时或上游故障时，按顺序自动切换至备选模型")} />
          </h2>
        </div>
        <div className="av-stack">
          <div className="av-routing__grid">
            {purposes.map(([key, p]) => {
              const picked = options.find((o) => o.value === choice[key]);
              const list = backups[key] ?? [];
              const primaryEffort = effortOf(choice[key] ?? "", effort[key] ?? p.default_effort, p.default_effort);
              return (
                <article key={key} className="av-routing__card">
                  <div className="av-routing__card-hd">
                    <div>
                      <h3>{p.label}</h3>
                      <p className="av-caption">{p.hint}</p>
                    </div>
                    {p.needs_tools ? <span className="av-tag">{t("需工具调用")}</span> : null}
                  </div>

                  <div className="av-routing__primary">
                    <label className="av-field">
                      <span className="av-label">{t("主模型")}</span>
                      <Select
                        label={p.label}
                        value={choice[key] ?? ""}
                        disabled={disabled}
                        onChange={(value) => setChoice({ ...choice, [key]: value })}
                      >
                        <option value="">
                          {key === "explain"
                            ? t("同问数理解模型")
                            : key === "analysis"
                              ? t("跟随问数默认模型")
                              : t("演示模型（规则）")}
                        </option>
                        {choice[key] && !picked ? (
                          <option value={choice[key]} disabled>{t("已配置模型当前不可用")}</option>
                        ) : null}
                        {options.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </Select>
                    </label>
                    {picked && picked.efforts.length ? (
                      <label className="av-field">
                        <span className="av-label">{t("思考强度")}</span>
                        <Select
                          value={primaryEffort?.value ?? ""}
                          disabled={disabled}
                          label={t("{{label}}的思考强度", { label: p.label })}
                          onChange={(value) => setEffort({ ...effort, [key]: value })}
                        >
                          {picked.efforts.map((e) => (
                            <option key={e.value} value={e.value}>
                              {e.label}
                              {e.effective !== e.value
                                ? t("（实际按“{{effective_label}}”执行）", { effective_label: e.effective_label })
                                : ""}
                            </option>
                          ))}
                        </Select>
                      </label>
                    ) : null}
                  </div>

                  {choice[key] && !picked ? <span className="av-status av-status--warn">{t("请选择可用模型")}</span> : null}
                  {p.needs_tools && picked && picked.tools !== true ? (
                    <span className="av-status av-status--warn">{t("工具调用未检测通过")}</span>
                  ) : null}

                  <div className="av-routing__backups">
                    <div className="av-routing__backups-hd">
                      <span className="av-caption">{t("备选容灾链（最多 5 个）")}</span>
                      <span className="av-count">{list.length} / {MAX_BACKUPS}</span>
                    </div>
                    {list.length ? (
                      <ol className="av-routing__backup-list">
                        {list.map((b, idx) => {
                          const bkVal = `${b.provider_id}|${b.model_id}`;
                          const bOpt = options.find((o) => o.value === bkVal);
                          return (
                            <li key={`${bkVal}-${idx}`} className="av-routing__backup-row">
                              <span className="av-tag av-mono">#{idx + 1}</span>
                              <Select
                                label={t("第 {{index}} 个备选模型", { index: idx + 1 })}
                                value={bkVal}
                                disabled={disabled}
                                onChange={(nextVal) => {
                                  const parsed = parseTargetValue(nextVal);
                                  if (!parsed) return;
                                  const nextOpt = options.find((o) => o.value === nextVal);
                                  const updated = list.map((item, j) =>
                                    j === idx
                                      ? {
                                          provider_id: parsed.provider_id,
                                          model_id: parsed.model_id,
                                          reasoning_effort: (nextOpt?.efforts[0]?.value ?? null) as Effort | null,
                                        }
                                      : item,
                                  );
                                  setBackups({ ...backups, [key]: updated });
                                }}
                              >
                                {!bOpt ? <option value={bkVal} disabled>{bkVal}</option> : null}
                                {options.map((o) => (
                                  <option key={o.value} value={o.value}>{o.label}</option>
                                ))}
                              </Select>
                              {bOpt && bOpt.efforts.length ? (
                                <Select
                                  label={t("第 {{index}} 个备选模型的思考强度", { index: idx + 1 })}
                                  value={effortOf(bkVal, b.reasoning_effort)?.value ?? ""}
                                  disabled={disabled}
                                  onChange={(eff) => {
                                    const updated = list.map((item, j) =>
                                      j === idx ? { ...item, reasoning_effort: eff as Effort } : item,
                                    );
                                    setBackups({ ...backups, [key]: updated });
                                  }}
                                >
                                  {bOpt.efforts.map((e) => (
                                    <option key={e.value} value={e.value}>{e.label}</option>
                                  ))}
                                </Select>
                              ) : <span className="av-routing__effort-empty av-caption">—</span>}
                              <div className="av-actions">
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm av-btn--icon"
                                  disabled={disabled || idx === 0}
                                  aria-label={t("上移第 {{index}} 个备选模型", { index: idx + 1 })}
                                  onClick={() => moveBackup(key, idx, -1)}
                                >
                                  ↑
                                </button>
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm av-btn--icon"
                                  disabled={disabled || idx === list.length - 1}
                                  aria-label={t("下移第 {{index}} 个备选模型", { index: idx + 1 })}
                                  onClick={() => moveBackup(key, idx, 1)}
                                >
                                  ↓
                                </button>
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm av-btn--icon"
                                  disabled={disabled}
                                  aria-label={t("移除第 {{index}} 个备选模型", { index: idx + 1 })}
                                  onClick={() => setBackups({ ...backups, [key]: list.filter((_, j) => j !== idx) })}
                                >
                                  <Icon name="trash" small />
                                </button>
                              </div>
                            </li>
                          );
                        })}
                      </ol>
                    ) : (
                      <span className="av-caption">{t("未配置备选模型，故障时不自动切换")}</span>
                    )}
                    {list.length < MAX_BACKUPS && options.length > 0 ? (
                      <div className="av-routing__backup-add">
                        <Select
                          label={t("为{{label}}添加备选模型", { label: p.label })}
                          value={backupPick[key] ?? ""}
                          disabled={disabled}
                          onChange={(val) => addBackup(key, val)}
                        >
                          <option value="">{t("+ 添加备选模型…")}</option>
                          {options.map((o) => (
                            <option key={o.value} value={o.value}>{o.label}</option>
                          ))}
                        </Select>
                      </div>
                    ) : null}
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      </section>

      <section className="av-panel" aria-label={t("复杂度自动路由")}>
        <div className="av-pane-hd">
          <h2>
            {t("复杂度自动路由")}{" "}
            <Hint text={t("根据问题中的对比、趋势、归因与多维拆解信号自动分流到轻快或深度推理模型")} />
          </h2>
          <div className="av-actions">
            <span className="av-caption">{routing.enabled ? t("已启用") : t("未启用")}</span>
            <Switch
              label={t("启用复杂度路由")}
              checked={!!routing.enabled}
              disabled={disabled}
              onChange={(v) => setRouting({ ...routing, enabled: v })}
            />
          </div>
        </div>
        <div className="av-stack">
          <p className="av-caption">
            {t("启用后，简单查数优先走轻快模型以降低延迟与成本；涉及同比环比、多维拆解与归因分析时自动路由至高推理模型。")}
          </p>
          <div className="av-models__assignments">
            {renderRoutingTarget(
              "simple_target",
              t("简单查询路由目标"),
              t("用于单指标查询、基础明细等低复杂度请求"),
            )}
            {renderRoutingTarget(
              "complex_target",
              t("复杂分析路由目标"),
              t("用于趋势对比、原因归因、多维拆解等高复杂度请求"),
            )}
          </div>
        </div>
      </section>

      <section className="av-panel" aria-labelledby="model-policy-title">
        <div className="av-pane-hd">
          <h2 id="model-policy-title">
            {t("数据外发")}{" "}
            <Hint text={t("默认发送查询结果与代码片段，分析模式需要它们；需保密请接入本地模型（OpenAI 兼容接口）")} />
          </h2>
        </div>
        <div className="av-stack">
          <div className="av-toggle-row">
            <span className="av-toggle-row__text">
              <span>
                {t("发送查询结果")}{" "}
                <Hint text={t("问数结果发给结果解释模型与分析模型；关闭时使用模板解释，分析模式不可用")} />
              </span>
            </span>
            <Switch label={t("发送查询结果")} checked={policy.send_results} disabled={disabled} onChange={(v) => setPolicy({ ...policy, send_results: v })} />
          </div>
          <div className="av-toggle-row">
            <span className="av-toggle-row__text">
              <span>
                {t("发送代码片段")}{" "}
                <Hint text={t("未能解析的代码脱敏后发给导入血缘推断模型；脱敏是尽力而为，敏感环境请使用本地模型；关闭时使用演示规则")} />
              </span>
            </span>
            <Switch label={t("发送代码片段")} checked={policy.send_code} disabled={disabled} onChange={(v) => setPolicy({ ...policy, send_code: v })} />
          </div>
          <div className="av-models__feedback" role={message?.ok === false && !save.isPending ? "alert" : "status"} aria-busy={save.isPending}>
            {save.isPending ? <span className="av-status">{t("正在保存…")}</span> : <Banner message={message} />}
          </div>
          <div className="av-actions">
            <button
              type="button"
              className="av-btn av-btn--secondary"
              disabled={disabled || !dirty}
              onClick={() => {
                setChoice(initialChoice);
                setEffort(initialEffort);
                setBackups(initialBackups);
                setRouting(initialRouting);
                setPolicy(settings.policy);
                setMessage(null);
              }}
            >
              {t("还原")}
            </button>
            <button type="submit" className="av-btn av-btn--primary" disabled={disabled || !dirty}>
              {save.isPending ? t("保存中…") : t("保存配置")}
            </button>
          </div>
        </div>
      </section>
    </form>
  );
}
