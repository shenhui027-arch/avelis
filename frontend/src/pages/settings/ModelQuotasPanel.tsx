// 模型定价目录、个人实时配额用量条与管理员配额/RPM 规则管理面板。
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { api, unwrap } from "../../api/client";
import {
  errorText,
  useDomains,
  useLlmQuotas,
  useModelPricing,
  useMyLlmUsage,
  useUsers,
  type ModelProvider,
  type QuotaRule,
  type QuotaStatus,
} from "../../api/hooks";
import { Hint, Icon } from "../../components/Icon";
import { Banner, ConfirmDialog, Drawer, Empty, Meter, Pill, QueryState, Select, SuggestInput, Switch } from "../../components/ui";
import { formatCompact, formatTime } from "../../format";
import { t } from "../../i18n";

type Message = { ok: boolean; text: string } | null;

type PricingDraft = {
  provider_code: string;
  model_id: string;
  input_per_1m: string;
  output_per_1m: string;
  cache_hit_per_1m: string;
  currency: string;
};

type QuotaDraft = {
  scope_type: "global" | "user" | "domain";
  scope_id: string;
  daily_token_limit: string;
  monthly_token_limit: string;
  daily_cost_limit: string;
  monthly_cost_limit: string;
  rpm_limit: string;
  enabled: boolean;
};

function fmtPrice(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  return Number.isInteger(v) ? v.toFixed(2) : String(v);
}

function fmtCost(v: number, currency = "USD"): string {
  const sym = currency === "CNY" ? "¥" : "$";
  return `${sym}${v.toFixed(v > 0 && v < 0.01 ? 4 : 2)}`;
}

function parseOptionalInt(s: string): number | null {
  const trimmed = s.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n >= 0 ? Math.floor(n) : null;
}

