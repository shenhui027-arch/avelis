// 血缘表格视图：每条列级边一行（表级边、外部表身份映射各一行），可搜索、按关系筛选、点表头排序、分页，导出 CSV。
// 数据与画布同一份 GET /api/lineage/graph；“选中路径”范围用画布当前的追溯结果（pathEdges）。
// 复用约定：搜索框 .av-input、关系下拉 controls.tsx 的 Select、范围 ui.tsx 的 Seg、分页 Pager、空态 Empty、表格 .av-table；
// 表头排序按钮沿用问数结果表的 .av-answer-table__sort 写法（AskPage.tsx ResultTable）；CSV 用 src/csv.ts。不新造控件。
import { useEffect, useMemo, useState } from "react";

import type { Graph } from "../../api/hooks";
import { Select } from "../../components/controls";
import { Icon } from "../../components/Icon";
import { Empty, Pager, RELATION_LABEL, Seg, shortName } from "../../components/ui";
import { downloadText, toCsv } from "../../csv";
import { t } from "../../i18n";
import { edgeRows, filterRows, IDENTITY_RELATION, rowsToMatrix, sortRows, type EdgeRow, type SortKey } from "./trace";

export const PAGE_SIZE = 100;

/** 关系显示文字：RELATION_LABEL 之外补“同一张表” */
export function relationLabel(relation: string): string {
  return relation === IDENTITY_RELATION ? t("同一张表") : RELATION_LABEL[relation] ?? relation;
}

