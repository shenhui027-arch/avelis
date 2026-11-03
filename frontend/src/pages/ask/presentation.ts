import type { Answer } from "../../api/hooks";
import { t } from "../../i18n";
import type { ChartSpec } from "../../components/charts/chartOption";
import { formatValue, type NumberStyle, type UnitMeta } from "../../numbers";

export type ColumnRole = { role: "time" | "dimension" | "measure" | "change" | "rate"; label?: string; grain?: string | null; additive?: boolean | null };
export type Polarity = "up_good" | "down_good";
export type AskResult = { columns?: string[]; rows?: unknown[][]; row_count?: number; truncated?: boolean;
  column_docs?: Record<string, import("../../components/ui").ColumnDoc>;
  // 列契约（ADR 0014）：角色与期望方向来自后端元数据；旧答案没有时退回按列名后缀判断
  column_roles?: Record<string, ColumnRole>; polarity?: Record<string, Polarity>; units?: Record<string, UnitMeta>;
  // 图表规格（后端 charts.recommend）：第一个为默认图，其余为同一份数据的候选
  charts?: ChartSpec[] };

/** 列的角色：有列契约时只读契约（契约里没有的列不猜）；没有契约的旧答案按“·变化 / ·变化率”后缀判断。 */
export function columnRole(result: AskResult, column: string): ColumnRole["role"] | undefined {
  if (result.column_roles) return result.column_roles[column]?.role;
  return column.endsWith("·变化率") ? "rate" : column.endsWith("·变化") ? "change" : undefined;
}
/** 列标题：时间、维度用契约里的显示名；单指标的值列（value）用指标名；其余度量列名本身已可读（如“<指标>·上一期”）。 */
export function columnLabel(result: AskResult, column: string): string {
  const role = result.column_roles?.[column];
  if (role?.label && (role.role === "time" || role.role === "dimension" || column === "value")) return role.label;
  return column === "value" ? t("值") : column;
}

/** 单元格文字：有量纲时按量纲格式化（与报告正文一致）；旧答案没有量纲时沿用原规则。 */
export function cellText(result: AskResult, column: string, value: unknown, dimension: boolean, style: NumberStyle): string {
  const role = columnRole(result, column);
  if (dimension || role === "time" || role === "dimension") return value == null ? "—" : String(value);
  const unit = result.units?.[column];
  return unit ? formatValue(value, unit, style) : resultCell(value, column, dimension, role);
}

export type AskIntent = {
  group_by?: string[];
  metric_code?: string;
  metric_codes?: string[];
  order?: "desc" | "asc" | null;
  limit?: number | null;
  compare?: "previous_period" | "previous_year" | "previous_calendar" | "custom" | null;
};
export type PlanQuery = {
  metric_code: string;
  metric_version_id?: string;
  period: string;
  range?: string[];
  status?: string;
  cache_hit?: boolean;
};
export type DerivedMetric = {
  metric_code: string;
  formula: string;
  components: {
    metric_id?: string;
    metric_code: string;
    metric_name: string;
    version_id: string;
    version_no: number;
  }[];
};

export const PERIOD_LABEL: Record<string, string> = {
  current: t("本期"), previous_period: t("上一期"), previous_year: t("去年同期"), previous_calendar: t("上一自然期"),
};

export function planQueries(answer: Answer): PlanQuery[] {
  // 查询被阻断时，后端只在 evidence.plan 留下已经尝试过的子查询。
  const queries = answer.plan?.kind === "plan" ? answer.plan.subqueries : answer.evidence?.plan;
  return Array.isArray(queries) ? queries as PlanQuery[] : [];
}

export function dateRange(range: string[] | undefined): string {
  return range?.length ? range.join(" ~ ") : "—";
}

function decimal(value: unknown) {
  if (value == null) return null;
  const match = /^([+-]?)(\d+)(?:\.(\d*))?$/.exec(String(value));
  if (!match) return null;
  const [, sign, whole, fraction = ""] = match;
  return { negative: sign === "-", whole, fraction, nonzero: /[1-9]/.test(whole + fraction) };
}

/** 变化的好坏只看指标的期望方向：越高越好时上升为好，越低越好时下降为好；方向未知时不着色。 */
export function changeTone(value: unknown, polarity?: Polarity): "ok" | "danger" | "" {
  const parts = decimal(value);
  if (!parts?.nonzero || !polarity) return "";
  return parts.negative === (polarity === "up_good") ? "danger" : "ok";
}

export function resultCell(value: unknown, column: string, dimension = false,
                           role: ColumnRole["role"] | undefined = columnRole({}, column)): string {
  if (value == null) return "—";
  if (dimension || (role !== "change" && role !== "rate")) return String(value);
  const parts = decimal(value);
  if (!parts) return String(value);
  const sign = parts.nonzero ? parts.negative ? "−" : "+" : "";
  if (role === "rate") {
    // 直接移动十进制位并四舍五入，避免大数或 1.005 一类浮点数误差。
    const digits = parts.fraction.padEnd(4, "0");
    let tenths = BigInt(parts.whole) * 1000n + BigInt(digits.slice(0, 3));
    if (digits[3] >= "5") tenths += 1n;
    return `${sign}${tenths / 10n}.${tenths % 10n}%`;
  }
  return `${sign}${parts.whole}${parts.fraction ? `.${parts.fraction}` : ""}`;
}

export function sqlSections(sql: string): { title?: string; sql: string }[] {
  const sections: { title?: string; sql: string }[] = [];
  let title: string | undefined;
  let lines: string[] = [];
  const flush = () => {
    const body = lines.join("\n").trim();
    if (body || title) sections.push({ title, sql: body });
    lines = [];
  };
  for (const line of sql.split(/\r?\n/)) {
    const header = /^--\s+(.+ V\d+\s+·\s+(?:本期|上一期|去年同期)\s+\d{4}-\d{2}-\d{2}\s+~\s+\d{4}-\d{2}-\d{2})\s*$/.exec(line);
    if (header) {
      flush();
      title = header[1];
    } else {
      lines.push(line);
    }
  }
  flush();
  return sections;
}
