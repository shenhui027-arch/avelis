import type { Answer } from "../../api/hooks";
import { t } from "../../i18n";

export type AskResult = { columns?: string[]; rows?: unknown[][]; row_count?: number; truncated?: boolean;
  column_docs?: Record<string, import("../../components/ui").ColumnDoc> };
export type AskIntent = {
  group_by?: string[];
  metric_code?: string;
  metric_codes?: string[];
  order?: "desc" | "asc" | null;
  limit?: number | null;
  compare?: "previous_period" | "previous_year" | null;
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
  current: t("本期"), previous_period: t("上一期"), previous_year: t("去年同期"),
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

export function changeTone(value: unknown): "ok" | "danger" | "" {
  const parts = decimal(value);
  return parts?.nonzero ? parts.negative ? "danger" : "ok" : "";
}

export function resultCell(value: unknown, column: string, dimension = false): string {
  if (value == null) return "—";
  if (dimension || !/·变化(?:率)?$/.test(column)) return String(value);
  const parts = decimal(value);
  if (!parts) return String(value);
  const sign = parts.nonzero ? parts.negative ? "−" : "+" : "";
  if (column.endsWith("·变化率")) {
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
