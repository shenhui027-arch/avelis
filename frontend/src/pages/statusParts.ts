// 工作台“我负责的指标”的状态构成：纯函数，便于单测。
import type { KitTone } from "../components/ui";

// 口径状态机的全部状态；名单之外的状态归入“其他”，分段之和始终等于指标总数
const STATUS_PARTS: [string, KitTone][] = [["published", "ok"], ["pending_review", "accent"], ["affected", "warn"], ["rejected", "change"],
  ["draft", "flow"], ["retired", "danger"], ["archived", "neutral"]];
/** 我负责的指标按状态分段。 */
export function statusParts(statuses: string[], label: (status: string) => string, other: string): { label: string; tone: KitTone; value: number }[] {
  const known = new Set(STATUS_PARTS.map(([status]) => status));
  return [
    ...STATUS_PARTS.map(([status, tone]) => ({ label: label(status), tone, value: statuses.filter((s) => s === status).length })),
    { label: other, tone: "neutral" as KitTone, value: statuses.filter((s) => !known.has(s)).length },
  ];
}