function parseOptionalFloat(s: string): number | null {
  const trimmed = s.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

export function ModelQuotasPanel({
  providers,
  isAdmin,
  timeZone,
}: {
  providers: ModelProvider[];
  isAdmin: boolean;
  timeZone: string;
}) {
  const qc = useQueryClient();
  const pricing = useModelPricing(true);
  const myUsage = useMyLlmUsage(true);
  const quotas = useLlmQuotas(isAdmin);
  const users = useUsers(undefined, isAdmin);
  const domains = useDomains();

  const [pricingMsg, setPricingMsg] = useState<Message>(null);
  const [quotaMsg, setQuotaMsg] = useState<Message>(null);
  const [pricingDraft, setPricingDraft] = useState<PricingDraft | null>(null);
  const [deletePricingTarget, setDeletePricingTarget] = useState<{ provider_code: string; model_id: string } | null>(null);
  const [quotaDraft, setQuotaDraft] = useState<QuotaDraft | null>(null);
  const [deleteQuotaTarget, setDeleteQuotaTarget] = useState<QuotaRule | null>(null);

  const userNameMap = useMemo(
    () => new Map((users.data ?? []).map((u) => [u.id, `${u.display_name} (${u.username})`])),
    [users.data],
  );
  const domainNameMap = useMemo(
    () => new Map((domains.data ?? []).map((d) => [d.id, d.name])),
    [domains.data],
  );

  const providerCodes = useMemo(
    () => ["*", ...new Set(providers.map((p) => p.code))],
    [providers],
  );
  const modelSuggestions = useMemo(() => {
    const ids = new Set<string>();
    for (const p of providers) for (const m of p.models) ids.add(m.id);
    for (const d of pricing.data?.defaults ?? []) ids.add(d.model_id);
    return [...ids].sort();
  }, [providers, pricing.data?.defaults]);

  const savePricing = useMutation({
    mutationFn: (items: {
      provider_code?: string;
      model_id: string;
      input_per_1m?: number;
      output_per_1m?: number;
      cache_hit_per_1m?: number | null;
      currency?: string;
      delete?: boolean;
    }[]) => unwrap(api.PUT("/api/model-pricing", { body: { items } })),
    onSuccess: async () => {
      setPricingMsg({ ok: true, text: t("定价已更新") });
      setPricingDraft(null);
      setDeletePricingTarget(null);
      await qc.invalidateQueries({ queryKey: ["model-pricing"] });
    },
    onError: (e) => setPricingMsg({ ok: false, text: errorText(e) }),
  });

  const saveQuota = useMutation({
    mutationFn: (body: {
      scope_type: "global" | "user" | "domain";
      scope_id: string;
      daily_token_limit: number | null;
      monthly_token_limit: number | null;
      daily_cost_limit: number | null;
      monthly_cost_limit: number | null;
      rpm_limit: number | null;
      enabled: boolean;
    }) => unwrap(api.PUT("/api/llm-quotas", { body })),
    onSuccess: async () => {
      setQuotaMsg({ ok: true, text: t("配额规则已保存") });
      setQuotaDraft(null);
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["llm-quotas"] }),
        qc.invalidateQueries({ queryKey: ["llm-usage", "me"] }),
      ]);
    },
    onError: (e) => setQuotaMsg({ ok: false, text: errorText(e) }),
  });

  const removeQuota = useMutation({
    mutationFn: (id: string) => unwrap(api.DELETE("/api/llm-quotas/{quota_id}", { params: { path: { quota_id: id } } })),
    onSuccess: async () => {
      setQuotaMsg({ ok: true, text: t("配额规则已删除") });
      setDeleteQuotaTarget(null);
      await Promise.all([
        qc.invalidateQueries({ queryKey: ["llm-quotas"] }),
        qc.invalidateQueries({ queryKey: ["llm-usage", "me"] }),
      ]);
    },
    onError: (e) => setQuotaMsg({ ok: false, text: errorText(e) }),
  });

  const scopeLabel = (type: string, id: string) => {
    if (type === "global") return t("全局默认 (*)");
    if (type === "user") return userNameMap.get(id) ?? id;
    if (type === "domain") return domainNameMap.get(id) ?? id;
    return id;
  };

  const scopeTypeLabel = (type: string) =>
    type === "global" ? t("全局") : type === "user" ? t("用户") : t("业务域");

  const renderMeterList = (q: QuotaStatus) => {
    const bars: { label: string; used: number; max: number; text: string }[] = [];
    if (q.daily_token_limit != null && q.daily_token_limit > 0) {
      bars.push({
        label: t("日 Token 用量"),
        used: q.used_daily_tokens,
        max: q.daily_token_limit,
        text: `${formatCompact(q.used_daily_tokens)} / ${formatCompact(q.daily_token_limit)}`,
      });
    }
    if (q.monthly_token_limit != null && q.monthly_token_limit > 0) {
      bars.push({
        label: t("月 Token 用量"),
        used: q.used_monthly_tokens,
        max: q.monthly_token_limit,
        text: `${formatCompact(q.used_monthly_tokens)} / ${formatCompact(q.monthly_token_limit)}`,
      });
    }
    if (q.daily_cost_limit != null && q.daily_cost_limit > 0) {
      bars.push({
        label: t("日费用用量"),
        used: q.used_daily_cost,
        max: q.daily_cost_limit,
        text: `${fmtCost(q.used_daily_cost)} / ${fmtCost(q.daily_cost_limit)}`,
      });
    }
    if (q.monthly_cost_limit != null && q.monthly_cost_limit > 0) {
      bars.push({
        label: t("月费用用量"),
        used: q.used_monthly_cost,
        max: q.monthly_cost_limit,
        text: `${fmtCost(q.used_monthly_cost)} / ${fmtCost(q.monthly_cost_limit)}`,
      });
    }
    return bars;
  };

  return (
    <div className="av-quotas">
      <section className="av-panel" aria-label={t("我的配额状态")}>
        <div className="av-pane-hd">
          <h2>
            {t("当前账号配额与消耗")}{" "}
            <Hint text={t("按 UTC 自然日与自然月统计；同时展示对你生效的全局、业务域与个人配额进度")} />
          </h2>
          {myUsage.data ? (
            <div className="av-actions">
              <span className="av-tag av-mono">
                {t("今日")} {formatCompact(myUsage.data.today_tokens)} · {fmtCost(myUsage.data.today_cost, myUsage.data.summary.currency)}
              </span>
              <span className="av-tag av-mono">
                {t("本月")} {formatCompact(myUsage.data.month_tokens)} · {fmtCost(myUsage.data.month_cost, myUsage.data.summary.currency)}
              </span>
            </div>
          ) : null}
        </div>
        <div className="av-stack">
          <QueryState query={myUsage} rows={2} compact>
            {(data) =>
              data.quotas.length ? (
                <div className="av-quotas__meters">
                  {data.quotas.map((q) => (
                    <article key={q.id} className="av-quotas__meter-card">
                      <div className="av-quotas__meter-hd">
                        <Pill tone="info">{scopeTypeLabel(q.scope_type)}</Pill>
                        <span className="av-row__title">{scopeLabel(q.scope_type, q.scope_id)}</span>
                        {q.rpm_limit != null ? (
                          <span className="av-tag av-mono">{q.rpm_limit} RPM</span>
                        ) : null}
                      </div>
                      {renderMeterList(q).map((bar) => (
                        <div key={bar.label} className="av-quotas__bar">
                          <div className="av-quotas__bar-hd">
                            <span className="av-caption">{bar.label}</span>
                            <span className="av-caption av-mono">{bar.text}</span>
                          </div>
                          <Meter value={bar.used} max={bar.max} label={bar.label} />
                        </div>
                      ))}
                    </article>
                  ))}
                </div>
              ) : (
                <span className="av-caption">{t("当前未命中任何启用中的配额上限，仅受默认 RPM 频次保护")}</span>
              )
            }
          </QueryState>
        </div>
      </section>

      <section className="av-panel" aria-label={t("模型定价")}>
        <div className="av-pane-hd">
          <h2>
            {t("模型定价（每百万 Tokens）")}{" "}
            <Hint text={t("自定义定价优先于内置参考价；精确提供方编码优先于通配符 *")} />
          </h2>
          <button
            type="button"
            className="av-btn av-btn--secondary av-btn--sm"
            disabled={!isAdmin}
            onClick={() =>
              setPricingDraft({
                provider_code: "*",
                model_id: "",
                input_per_1m: "0.50",
                output_per_1m: "1.50",
                cache_hit_per_1m: "",
                currency: "USD",
              })
            }
          >
            <Icon name="plus" small />
            {t("添加定价")}
          </button>
        </div>
        <div className="av-stack">
          <Banner message={pricingMsg} />
          <QueryState query={pricing} rows={4} compact>
            {(catalog) => (
              <>
                {catalog.configured.length ? (
                  <div className="av-table-wrap">
                    <table className="av-table">
                      <thead>
                        <tr>
                          <th>{t("提供方编码")}</th>
                          <th>{t("模型")}</th>
                          <th className="num">{t("输入 (/1M)")}</th>
                          <th className="num">{t("输出 (/1M)")}</th>
                          <th className="num">{t("缓存命中 (/1M)")}</th>
                          <th>{t("币种")}</th>
                          <th>{t("更新时间")}</th>
                          <th className="end">{t("操作")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {catalog.configured.map((p) => (
                          <tr key={p.id}>
                            <td className="av-mono">{p.provider_code}</td>
                            <td className="av-mono">{p.model_id}</td>
                            <td className="num">{fmtPrice(p.input_per_1m)}</td>
                            <td className="num">{fmtPrice(p.output_per_1m)}</td>
                            <td className="num">{fmtPrice(p.cache_hit_per_1m)}</td>
                            <td className="av-mono">{p.currency}</td>
                            <td className="av-mono">{formatTime(p.updated_at, timeZone)}</td>
                            <td className="end">
                              <div className="av-actions">
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm"
                                  disabled={!isAdmin}
                                  onClick={() =>
                                    setPricingDraft({
                                      provider_code: p.provider_code,
                                      model_id: p.model_id,
                                      input_per_1m: String(p.input_per_1m),
                                      output_per_1m: String(p.output_per_1m),
                                      cache_hit_per_1m: p.cache_hit_per_1m != null ? String(p.cache_hit_per_1m) : "",
                                      currency: p.currency,
                                    })
                                  }
                                >
                                  {t("编辑")}
                                </button>
                                <button
                                  type="button"
                                  className="av-btn av-btn--ghost av-btn--sm av-btn--icon"
                                  disabled={!isAdmin}
                                  aria-label={t("删除定价 {{model}}", { model: p.model_id })}
                                  onClick={() => setDeletePricingTarget({ provider_code: p.provider_code, model_id: p.model_id })}
                                >
                                  <Icon name="trash" small />
                                </button>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <Empty compact text={t("尚未配置自定义模型定价，当前按内置参考单价估算费用")} />
                )}

                <details className="av-fold">
                  <summary>
                    <span>{t("内置参考单价目录")}</span>
                    <span className="av-count">{catalog.defaults.length}</span>
                  </summary>
                  <div className="av-table-wrap">
                    <table className="av-table">
                      <thead>
                        <tr>
                          <th>{t("模型")}</th>
                          <th className="num">{t("输入 (/1M)")}</th>
                          <th className="num">{t("输出 (/1M)")}</th>
                          <th className="num">{t("缓存命中 (/1M)")}</th>
                          <th>{t("币种")}</th>
                          <th className="end">{t("操作")}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {catalog.defaults.map((d) => (
                          <tr key={d.model_id}>
                            <td className="av-mono">{d.model_id}</td>
                            <td className="num">{fmtPrice(d.input_per_1m)}</td>
                            <td className="num">{fmtPrice(d.output_per_1m)}</td>
                            <td className="num">{fmtPrice(d.cache_hit_per_1m)}</td>
                            <td className="av-mono">{d.currency}</td>
                            <td className="end">
                              <button
                                type="button"
                                className="av-btn av-btn--ghost av-btn--sm"
                                disabled={!isAdmin}
                                onClick={() =>
                                  setPricingDraft({
                                    provider_code: "*",
                                    model_id: d.model_id,
                                    input_per_1m: String(d.input_per_1m),
                                    output_per_1m: String(d.output_per_1m),
                                    cache_hit_per_1m: d.cache_hit_per_1m != null ? String(d.cache_hit_per_1m) : "",
                                    currency: d.currency,
                                  })
                                }
                              >
                                {t("覆盖定价")}
                              </button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </details>
              </>
            )}
          </QueryState>
        </div>
      </section>

      <section className="av-panel" aria-label={t("配额与频次规则")}>
        <div className="av-pane-hd">
          <h2>
            {t("配额与限流规则")}{" "}
            <Hint text={t("可按全局、单用户或业务域设置日/月 Token 与费用上限，以及每分钟请求数（RPM）限制")} />
          </h2>
          {isAdmin ? (
            <button
              type="button"
              className="av-btn av-btn--primary av-btn--sm"
              onClick={() =>
                setQuotaDraft({
                  scope_type: "global",
                  scope_id: "*",
                  daily_token_limit: "",
                  monthly_token_limit: "",
                  daily_cost_limit: "",
                  monthly_cost_limit: "",
                  rpm_limit: "",
                  enabled: true,
                })
              }
            >
              <Icon name="plus" small />
              {t("新建配额规则")}
            </button>
          ) : null}
        </div>
        <div className="av-stack">
          <Banner message={quotaMsg} />
          {!isAdmin ? (
            <span className="av-caption">{t("仅管理员可查看和修改组织级配额规则")}</span>
          ) : (
            <QueryState
              query={quotas}
              isEmpty={(items) => items.length === 0}
              emptyText={t("尚未设置模型配额规则")}
              rows={3}
              compact
            >
              {(items) => (
                <div className="av-table-wrap">
                  <table className="av-table">
                    <thead>
                      <tr>
                        <th>{t("作用域")}</th>
                        <th>{t("对象")}</th>
                        <th className="num">{t("日 Token 上限")}</th>
                        <th className="num">{t("月 Token 上限")}</th>
                        <th className="num">{t("日费用上限")}</th>
                        <th className="num">{t("月费用上限")}</th>
                        <th className="num">RPM</th>
                        <th>{t("状态")}</th>
                        <th className="end">{t("操作")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((q) => (
                        <tr key={q.id}>
                          <td><Pill tone="info">{scopeTypeLabel(q.scope_type)}</Pill></td>
                          <td>{scopeLabel(q.scope_type, q.scope_id)}</td>
                          <td className="num">{q.daily_token_limit != null ? formatCompact(q.daily_token_limit) : "—"}</td>
                          <td className="num">{q.monthly_token_limit != null ? formatCompact(q.monthly_token_limit) : "—"}</td>
                          <td className="num">{q.daily_cost_limit != null ? fmtCost(q.daily_cost_limit) : "—"}</td>
                          <td className="num">{q.monthly_cost_limit != null ? fmtCost(q.monthly_cost_limit) : "—"}</td>
                          <td className="num">{q.rpm_limit != null ? q.rpm_limit : "—"}</td>
                          <td>
                            <Pill tone={q.enabled ? "ok" : ""}>{q.enabled ? t("已启用") : t("已停用")}</Pill>
                          </td>
                          <td className="end">
                            <div className="av-actions">
                              <button
                                type="button"
                                className="av-btn av-btn--ghost av-btn--sm"
                                onClick={() =>
                                  setQuotaDraft({
                                    scope_type: q.scope_type,
                                    scope_id: q.scope_id,
                                    daily_token_limit: q.daily_token_limit != null ? String(q.daily_token_limit) : "",
                                    monthly_token_limit: q.monthly_token_limit != null ? String(q.monthly_token_limit) : "",
                                    daily_cost_limit: q.daily_cost_limit != null ? String(q.daily_cost_limit) : "",
                                    monthly_cost_limit: q.monthly_cost_limit != null ? String(q.monthly_cost_limit) : "",
                                    rpm_limit: q.rpm_limit != null ? String(q.rpm_limit) : "",
                                    enabled: q.enabled,
                                  })
                                }
                              >
                                {t("编辑")}
                              </button>
                              <button
                                type="button"
                                className="av-btn av-btn--ghost av-btn--sm av-btn--icon"
                                aria-label={t("删除配额规则 {{target}}", { target: scopeLabel(q.scope_type, q.scope_id) })}
                                onClick={() => setDeleteQuotaTarget(q)}
                              >
                                <Icon name="trash" small />
                              </button>
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </QueryState>
          )}
        </div>
      </section>

      {pricingDraft ? (
        <Drawer
          title={t("配置模型定价")}
          onClose={() => !savePricing.isPending && setPricingDraft(null)}
          footer={
            <>
              <button type="button" className="av-btn av-btn--secondary" disabled={savePricing.isPending} onClick={() => setPricingDraft(null)}>
                {t("取消")}
              </button>
              <button
                type="submit"
                form="model-pricing-form"
                className="av-btn av-btn--primary"
                disabled={savePricing.isPending || !pricingDraft.model_id.trim()}
              >
                {savePricing.isPending ? t("保存中…") : t("保存定价")}
              </button>
            </>
          }
        >
          <form
            id="model-pricing-form"
            className="av-stack"
            onSubmit={(e) => {
              e.preventDefault();
              if (!pricingDraft.model_id.trim() || savePricing.isPending) return;
              savePricing.mutate([
                {
                  provider_code: pricingDraft.provider_code.trim() || "*",
                  model_id: pricingDraft.model_id.trim(),
                  input_per_1m: Number(pricingDraft.input_per_1m) || 0,
                  output_per_1m: Number(pricingDraft.output_per_1m) || 0,
                  cache_hit_per_1m: pricingDraft.cache_hit_per_1m.trim() ? Number(pricingDraft.cache_hit_per_1m) : null,
                  currency: pricingDraft.currency || "USD",
                },
              ]);
            }}
          >
            <label className="av-field">
              <span className="av-label">{t("提供方编码")} <Hint text={t("填 * 表示匹配所有提供方的同名模型")} /></span>
              <SuggestInput
                label={t("提供方编码")}
                className="av-input av-mono"
                value={pricingDraft.provider_code}
                suggestions={providerCodes}
                placeholder="*"
                onChange={(v) => setPricingDraft({ ...pricingDraft, provider_code: v })}
              />
            </label>
            <label className="av-field">
              <span className="av-label">{t("模型 ID")}</span>
              <SuggestInput
                label={t("模型 ID")}
                className="av-input av-mono"
                value={pricingDraft.model_id}
                suggestions={modelSuggestions}
                required
                placeholder="gpt-5"
                onChange={(v) => setPricingDraft({ ...pricingDraft, model_id: v })}
              />
            </label>
            <div className="av-formrow">
              <label className="av-field">
                <span className="av-label">{t("输入单价 (/ 1M Tokens)")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  step="0.0001"
                  min="0"
                  required
                  value={pricingDraft.input_per_1m}
                  onChange={(e) => setPricingDraft({ ...pricingDraft, input_per_1m: e.target.value })}
                />
              </label>
              <label className="av-field">
                <span className="av-label">{t("输出单价 (/ 1M Tokens)")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  step="0.0001"
                  min="0"
                  required
                  value={pricingDraft.output_per_1m}
                  onChange={(e) => setPricingDraft({ ...pricingDraft, output_per_1m: e.target.value })}
                />
              </label>
            </div>
            <div className="av-formrow">
              <label className="av-field">
                <span className="av-label">{t("缓存命中单价 (/ 1M)")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  step="0.0001"
                  min="0"
                  placeholder={t("留空则按输入单价")}
                  value={pricingDraft.cache_hit_per_1m}
                  onChange={(e) => setPricingDraft({ ...pricingDraft, cache_hit_per_1m: e.target.value })}
                />
              </label>
              <label className="av-field">
                <span className="av-label">{t("币种")}</span>
                <Select
                  label={t("币种")}
                  value={pricingDraft.currency}
                  onChange={(currency) => setPricingDraft({ ...pricingDraft, currency })}
                >
                  <option value="USD">USD ($)</option>
                  <option value="CNY">CNY (¥)</option>
                </Select>
              </label>
            </div>
          </form>
        </Drawer>
      ) : null}

      {deletePricingTarget ? (
        <ConfirmDialog
          title={t("删除自定义定价 {{model}}？", { model: deletePricingTarget.model_id })}
          body={t("删除后将回退到内置参考单价（如有）。")}
          danger
          confirm={t("删除")}
          onCancel={() => setDeletePricingTarget(null)}
          onConfirm={async () => {
            await savePricing.mutateAsync([{ ...deletePricingTarget, delete: true }]);
          }}
        />
      ) : null}

      {quotaDraft ? (
        <Drawer
          title={t("配置配额与限流规则")}
          onClose={() => !saveQuota.isPending && setQuotaDraft(null)}
          footer={
            <>
              <button type="button" className="av-btn av-btn--secondary" disabled={saveQuota.isPending} onClick={() => setQuotaDraft(null)}>
                {t("取消")}
              </button>
              <button
                type="submit"
                form="llm-quota-form"
                className="av-btn av-btn--primary"
                disabled={saveQuota.isPending || !quotaDraft.scope_id}
              >
                {saveQuota.isPending ? t("保存中…") : t("保存规则")}
              </button>
            </>
          }
        >
          <form
            id="llm-quota-form"
            className="av-stack"
            onSubmit={(e) => {
              e.preventDefault();
              if (!quotaDraft.scope_id || saveQuota.isPending) return;
              saveQuota.mutate({
                scope_type: quotaDraft.scope_type,
                scope_id: quotaDraft.scope_id,
                daily_token_limit: parseOptionalInt(quotaDraft.daily_token_limit),
                monthly_token_limit: parseOptionalInt(quotaDraft.monthly_token_limit),
                daily_cost_limit: parseOptionalFloat(quotaDraft.daily_cost_limit),
                monthly_cost_limit: parseOptionalFloat(quotaDraft.monthly_cost_limit),
                rpm_limit: parseOptionalInt(quotaDraft.rpm_limit),
                enabled: quotaDraft.enabled,
              });
            }}
          >
            <label className="av-field">
              <span className="av-label">{t("作用域类型")}</span>
              <Select
                label={t("作用域类型")}
                value={quotaDraft.scope_type}
                onChange={(val) => {
                  const st = val as "global" | "user" | "domain";
                  const nextId = st === "global" ? "*" : st === "user" ? (users.data?.[0]?.id ?? "") : (domains.data?.[0]?.id ?? "");
                  setQuotaDraft({ ...quotaDraft, scope_type: st, scope_id: nextId });
                }}
              >
                <option value="global">{t("全局")}</option>
                <option value="user">{t("用户")}</option>
                <option value="domain">{t("业务域")}</option>
              </Select>
            </label>
            {quotaDraft.scope_type === "user" ? (
              <label className="av-field">
                <span className="av-label">{t("目标用户")}</span>
                <Select
                  label={t("目标用户")}
                  value={quotaDraft.scope_id}
                  onChange={(scope_id) => setQuotaDraft({ ...quotaDraft, scope_id })}
                >
                  {(users.data ?? []).map((u) => (
                    <option key={u.id} value={u.id}>{u.display_name} ({u.username})</option>
                  ))}
                </Select>
              </label>
            ) : quotaDraft.scope_type === "domain" ? (
              <label className="av-field">
                <span className="av-label">{t("目标业务域")}</span>
                <Select
                  label={t("目标业务域")}
                  value={quotaDraft.scope_id}
                  onChange={(scope_id) => setQuotaDraft({ ...quotaDraft, scope_id })}
                >
                  {(domains.data ?? []).map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </Select>
              </label>
            ) : null}
            <div className="av-formrow">
              <label className="av-field">
                <span className="av-label">{t("日 Token 上限")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  min="0"
                  placeholder={t("不限制")}
                  value={quotaDraft.daily_token_limit}
                  onChange={(e) => setQuotaDraft({ ...quotaDraft, daily_token_limit: e.target.value })}
                />
              </label>
              <label className="av-field">
                <span className="av-label">{t("月 Token 上限")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  min="0"
                  placeholder={t("不限制")}
                  value={quotaDraft.monthly_token_limit}
                  onChange={(e) => setQuotaDraft({ ...quotaDraft, monthly_token_limit: e.target.value })}
                />
              </label>
            </div>
            <div className="av-formrow">
              <label className="av-field">
                <span className="av-label">{t("日费用上限")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  step="0.01"
                  min="0"
                  placeholder={t("不限制")}
                  value={quotaDraft.daily_cost_limit}
                  onChange={(e) => setQuotaDraft({ ...quotaDraft, daily_cost_limit: e.target.value })}
                />
              </label>
              <label className="av-field">
                <span className="av-label">{t("月费用上限")}</span>
                <input
                  className="av-input av-mono"
                  type="number"
                  step="0.01"
                  min="0"
                  placeholder={t("不限制")}
                  value={quotaDraft.monthly_cost_limit}
                  onChange={(e) => setQuotaDraft({ ...quotaDraft, monthly_cost_limit: e.target.value })}
                />
              </label>
            </div>
            <label className="av-field">
              <span className="av-label">{t("每分钟请求上限 (RPM)")}</span>
              <input
                className="av-input av-mono"
                type="number"
                min="0"
                placeholder={t("留空使用系统默认值")}
                value={quotaDraft.rpm_limit}
                onChange={(e) => setQuotaDraft({ ...quotaDraft, rpm_limit: e.target.value })}
              />
            </label>
            <div className="av-toggle-row">
              <span className="av-toggle-row__text"><span>{t("启用此配额规则")}</span></span>
              <Switch
                label={t("启用此配额规则")}
                checked={quotaDraft.enabled}
                onChange={(enabled) => setQuotaDraft({ ...quotaDraft, enabled })}
              />
            </div>
          </form>
        </Drawer>
      ) : null}

      {deleteQuotaTarget ? (
        <ConfirmDialog
          title={t("删除配额规则 {{target}}？", { target: scopeLabel(deleteQuotaTarget.scope_type, deleteQuotaTarget.scope_id) })}
          danger
          confirm={t("删除")}
          onCancel={() => setDeleteQuotaTarget(null)}
          onConfirm={async () => {
            await removeQuota.mutateAsync(deleteQuotaTarget.id);
          }}
        />
      ) : null}
    </div>
  );
}
