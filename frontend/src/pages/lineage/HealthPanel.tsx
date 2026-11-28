// 血缘体检（血缘页检查器内的折叠区）：确定性规则的发现（字段已不存在、来源未采集、定义未解析、待确认 / 低置信候选）
// 与可选的模型复核结论。数据：GET /api/lineage/health?asset_id=、POST /api/lineage/health/review。只读，不改任何记录。
// 复用约定：折叠区 .av-fold（与检查器里“影响条件”一致）、Pill / Status / Empty / QueryState（components/ui.tsx）、
// 列表行沿用检查器的 .av-lineage__flow-item / .av-chain__item 写法、提示 useMessage + Banner。不新造控件。
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, unwrap } from "../../api/client";
import { errorText } from "../../api/hooks";
import type { components } from "../../api/schema";
import { Banner, Empty, Pill, QueryState, Status, statusOf, useMessage, type Tone } from "../../components/ui";
import { t } from "../../i18n";

export type HealthFinding = components["schemas"]["HealthFindingOut"];
export type HealthReview = components["schemas"]["LineageHealthReviewOut"];

/** 发现的严重程度 → Pill 色调 */
export const SEVERITY_TONE = { danger: "danger", warn: "warn", info: "info" } as const;

/** 发现代码的简短名称 */
function findingLabel(code: HealthFinding["code"]): string {
  switch (code) {
    case "missing_column": return t("字段已不存在");
    case "uncollected_source": return t("来源未采集");
    case "definition_unparsed": return t("定义未解析");
    case "pending_candidates": return t("待确认候选");
    case "low_confidence": return t("低置信候选");
  }
}

/** 复核结论 → Pill 色调与文案 */
function verdictMeta(verdict: HealthReview["verdict"]): [Tone, string] {
  switch (verdict) {
    case "ok": return ["ok", t("未发现问题")];
    case "suspect": return ["warn", t("发现疑似问题")];
    default: return ["", t("无法判定")];
  }
}

function reviewError(err: unknown): string {
  if (err && typeof err === "object" && "detail" in err && typeof (err as { detail?: unknown }).detail === "string") {
    return (err as { detail: string }).detail;
  }
  return errorText(err);
}

/**
 * 一条发现的说明文字（已翻译）。按 code 取 detail 里的参数：
 * - missing_column：“字段 {{column}} 已不存在，仍被 {{count}} 条血缘边引用”（count = detail.edges）
 * - uncollected_source：“来源 {{subject}} 未采集，{{target}} 依赖它”
 * - definition_unparsed：“定义未完整解析（{{status}}），血缘可能缺失”；有 detail.reason 时另起一行附原因
 * - pending_candidates：“有 {{count}} 条模型候选待确认”
 * - low_confidence：“其中 {{count}} 条置信度低于 60%，最低 {{min}}”（min 显示为百分比，取整）
 */
export function findingText(finding: HealthFinding): string {
  const d = finding.detail;
  switch (finding.code) {
    case "missing_column":
      return t("字段 {{column}} 已不存在，仍被 {{count}} 条血缘边引用", { column: String(d.column ?? ""), count: Number(d.edges ?? 1) });
    case "uncollected_source":
      return t("来源 {{subject}} 未采集，{{target}} 依赖它", { subject: finding.subject, target: String(d.target ?? "") });
    case "definition_unparsed": {
      const status = typeof d.status === "string" && d.status ? statusOf(d.status)[1] : "";
      const head = t("定义未完整解析（{{status}}），血缘可能缺失", { status });
      const reason = typeof d.reason === "string" ? d.reason.trim() : "";
      return reason ? `${head}\n${reason}` : head;
    }
    case "pending_candidates":
      return t("有 {{count}} 条模型候选待确认", { count: Number(d.count ?? 0) });
    case "low_confidence": {
      const min = typeof d.min === "string" && d.min.endsWith("%") ? d.min : `${Math.round(Number(d.min ?? 0) * 100)}%`;
      return t("其中 {{count}} 条置信度低于 60%，最低 {{min}}", { count: Number(d.count ?? 0), min });
    }
    default:
      return "";
  }
}

