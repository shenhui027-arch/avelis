// 筛选取值对齐说明（ADR 0021）：问数答案与分析运行共用。
import type { Schemas } from "../api/client";
import { Icon } from "./Icon";
import { t } from "../i18n";

type FilterAlignment = Schemas["FilterAlignmentOut"];

/** 筛选取值被对齐时的说明：原话与实际筛选值不同才显示；按业务维表对上的注明维表与列（ADR 0021）。 */
export function FilterAlignmentNote({ items }: { items?: FilterAlignment[] | null }) {
  const changed = (items ?? []).filter((i) => i.method !== "exact" && i.value !== i.original);
  if (!changed.length) return null;
  return <p className="av-caption av-filter-alignment">
    <Icon name="info" small />
    {changed.map((i, n) => <span key={n}>
      {t("按“{{value}}”筛选（原话“{{original}}”）", { value: i.value, original: i.original })}
      {i.method === "dimension_table" && i.table ? t("，依据维表 {{table}} 的 {{column}} 列", { table: i.table, column: i.column ?? "" }) : ""}
      {i.method === "confirmed" ? t("，由你确认") : ""}
    </span>)}
  </p>;
}
