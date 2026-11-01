// 通用界面件：全部基于 Avelis UI 的 av-* 类，不引入新的视觉规则。
import type { UseQueryResult } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { labelTable, t, tx } from "../i18n";
import { Icon } from "./Icon";

export { Hint, Icon } from "./Icon";

// --- 状态：状态点 + 两三个字（文字取自语言包 status.*） ----------------------------------

type Tone = "ok" | "warn" | "danger" | "info" | "";

const TONE: Record<string, Tone> = {
  // 指标版本
  draft: "", pending_review: "info", published: "ok", affected: "warn", retired: "danger", archived: "", rejected: "danger",
  // 回答
  answered: "ok", blocked: "danger", clarify: "info", denied: "danger", error: "danger",
  // 变更、复核、任务
  open: "warn", resolved: "ok", queued: "info", running: "info", succeeded: "ok", failed: "danger",
  // 同一张表、粒度、指纹
  confirmed: "ok", candidate: "warn", mismatch: "danger", unresolved: "", verified: "ok", unverified: "warn", violated: "danger",
  stale: "warn", timeout: "warn", ok: "ok", broken: "danger", unknown: "warn",
  // 关联路径、导入
  pending: "info", approved: "ok", needs_params: "warn", needs_review: "warn", done: "ok", parsed: "ok", partial: "warn",
  unsupported: "", skipped: "", model: "warn", written: "ok", accepted: "ok", modified: "info", active: "ok", deleted: "danger",
  waiting: "info", never: "",
};

export function statusOf(status: string | null | undefined): [Tone, string] {
  const s = status ?? "";
  return s in TONE ? [TONE[s], tx(`status.${s}`, s)] : ["", status ?? "—"];
}

export function Status({ value, label }: { value: string | null | undefined; label?: string }) {
  const [tone, text] = statusOf(value);
  return <span className={`av-status${tone ? ` av-status--${tone}` : ""}`}>{label ?? text}</span>;
}

export function Pill({ tone = "", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`av-pill${tone ? ` av-pill--${tone}` : ""}`} title={title}>{children}</span>;
}

export function Dot({ tone = "" }: { tone?: Tone }) {
  return <span className={`av-dot${tone ? ` av-dot--${tone}` : ""}`} />;
}

export function Count({ n }: { n: number }) {
  return <span className="av-count">{n}</span>;
}

const PLAT: Record<string, string> = {
  starrocks: "SR", mysql: "MY", postgres: "PG", clickhouse: "CH", doris: "DR", sqlserver: "MS", oracle: "OR", hive: "HV",
  fake: "FK",
};

export function Plat({ kind }: { kind: string }) {
  return <span className="av-plat">{PLAT[kind] ?? kind.slice(0, 2).toUpperCase()}</span>;
}

export const KIND_LABEL = labelTable("kind", ["table", "view", "materialized_view", "external_table", "catalog_table", "design_time"]);

export const RELATION_LABEL = labelTable("relation",
  ["value_direct", "value_computed", "value_aggregated", "filter", "join_key", "group_by", "table"]);

export const CARD_LABEL: Record<string, string> = {
  n1: "N:1", "11": "1:1", "1n": "1:N", nn: "N:N", get unknown() { return t("cardinality.unknown"); },
};

// --- 控件 -------------------------------------------------------------------------

export function Switch({ checked, onChange, disabled, label }: {
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label: string;
}) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} className="av-switch"
      disabled={disabled} onClick={() => onChange(!checked)} />
  );
}