export function EdgeTable({ graph, pathEdges, originLabel, onLocate }: {
  graph: Graph;
  /** 画布上当前选中路径经过的边键；没有选中时为 null（此时范围固定为“全部”，Seg 不显示） */
  pathEdges: ReadonlySet<string> | null;
  /** 来源（解析 / 人工 / 导入解析…）的显示文字，与检查器同一张表：LineagePage 的 ORIGIN */
  originLabel: (origin: string) => string;
  /** 定位到画布：选中该节点（有字段时选中字段）并切回图视图 */
  onLocate: (nodeId: string, column: string | null) => void;
}) {
  const [text, setText] = useState("");
  const [relation, setRelation] = useState("");
  const [scope, setScope] = useState<"all" | "path">(pathEdges ? "path" : "all");
  const [sort, setSort] = useState<{ key: SortKey; dir: "asc" | "desc" } | null>(null);
  const [offset, setOffset] = useState(0);

  const all = useMemo(() => edgeRows(graph), [graph]);
  const rows = useMemo(() => {
    const base = scope === "path" && pathEdges ? edgeRows(graph, pathEdges) : all;
    const filtered = filterRows(base, { text, relation });
    return sort ? sortRows(filtered, sort.key, sort.dir) : filtered;
  }, [all, graph, scope, pathEdges, text, relation, sort]);
  const relations = useMemo(() => [...new Set(all.map((row) => row.relation))], [all]);

  // 筛选、范围、排序或外部路径变化时翻回第一页，防止页码越界显示空白
  useEffect(() => { setOffset(0); }, [text, relation, scope, pathEdges, sort, graph]);

  const exportCsv = () => {
    const matrix = rowsToMatrix(rows, [t("源表"), t("源字段"), t("目标表"), t("目标字段"), t("关系"), t("表达式"), t("来源")],
      { relation: relationLabel, origin: originLabel });
    downloadText(toCsv(matrix), "text/csv;charset=utf-8", "lineage-edges.csv");
  };

  const currentOffset = offset >= rows.length ? 0 : offset;
  const pageRows = rows.slice(currentOffset, currentOffset + PAGE_SIZE);
  const hasFilter = Boolean(text || relation);

  const toggleSort = (key: SortKey) => {
    setSort((prev) => (prev?.key === key ? { key, dir: prev.dir === "asc" ? "desc" : "asc" } : { key, dir: "asc" }));
    setOffset(0);
  };

  const renderSortHead = (key: SortKey, label: string) => {
    const order = sort?.key === key ? (sort.dir === "desc" ? "descending" : "ascending") : "none";
    return (
      <th key={key} scope="col" aria-sort={order}>
        <button type="button" className="av-answer-table__sort" title={t("按此列排序")} onClick={() => toggleSort(key)}>
          <span className="av-answer-table__head">{label}</span>
          {order !== "none" ? <span aria-hidden="true">{order === "descending" ? "↓" : "↑"}</span> : null}
        </button>
      </th>
    );
  };

  return (
    <div className="av-lineage__table">
      <div className="av-lineage__table-bar">
        <input
          className="av-input av-lineage__table-search"
          type="search"
          value={text}
          placeholder={t("搜索表、字段或表达式")}
          aria-label={t("搜索表、字段或表达式")}
          onChange={(e) => { setText(e.target.value); setOffset(0); }}
        />
        <Select
          className="av-lineage__table-select"
          value={relation}
          label={t("关系")}
          onChange={(v) => { setRelation(v); setOffset(0); }}
        >
          <option value="">{t("全部关系")}</option>
          {relations.map((rel) => (
            <option key={rel} value={rel}>{relationLabel(rel)}</option>
          ))}
        </Select>
        {pathEdges ? (
          <Seg
            value={scope}
            label={t("范围")}
            options={[
              { value: "all", label: t("全部") },
              { value: "path", label: t("选中路径") },
            ]}
            onChange={(v) => { setScope(v); setOffset(0); }}
          />
        ) : null}
        <span className="av-caption av-lineage__table-count">{t("{{count}} 条", { count: rows.length })}</span>
        <button
          type="button"
          className="av-btn av-btn--secondary av-btn--sm"
          disabled={rows.length === 0}
          onClick={exportCsv}
        >
          <Icon name="download" small />
          {t("导出 CSV")}
        </button>
      </div>
      {pageRows.length ? (
        <div className="av-table-wrap av-lineage__table-wrap">
          <table className="av-table">
            <thead>
              <tr>
                {renderSortHead("source", t("源表"))}
                {renderSortHead("sourceColumn", t("源字段"))}
                {renderSortHead("target", t("目标表"))}
                {renderSortHead("targetColumn", t("目标字段"))}
                {renderSortHead("relation", t("关系"))}
                <th scope="col">{t("表达式")}</th>
                {renderSortHead("origin", t("来源"))}
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row) => (
                <tr key={row.key}>
                  <td>
                    <button
                      type="button"
                      className="av-lineage__table-link av-mono"
                      title={row.source}
                      onClick={() => onLocate(row.sourceId, null)}
                    >
                      {shortName(row.source)}
                    </button>
                  </td>
                  <td className="av-mono">
                    {row.sourceColumn ? (
                      <button
                        type="button"
                        className="av-lineage__table-link av-mono"
                        onClick={() => onLocate(row.sourceId, row.sourceColumn)}
                      >
                        {row.sourceColumn}
                      </button>
                    ) : <span className="av-muted">—</span>}
                  </td>
                  <td>
                    <button
                      type="button"
                      className="av-lineage__table-link av-mono"
                      title={row.target}
                      onClick={() => onLocate(row.targetId, null)}
                    >
                      {shortName(row.target)}
                    </button>
                  </td>
                  <td className="av-mono">
                    {row.targetColumn ? (
                      <button
                        type="button"
                        className="av-lineage__table-link av-mono"
                        onClick={() => onLocate(row.targetId, row.targetColumn)}
                      >
                        {row.targetColumn}
                      </button>
                    ) : <span className="av-muted">—</span>}
                  </td>
                  <td><span className="av-tag">{relationLabel(row.relation)}</span></td>
                  <td className="av-mono av-lineage__table-expr" title={row.expression ?? undefined}>
                    {row.expression ?? <span className="av-muted">—</span>}
                  </td>
                  <td>{row.origin ? originLabel(row.origin) : <span className="av-muted">—</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <Empty
          compact
          text={t("没有匹配的血缘关系")}
          action={hasFilter ? (
            <button
              type="button"
              className="av-btn av-btn--secondary av-btn--sm"
              onClick={() => { setText(""); setRelation(""); setOffset(0); }}
            >
              {t("清除筛选")}
            </button>
          ) : undefined}
        />
      )}
      <Pager
        offset={currentOffset}
        page={PAGE_SIZE}
        total={rows.length}
        shown={pageRows.length}
        ariaLabel={t("血缘表格分页")}
        onChange={setOffset}
      />
    </div>
  );
}

export type { EdgeRow };
