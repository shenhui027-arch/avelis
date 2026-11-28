// 血缘体检（血缘页检查器内的折叠区）：确定性规则的发现（字段已不存在、来源未采集、定义未解析、待确认 / 低置信候选）
// 与可选的模型复核结论。数据：GET /api/lineage/health?asset_id=、POST /api/lineage/health/review。只读，不改任何记录。
// 复用约定：折叠区 .av-fold（与检查器里“影响条件”一致）、Pill / Status / Empty / QueryState（components/ui.tsx）、
// 列表行沿用检查器的 .av-lineage__flow-item / .av-chain__item 写法、提示 useMessage + Banner。不新造控件。
import { useMutation, useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../../api/client";
import type { components } from "../../api/schema";
import { t } from "../../i18n";

export type HealthFinding = components["schemas"]["HealthFindingOut"];
export type HealthReview = components["schemas"]["LineageHealthReviewOut"];

/** 发现的严重程度 → Pill 色调 */
export const SEVERITY_TONE = { danger: "danger", warn: "warn", info: "info" } as const;

/**
 * 一条发现的说明文字（已翻译）。按 code 取 detail 里的参数：
 * - missing_column：“字段 {{column}} 已不存在，仍被 {{count}} 条血缘边引用”（count = detail.edges）
 * - uncollected_source：“来源 {{subject}} 未采集，{{target}} 依赖它”
 * - definition_unparsed：“定义未完整解析（{{status}}），血缘可能缺失”；有 detail.reason 时另起一行附原因
 * - pending_candidates：“有 {{count}} 条模型候选待确认”
 * - low_confidence：“其中 {{count}} 条置信度低于 60%，最低 {{min}}”（min 显示为百分比，取整）
 */
export function findingText(finding: HealthFinding): string {
  // TODO(agy): 按上面的规则实现（每种文案一个字面量 t() 调用，参数用 {{…}} 插值；带 count 的英文要有 _one / _other）
  void finding;
  return "";
}

export function HealthPanel({ assetId, onPickAsset }: {
  assetId: string;
  /** 点击发现里的资产名时在画布上定位（asset_id 为空的发现不可点击） */
  onPickAsset: (assetId: string) => void;
}) {
  const health = useQuery({
    queryKey: ["lineage-health", assetId],
    queryFn: ({ signal }) => unwrap(api.GET("/api/lineage/health", { params: { query: { asset_id: assetId } }, signal })),
  });
  const review = useMutation({
    mutationFn: () => unwrap(api.POST("/api/lineage/health/review", { body: { asset_id: assetId } })),
  });

  // TODO(agy): 渲染，结构如下：
  //  <details className="av-fold av-lineage__health" open={有 danger 或 warn 发现}>
  //    <summary>“血缘体检” + 计数：有发现时显示 Pill（最高严重程度的色调）“{{count}} 项”；无发现时 Status 风格的“正常”</summary>
  //    QueryState 包住 health：加载中骨架、出错可重试；
  //    findings 为空：Empty compact “没有发现问题”；
  //    否则逐条：Pill（色调 SEVERITY_TONE，文字用 code 的简短名称：字段已不存在 / 来源未采集 / 定义未解析 / 待确认候选 / 低置信候选）
  //      + subject（.av-mono，asset_id 非空时是按钮，点击 onPickAsset(asset_id)）+ findingText（.av-caption）；
  //    truncated 时末尾 .av-caption “只显示前 200 项”；
  //    底部“AI 复核”按钮（.av-btn--secondary .av-btn--sm，review.isPending 时禁用并显示“复核中…”）：
  //      成功且 skipped === "policy" → Banner 信息“已关闭向模型发送代码，不能复核”；skipped === "no_model" → “没有可用的模型，请先在设置里配置”；
  //      否则显示结论 Pill：ok →（ok 色）“未发现问题”、suspect →（warn 色）“发现疑似问题”、unknown →（默认色）“无法判定”，
  //      其后 summary 一段，items 逐条（subject .av-mono + note）；并在下方 .av-caption 注明“结论由模型生成，仅供参考，请人工核对”；
  //      失败 → Banner 显示接口返回的 detail。
  //  </details>
  void health; void review; void onPickAsset;
  return null;
}