export function HealthPanel({ assetId, onPickAsset }: {
  assetId: string;
  /** 点击发现里的资产名时在画布上定位（asset_id 为空的发现不可点击） */
  onPickAsset: (assetId: string) => void;
}) {
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const [message, setMessage] = useMessage();
  const health = useQuery({
    queryKey: ["lineage-health", assetId],
    queryFn: ({ signal }) => unwrap(api.GET("/api/lineage/health", { params: { query: { asset_id: assetId } }, signal })),
  });
  const review = useMutation({
    mutationFn: () => unwrap(api.POST("/api/lineage/health/review", { body: { asset_id: assetId } })),
  });
  const resetReviewRef = useRef(review.reset);
  resetReviewRef.current = review.reset;
  const prevAssetRef = useRef(assetId);
  useEffect(() => {
    if (prevAssetRef.current === assetId) return;
    prevAssetRef.current = assetId;
    setUserOpen(null);
    setMessage(null);
    resetReviewRef.current();
  }, [assetId, setMessage]);

  const findings = health.data?.findings ?? [];
  const hasAlert = findings.some((f) => f.severity === "danger" || f.severity === "warn");
  const topTone = findings.some((f) => f.severity === "danger")
    ? SEVERITY_TONE.danger
    : findings.some((f) => f.severity === "warn")
      ? SEVERITY_TONE.warn
      : SEVERITY_TONE.info;
  const summaryTotal = Object.values(health.data?.summary ?? {}).reduce((sum, n) => sum + n, 0);
  const totalCount = Math.max(summaryTotal, findings.length);

  const runReview = () => {
    setMessage(null);
    review.mutate(undefined, {
      onSuccess: (res) => {
        if (res.skipped === "policy") setMessage({ ok: true, text: t("已关闭向模型发送代码，不能复核") });
        else if (res.skipped === "no_model") setMessage({ ok: true, text: t("没有可用的模型，请先在设置里配置") });
        else setMessage(null);
      },
      onError: (err) => setMessage({ ok: false, text: reviewError(err) }),
    });
  };

  const bannerMessage = review.isPending
    ? null
    : message
      ? (message.ok ? { ...message, tone: "info" as const } : message)
      : review.isError
        ? { ok: false, text: reviewError(review.error) }
        : review.data?.skipped === "policy"
          ? { ok: true, tone: "info" as const, text: t("已关闭向模型发送代码，不能复核") }
          : review.data?.skipped === "no_model"
            ? { ok: true, tone: "info" as const, text: t("没有可用的模型，请先在设置里配置") }
            : null;

  const reviewResult = !review.isPending && review.data && !review.data.skipped ? review.data : null;
  const [verdictTone, verdictLabel] = verdictMeta(reviewResult?.verdict);

  return (
    <details
      className="av-fold av-lineage__health"
      open={userOpen ?? hasAlert}
      onToggle={(e) => {
        if (e.target === e.currentTarget) setUserOpen(e.currentTarget.open);
      }}
    >
      <summary>
        <span>{t("血缘体检")}</span>
        {health.data ? (
          findings.length ? <Pill tone={topTone}>{t("{{count}} 项", { count: totalCount })}</Pill> : <Status value="ok" label={t("正常")} />
        ) : null}
      </summary>
      <QueryState query={health} rows={2}>
        {(data) => (
          <div className="av-lineage__health-body">
            {data.findings.length ? (
              <div className="av-lineage__stack">
                {data.findings.map((f, i) => (
                  <div key={`${f.code}-${f.subject}-${i}`} className="av-lineage__flow-item">
                    <span className="av-chain__item">
                      <Pill tone={SEVERITY_TONE[f.severity]}>{findingLabel(f.code)}</Pill>
                      {f.asset_id ? (
                        <button
                          type="button"
                          className="av-mono av-lineage__health-link"
                          title={t("定位到 {{v1}}", { v1: f.subject })}
                          onClick={() => onPickAsset(f.asset_id!)}
                        >
                          {f.subject}
                        </button>
                      ) : (
                        <span className="av-mono av-lineage__health-subject">{f.subject}</span>
                      )}
                    </span>
                    <span className="av-caption av-lineage__health-desc">{findingText(f)}</span>
                  </div>
                ))}
                {data.truncated ? <span className="av-caption">{t("只显示前 200 项")}</span> : null}
              </div>
            ) : (
              <Empty compact text={t("没有发现问题")} />
            )}
            <div className="av-lineage__health-review">
              <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={review.isPending} onClick={runReview}>
                {review.isPending ? t("复核中…") : t("AI 复核")}
              </button>
              <Banner message={bannerMessage} />
              {reviewResult ? (
                <>
                  <Pill tone={verdictTone}>{verdictLabel}</Pill>
                  {reviewResult.summary ? <p className="av-lineage__health-summary">{reviewResult.summary}</p> : null}
                  {reviewResult.items?.length ? (
                    <div className="av-lineage__stack av-lineage__health-items">
                      {reviewResult.items.map((item, i) => (
                        <div key={`${item.code}-${item.subject}-${i}`} className="av-lineage__flow-item">
                          <span className="av-chain__item">
                            <span className="av-mono av-lineage__health-subject">{item.subject}</span>
                          </span>
                          <span className="av-caption">{item.note}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}
                  <span className="av-caption">{t("结论由模型生成，仅供参考，请人工核对")}</span>
                </>
              ) : null}
            </div>
          </div>
        )}
      </QueryState>
    </details>
  );
}
