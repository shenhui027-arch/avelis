// 通用界面件：全部基于 Avelis UI 的 av-* 类，不引入新的视觉规则。
import { useEffect, useState, type ReactNode } from "react";

import { Icon } from "./Icon";

export { Hint, Icon } from "./Icon";

// --- 状态：状态点 + 两三个字 ---------------------------------------------------------

type Tone = "ok" | "warn" | "danger" | "info" | "";

const STATUS: Record<string, [Tone, string]> = {
  // 指标版本
  draft: ["", "草稿"], pending_review: ["info", "待复核"], published: ["ok", "已发布"], affected: ["warn", "受影响"],
  retired: ["danger", "失效"], archived: ["", "归档"], rejected: ["danger", "已驳回"],
  // 回答
  answered: ["ok", "已回答"], blocked: ["danger", "已拦截"], clarify: ["info", "待澄清"], denied: ["danger", "已拒绝"],
  error: ["danger", "出错"],
  // 变更、复核、任务
  open: ["warn", "待处理"], resolved: ["ok", "已处理"], queued: ["info", "排队中"], running: ["info", "进行中"],
  succeeded: ["ok", "成功"], failed: ["danger", "失败"],
  // 同一张表、粒度、指纹
  confirmed: ["ok", "同一张表"], candidate: ["warn", "待确认"], mismatch: ["danger", "列不一致"],
  unresolved: ["", "未匹配"], verified: ["ok", "已核验"], unverified: ["warn", "未核验"], violated: ["danger", "不唯一"],
  stale: ["warn", "需重新声明"], timeout: ["warn", "核验超时"], ok: ["ok", "正常"], broken: ["danger", "上游缺失"],
  unknown: ["warn", "证据不全"],
  // 关联路径、导入
  pending: ["info", "待批准"], approved: ["ok", "已批准"], needs_params: ["warn", "待补参数"],
  needs_review: ["warn", "待确认"], done: ["ok", "完成"], parsed: ["ok", "已解析"], partial: ["warn", "部分解析"],
  unsupported: ["", "不支持"], skipped: ["", "跳过"], model: ["warn", "模型候选"], written: ["ok", "已写入"],
  accepted: ["ok", "已接受"], modified: ["info", "已修改"], active: ["ok", "有效"], deleted: ["danger", "已删除"],
  waiting: ["info", "待回答"], never: ["", "未采集"],
};

export function statusOf(status: string | null | undefined): [Tone, string] {
  return STATUS[status ?? ""] ?? ["", status ?? "—"];
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

const PLAT: Record<string, string> = { starrocks: "SR", mysql: "MY", fake: "FK", hive: "HV" };

export function Plat({ kind }: { kind: string }) {
  return <span className="av-plat">{PLAT[kind] ?? kind.slice(0, 2).toUpperCase()}</span>;
}

export const KIND_LABEL: Record<string, string> = {
  table: "表", view: "视图", materialized_view: "物化视图", external_table: "外部表", catalog_table: "Catalog 表",
  design_time: "设计时",
};

export const RELATION_LABEL: Record<string, string> = {
  value_direct: "直接", value_computed: "计算", value_aggregated: "聚合", filter: "过滤", join_key: "关联键",
  group_by: "分组", table: "表级",
};

export const CARD_LABEL: Record<string, string> = { n1: "N:1", "11": "1:1", "1n": "1:N", nn: "N:N", unknown: "未知" };

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

export function Empty({ text, action }: { text: string; action?: ReactNode }) {
  return <div className="av-empty" style={{ padding: "var(--space-6)" }}><span>{text}</span>{action}</div>;
}

export function Banner({ message }: { message: { ok: boolean; text: string } | null }) {
  if (!message) return null;
  return <div className={`av-banner ${message.ok ? "av-banner--ok" : "av-banner--danger"}`}>{message.text}</div>;
}

export function Drawer({ title, onClose, children, footer, width }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="av-backdrop" onClick={onClose} />
      <aside className="av-drawer" role="dialog" aria-label={title} style={width ? { width } : undefined}>
        <PaneHeader title={title}>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label="关闭" onClick={onClose}>
            <Icon name="close" />
          </button>
        </PaneHeader>
        <div className="av-drawer__bd">{children}</div>
        {footer ? <div className="av-drawer__ft">{footer}</div> : null}
      </aside>
    </>
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