export function Seg<T extends string>({ value, options, onChange, label }: {
  value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="av-seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void;
}) {
  return (
    <div className="av-tabs" role="tablist">
      {options.map((o) => (
        <button key={o.value} type="button" className="av-tab" role="tab" aria-selected={o.value === value}
          onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Select({ value, onChange, children, disabled, label, style }: {
  value: string; onChange: (v: string) => void; children: ReactNode; disabled?: boolean; label?: string;
  style?: React.CSSProperties;
}) {
  return (
    <span className="av-select-wrap" style={{ width: "100%", ...style }}>
      <select className="av-select" value={value} disabled={disabled} aria-label={label}
        onChange={(e) => onChange(e.target.value)}>{children}</select>
      <Icon name="chevron" small />
    </span>
  );
}

// --- 布局件 -----------------------------------------------------------------------

export function PaneHeader({ title, count, children }: { title: ReactNode; count?: number; children?: ReactNode }) {
  return (
    <div className="av-pane-hd">
      <h2>{title}{count !== undefined ? <Count n={count} /> : null}</h2>
      {children ? <div className="av-actions">{children}</div> : null}
    </div>
  );
}

export function Empty({ text, action, compact, icon }: { text: string; action?: ReactNode; compact?: boolean; icon?: string }) {
  return (
    <div className={`av-empty${compact ? " av-empty--compact" : ""}`} style={{ padding: "var(--space-6)" }}>
      {icon ? <Icon name={icon} /> : null}<span>{text}</span>{action}
    </div>
  );
}

/** 列表加载时的占位行：保持布局，不先显示 0 或空态。 */
export function Skeleton({ rows = 5, height = 44 }: { rows?: number; height?: number }) {
  return (
    <div className="av-skeleton" aria-busy="true" aria-label={t("common.loading")}>
      {Array.from({ length: rows }, (_, i) => <span key={i} style={{ height }} />)}
    </div>
  );
}

/**
 * 一次查询的四种状态：加载（骨架）、失败（原因 + 重试）、成功但为空（空态，可带下一步动作）、有数据。
 * 只有成功后才显示“0 条”或业务空态，避免加载中、失败被误读为“没有数据”。
 */
export function QueryState<T>({ query, isEmpty, emptyText, emptyAction, rows, compact, children }: {
  query: UseQueryResult<T>; isEmpty?: (data: T) => boolean; emptyText?: string; emptyAction?: ReactNode;
  rows?: number; compact?: boolean; children: (data: T) => ReactNode;
}) {
  if (query.isPending) return <Skeleton rows={rows} />;
  if (query.isError) {
    return (
      <div className="av-query-error" role="alert">
        <span className="av-banner av-banner--danger"><Icon name="info" />{query.error instanceof Error ? query.error.message : t("common.loadFailed")}</span>
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => query.refetch()}>
          <Icon name="refresh" />{t("common.retry")}
        </button>
      </div>
    );
  }
  const data = query.data as T;
  if (isEmpty?.(data)) return <Empty text={emptyText ?? t("common.noData")} action={emptyAction} compact={compact} />;
  return <>{children(data)}</>;
}

export function Banner({ message }: { message: { ok: boolean; text: string } | null }) {
  if (!message) return null;
  return <div className={`av-banner ${message.ok ? "av-banner--ok" : "av-banner--danger"}`}>{message.text}</div>;
}

export function Drawer({ title, onClose, children, footer, width }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number;
}) {
  const ref = useRef<HTMLElement>(null);
  // 模态行为：打开时聚焦第一个可操作元素，Tab 在抽屉内循环，Escape 关闭，关闭后把焦点还给触发者
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const focusables = () => Array.from(ref.current?.querySelectorAll<HTMLElement>(
      "input, select, textarea, button, a[href], [tabindex]:not([tabindex='-1'])") ?? []).filter((el) => !el.hasAttribute("disabled"));
    (focusables().find((el) => el.tagName !== "BUTTON") ?? focusables()[0])?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); opener?.focus?.(); };
  }, [onClose]);
  // 挂到 body：玻璃面板的 backdrop-filter 会成为 fixed 子元素的包含块，抽屉不能留在面板里
  return createPortal(
    <div className="av-root">
      <div className="av-backdrop" onClick={onClose} />
      <aside ref={ref} className="av-drawer" role="dialog" aria-modal="true" aria-label={title} style={width ? { width } : undefined}>
        <PaneHeader title={title}>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label={t("common.close")} onClick={onClose}>
            <Icon name="close" />
          </button>
        </PaneHeader>
        <div className="av-drawer__bd">{children}</div>
        {footer ? <div className="av-drawer__ft">{footer}</div> : null}
      </aside>
    </div>,
    document.body,
  );
}

// --- SQL 与差异 --------------------------------------------------------------------

