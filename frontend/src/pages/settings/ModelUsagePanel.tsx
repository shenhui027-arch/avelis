// 模型用量与费用账本面板：4 枚 KPI 汇总卡、每日 Token/费用趋势图、多维筛选与分页流水表。
import { useMemo, useState } from "react";
import { Link } from "react-router";

import {
  useDomains,
  useLlmUsageLedger,
  useLlmUsageSummary,
  useUsers,
  type ModelProvider,
} from "../../api/hooks";
import { ChartPanel } from "../../components/charts/ChartPanel";
import type { ChartSpec } from "../../components/charts/chartOption";
import { CountUp } from "../../components/Motion";
import { DateInput, Empty, Pager, Pill, QueryState, Seg, Select, SuggestInput, Tile } from "../../components/ui";
import { formatCompact, formatTime } from "../../format";
import { t } from "../../i18n";

const PAGE_SIZE = 20;

const PURPOSE_LABEL: Record<string, string> = {
  ask: t("问数理解"),
  explain: t("结果解释"),
  import: t("导入血缘推断"),
  analysis: t("分析"),
};

const TIER_LABEL: Record<string, string> = {
  simple: t("简单路由"),
  complex: t("复杂路由"),
};

function fmtCurrency(amount: number, currency = "USD"): string {
  const sym = currency === "CNY" ? "¥" : "$";
  return `${sym}${amount.toFixed(amount > 0 && amount < 0.01 ? 4 : 2)}`;
}

function statusPill(status: string) {
  if (status === "ok" || status === "succeeded") return <Pill tone="ok">{t("成功")}</Pill>;
  if (status === "failover") return <Pill tone="warn">{t("容灾切换")}</Pill>;
  if (status === "quota_exceeded") return <Pill tone="warn">{t("配额拦截")}</Pill>;
  if (status === "rate_limited") return <Pill tone="warn">{t("触发限流")}</Pill>;
  if (status === "error" || status === "failed") return <Pill tone="danger">{t("失败")}</Pill>;
  return <Pill>{status}</Pill>;
}

export function ModelUsagePanel({
  providers,
  isAdmin,
  timeZone,
}: {
  providers: ModelProvider[];
  isAdmin: boolean;
  timeZone: string;
}) {
  const [days, setDays] = useState<"7" | "30" | "90">("30");
  const [purpose, setPurpose] = useState("");
  const [status, setStatus] = useState("");
  const [modelId, setModelId] = useState("");
  const [userId, setUserId] = useState("");
  const [domainId, setDomainId] = useState("");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [offset, setOffset] = useState(0);

  const users = useUsers(undefined, isAdmin);
  const domains = useDomains();

  const summary = useLlmUsageSummary(
    {
      days: Number(days),
      ...(isAdmin && userId ? { user_id: userId } : {}),
      ...(isAdmin && domainId ? { domain_id: domainId } : {}),
    },
    true,
  );
  const ledger = useLlmUsageLedger(
    {
      days: startDate || endDate ? undefined : Number(days),
      ...(purpose ? { purpose } : {}),
      ...(status ? { status } : {}),
      ...(modelId.trim() ? { model_id: modelId.trim() } : {}),
      ...(isAdmin && userId ? { user_id: userId } : {}),
      ...(isAdmin && domainId ? { domain_id: domainId } : {}),
      ...(startDate ? { start_date: startDate } : {}),
      ...(endDate ? { end_date: endDate } : {}),
      limit: PAGE_SIZE,
      offset,
    },
    true,
  );

  const modelSuggestions = useMemo(() => {
    const ids = new Set<string>();
    for (const p of providers) for (const m of p.models) ids.add(m.id);
    for (const m of summary.data?.by_model ?? []) ids.add(m.model_id);
    return [...ids].sort();
  }, [providers, summary.data?.by_model]);

  const userNameMap = useMemo(
    () => new Map((users.data ?? []).map((u) => [u.id, u.display_name])),
    [users.data],
  );
  const domainNameMap = useMemo(
    () => new Map((domains.data ?? []).map((d) => [d.id, d.name])),
    [domains.data],
  );

  const hasFilters = !!(purpose || status || modelId.trim() || userId || domainId || startDate || endDate);
  const resetFilters = () => {
    setPurpose("");
    setStatus("");
    setModelId("");
    setUserId("");
    setDomainId("");
    setStartDate("");
    setEndDate("");
    setOffset(0);
  };

  const chartSpec = useMemo<ChartSpec | null>(() => {
    const daily = summary.data?.daily ?? [];
    if (!daily.length) return null;
    const currency = summary.data?.currency === "CNY" ? "¥" : "$";
    return {
      kind: "combo",
      orientation: "vertical",
      reason: t("每日模型消耗与费用趋势"),
      title: null,
      x: { column: "date", label: t("日期"), role: "time", grain: "day" },
      categories: daily.map((d) => d.date),
      series: [
        {
          name: t("问数 Tokens"),
          column: "ask_tokens",
          label: t("问数 Tokens"),
          axis: 0,
          mark: "bar",
          unit: { kind: "count" },
          polarity: null,
          values: daily.map((d) => d.ask_tokens),
        },
        {
          name: t("分析 Tokens"),
          column: "analysis_tokens",
          label: t("分析 Tokens"),
          axis: 0,
          mark: "bar",
          unit: { kind: "count" },
          polarity: null,
          values: daily.map((d) => d.analysis_tokens),
        },
        {
          name: t("预估费用"),
          column: "estimated_cost",
          label: t("预估费用"),
          axis: 1,
          mark: "line",
          unit: { kind: "level", unit: currency },
          polarity: null,
          values: daily.map((d) => d.estimated_cost),
        },
      ],
      kpis: [],
      others: null,
      dropped_series: 0,
    };
  }, [summary.data]);

  const askCalls = (summary.data?.by_purpose ?? [])
    .filter((p) => p.purpose === "ask" || p.purpose === "explain")
    .reduce((sum, p) => sum + p.calls, 0);
  const analysisCalls = (summary.data?.by_purpose ?? [])
    .filter((p) => p.purpose === "analysis")
    .reduce((sum, p) => sum + p.calls, 0);

  return (
    <div className="av-usage">
      <div className="av-usage__toolbar">
        <Seg
          label={t("统计周期")}
          value={days}
          onChange={(v) => {
            setDays(v);
            setOffset(0);
          }}
          options={[
            { value: "7", label: t("近 7 天") },
            { value: "30", label: t("近 30 天") },
            { value: "90", label: t("近 90 天") },
          ]}
        />
        {!isAdmin ? (
          <span className="av-caption">{t("当前仅展示你个人发起的模型调用明细")}</span>
        ) : null}
      </div>

      <QueryState query={summary} rows={2} compact>
        {(s) => (
          <>
            <div className="av-usage__kpis">
              <article className="av-kpi av-usage__kpi">
                <div className="av-usage__kpi-hd">
                  <Tile icon="chip" tone="accent" size="sm" />
                  <span className="av-kpi__label">{t("总消耗 Tokens")}</span>
                </div>
                <span className="av-kpi__value av-mono">
                  <CountUp value={formatCompact(s.total_tokens)} />
                </span>
                <span className="av-kpi__delta av-mono">
                  <span>{t("输入")} {formatCompact(s.input_tokens)}</span>
                  <span>{t("输出")} {formatCompact(s.output_tokens)}</span>
                  {s.reasoning_tokens > 0 ? <span>{t("推理")} {formatCompact(s.reasoning_tokens)}</span> : null}
                  {s.cache_hit_tokens > 0 ? <span>{t("缓存")} {formatCompact(s.cache_hit_tokens)}</span> : null}
                </span>
              </article>

              <article className="av-kpi av-usage__kpi">
                <div className="av-usage__kpi-hd">
                  <Tile icon="chart" tone="ask" size="sm" />
                  <span className="av-kpi__label">{t("预估总费用")}</span>
                </div>
                <span className="av-kpi__value av-mono">{fmtCurrency(s.estimated_cost, s.currency)}</span>
                <span className="av-kpi__delta">
                  <span>{t("最近 {{count}} 天", { count: s.days })}</span>
                  <span className="av-mono">{s.currency}</span>
                </span>
              </article>

              <article className="av-kpi av-usage__kpi">
                <div className="av-usage__kpi-hd">
                  <Tile icon="ask" tone="collect" size="sm" />
                  <span className="av-kpi__label">{t("模型调用次数")}</span>
                </div>
                <span className="av-kpi__value av-mono">
                  <CountUp value={s.total_calls} />
                </span>
                <span className="av-kpi__delta av-mono">
                  <span>{t("问数")} {askCalls}</span>
                  <span>{t("分析")} {analysisCalls}</span>
                </span>
              </article>

              <article className="av-kpi av-usage__kpi">
                <div className="av-usage__kpi-hd">
                  <Tile icon="shield" tone={s.error_count > 0 ? "danger" : s.failover_count > 0 ? "warn" : "ok"} size="sm" />
                  <span className="av-kpi__label">{t("容灾与异常")}</span>
                </div>
                <span className="av-kpi__value av-mono">{s.failover_count + s.error_count}</span>
                <span className="av-kpi__delta av-mono">
                  <span>{t("容灾切换")} {s.failover_count}</span>
                  <span className={s.error_count > 0 ? "is-danger" : ""}>{t("失败")} {s.error_count}</span>
                </span>
              </article>
            </div>

            <section className="av-panel" aria-label={t("每日用量与费用趋势")}>
              <div className="av-pane-hd">
                <h2>{t("每日用量与费用趋势")}</h2>
                {s.by_model.length ? (
                  <div className="av-actions">
                    {s.by_model.slice(0, 3).map((m) => (
                      <span key={`${m.provider_code}-${m.model_id}`} className="av-tag av-mono">
                        {m.model_id}: {formatCompact(m.total_tokens)}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="av-panel__bd">
                {chartSpec ? (
                  <ChartPanel charts={[chartSpec]} title={t("每日用量与费用趋势")} />
                ) : (
                  <Empty compact text={t("该周期内暂无用量趋势数据")} />
                )}
              </div>
            </section>
          </>
        )}
      </QueryState>

      <section className="av-panel" aria-label={t("调用账本明细")}>
        <div className="av-pane-hd">
          <h2>{t("调用账本明细")}</h2>
          <div className="av-actions">
            {hasFilters ? (
              <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={resetFilters}>
                {t("重置筛选")}
              </button>
            ) : null}
          </div>
        </div>
        <div className="av-stack">
          <div className="av-usage__filters">
            <DateInput
              label={t("开始日期")}
              placeholder={t("开始日期")}
              value={startDate}
              onChange={(v) => {
                setStartDate(v);
                setOffset(0);
              }}
            />
            <DateInput
              label={t("结束日期")}
              placeholder={t("结束日期")}
              value={endDate}
              onChange={(v) => {
                setEndDate(v);
                setOffset(0);
              }}
            />
            <Select
              label={t("场景")}
              value={purpose}
              onChange={(v) => {
                setPurpose(v);
                setOffset(0);
              }}
            >
              <option value="">{t("全部场景")}</option>
              <option value="ask">{t("问数理解")}</option>
              <option value="explain">{t("结果解释")}</option>
              <option value="analysis">{t("分析")}</option>
              <option value="import">{t("导入血缘推断")}</option>
            </Select>
            <Select
              label={t("状态")}
              value={status}
              onChange={(v) => {
                setStatus(v);
                setOffset(0);
              }}
            >
              <option value="">{t("全部状态")}</option>
              <option value="ok">{t("成功")}</option>
              <option value="failover">{t("容灾切换")}</option>
              <option value="error">{t("失败")}</option>
            </Select>
            <SuggestInput
              label={t("按模型筛选")}
              className="av-input av-mono"
              value={modelId}
              suggestions={modelSuggestions}
              placeholder={t("按模型 ID 筛选")}
              onChange={(v) => {
                setModelId(v);
                setOffset(0);
              }}
            />
            {isAdmin ? (
              <>
                <Select
                  label={t("用户")}
                  value={userId}
                  onChange={(v) => {
                    setUserId(v);
                    setOffset(0);
                  }}
                >
                  <option value="">{t("全部用户")}</option>
                  {(users.data ?? []).map((u) => (
                    <option key={u.id} value={u.id}>{u.display_name}</option>
                  ))}
                </Select>
                <Select
                  label={t("业务域")}
                  value={domainId}
                  onChange={(v) => {
                    setDomainId(v);
                    setOffset(0);
                  }}
                >
                  <option value="">{t("全部业务域")}</option>
                  {(domains.data ?? []).map((d) => (
                    <option key={d.id} value={d.id}>{d.name}</option>
                  ))}
                </Select>
              </>
            ) : null}
          </div>

          <QueryState
            query={ledger}
            isEmpty={(page) => page.items.length === 0}
            emptyText={t("没有符合条件的模型调用记录")}
            rows={5}
            compact
          >
            {(page) => (
              <>
                <div className="av-table-wrap">
                  <table className="av-table">
                    <thead>
                      <tr>
                        <th>{t("时间")}</th>
                        <th>{t("场景")}</th>
                        {isAdmin ? <th>{t("用户 / 业务域")}</th> : null}
                        <th>{t("模型")}</th>
                        <th className="num">{t("Tokens")}</th>
                        <th className="num">{t("耗时")}</th>
                        <th className="num">{t("费用")}</th>
                        <th>{t("状态")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {page.items.map((item) => {
                        const targetLink = item.run_id
                          ? `/analysis/runs/${item.run_id}`
                          : item.thread_id
                            ? `/ask/${item.thread_id}`
                            : null;
                        return (
                          <tr key={item.id}>
                            <td className="av-mono">{formatTime(item.created_at, timeZone)}</td>
                            <td>
                              <div className="av-usage__cell-stack">
                                <span>
                                  {targetLink ? (
                                    <Link to={targetLink} className="av-link">
                                      {PURPOSE_LABEL[item.purpose] ?? item.purpose}
                                    </Link>
                                  ) : (
                                    PURPOSE_LABEL[item.purpose] ?? item.purpose
                                  )}
                                </span>
                                <span className="av-usage__sub">
                                  {item.phase ? <span className="av-caption av-mono">{item.phase}</span> : null}
                                  {item.routing_tier ? (
                                    <span className="av-tag">{TIER_LABEL[item.routing_tier] ?? item.routing_tier}</span>
                                  ) : null}
                                </span>
                              </div>
                            </td>
                            {isAdmin ? (
                              <td>
                                <div className="av-usage__cell-stack">
                                  <span>{item.user_id ? (userNameMap.get(item.user_id) ?? item.user_id) : "—"}</span>
                                  {item.domain_id ? (
                                    <span className="av-caption">{domainNameMap.get(item.domain_id) ?? item.domain_id}</span>
                                  ) : null}
                                </div>
                              </td>
                            ) : null}
                            <td>
                              <div className="av-usage__cell-stack">
                                <span className="av-mono">
                                  {item.provider_code ? `${item.provider_code} · ` : ""}
                                  {item.model_id}
                                </span>
                                {item.failover_from ? (
                                  <span className="av-usage__sub">
                                    <Pill tone="warn" title={t("由 {{source}} 自动切换", { source: item.failover_from })}>
                                      {t("已降级")}
                                    </Pill>
                                    <span className="av-caption av-mono">{item.failover_from}</span>
                                  </span>
                                ) : null}
                              </div>
                            </td>
                            <td className="num">
                              <div className="av-usage__cell-stack">
                                <span>{formatCompact(item.total_tokens)}</span>
                                <span className="av-caption">
                                  {formatCompact(item.input_tokens)} / {formatCompact(item.output_tokens)}
                                  {item.reasoning_tokens > 0 ? ` · R ${formatCompact(item.reasoning_tokens)}` : ""}
                                  {item.cache_hit_tokens > 0 ? ` · C ${formatCompact(item.cache_hit_tokens)}` : ""}
                                </span>
                              </div>
                            </td>
                            <td className="num">{item.latency_ms != null ? `${item.latency_ms} ms` : "—"}</td>
                            <td className="num">{fmtCurrency(item.estimated_cost, item.currency)}</td>
                            <td>
                              <div className="av-usage__cell-stack">
                                {statusPill(item.status)}
                                {item.error_code ? <span className="av-caption av-mono">{item.error_code}</span> : null}
                              </div>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <Pager
                  offset={offset}
                  page={PAGE_SIZE}
                  shown={page.items.length}
                  total={page.total}
                  busy={ledger.isPlaceholderData}
                  onChange={setOffset}
                />
              </>
            )}
          </QueryState>
        </div>
      </section>
    </div>
  );
}