const KEYWORDS = new Set(("SELECT FROM WHERE GROUP BY ORDER HAVING JOIN INNER LEFT RIGHT OUTER ON AS AND OR NOT IN IS " +
  "NULL LIMIT BETWEEN CASE WHEN THEN ELSE END DISTINCT INSERT INTO OVERWRITE CREATE TABLE VIEW TEMPORARY WITH UNION " +
  "ALL USE DROP").split(" "));

export function highlight(line: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(--.*$)|('(?:[^'\\]|\\.)*')|(`[^`]*`)|([A-Za-z_][A-Za-z0-9_]*)(\s*\()?|([^A-Za-z_'`-]+|-)/g;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(line)) !== null) {
    const key = i++;
    if (m[1]) out.push(<span key={key} className="c">{m[1]}</span>);
    else if (m[2]) out.push(<span key={key} className="s">{m[2]}</span>);
    else if (m[3]) out.push(m[3]);
    else if (m[4]) {
      if (m[5]) out.push(<span key={key} className="f">{m[4]}</span>, m[5]);
      else if (KEYWORDS.has(m[4].toUpperCase())) out.push(<span key={key} className="k">{m[4]}</span>);
      else out.push(m[4]);
    } else out.push(m[0]);
  }
  return out;
}

/** 把一行很长的规范化 SQL 按子句断行，便于阅读与比较。 */
export function formatSql(sql: string | null | undefined): string {
  if (!sql) return "";
  return sql
    .replace(/\s+/g, " ")
    .replace(/\s+(FROM|WHERE|GROUP BY|HAVING|ORDER BY|LIMIT|(?:INNER |LEFT |RIGHT )?JOIN)\s+/gi, "\n$1 ")
    .replace(/,\s*(?![^()]*\))/g, ",\n  ")
    .trim();
}

export function Code({ sql, raw }: { sql: string; raw?: boolean }) {
  const text = raw ? sql : formatSql(sql);
  return <pre className="av-code">{text.split("\n").map((line, i) => <span key={i}>{highlight(line)}{"\n"}</span>)}</pre>;
}

/** 两段 SQL 按行比较：删除行用 del，新增行用 add。 */
export function DiffCode({ before, after }: { before: string; after: string }) {
  const a = formatSql(before).split("\n");
  const b = formatSql(after).split("\n");
  const inB = new Set(b);
  const inA = new Set(a);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: "var(--space-3)" }}>
      <pre className="av-code">{a.map((line, i) => <span key={i} className={inB.has(line) ? undefined : "del"}>{highlight(line)}{"\n"}</span>)}</pre>
      <pre className="av-code">{b.map((line, i) => <span key={i} className={inA.has(line) ? undefined : "add"}>{highlight(line)}{"\n"}</span>)}</pre>
    </div>
  );
}

// --- 小工具 -----------------------------------------------------------------------

export function useMessage() {
  return useState<{ ok: boolean; text: string } | null>(null);
}

export function shortName(qualified: string | null | undefined): string {
  if (!qualified) return "";
  const parts = qualified.split(".");
  return parts.length >= 2 ? parts.slice(-2).join(".") : qualified;
}

// --- 字段注释 ----------------------------------------------------------------------

/** 字段含义以数据库列注释为准（后端 governance/column_docs.py）。列名原样展示，注释缺失如实标出，推动数据库侧补齐。 */
export type ColumnDoc = { status: "ok" | "linked" | "inherited" | "missing"; comment?: string | null; source?: string | null };

export function CommentDoc({ doc }: { doc: ColumnDoc | null | undefined }) {
  if (!doc) return null;
  if (doc.status === "ok") return <span className="av-coldoc">{doc.comment}</span>;
  if (doc.status === "linked") {
    return <span className="av-coldoc" title={t("coldoc.fromSource", { source: doc.source })}>{doc.comment}</span>;
  }
  if (doc.status === "inherited") {
    return (
      <span className="av-coldoc av-coldoc--gap" title={t("coldoc.inheritedTitle", { source: doc.source })}>
        <Pill tone="warn">{t("coldoc.missingComment")}</Pill><span>{t("coldoc.upstream", { comment: doc.comment })}</span>
      </span>
    );
  }
  return <span className="av-coldoc av-coldoc--gap" title={t("coldoc.noneTitle")}><Pill tone="warn">{t("coldoc.none")}</Pill></span>;
}
