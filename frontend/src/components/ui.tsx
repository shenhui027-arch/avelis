// 通用界面件：全部基于 Avelis UI 的 av-* 类，不引入新的视觉规则。
import type { UseQueryResult } from "@tanstack/react-query";
import { motion } from "motion/react";
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router";

import { t } from "../i18n";
import { Icon } from "./Icon";

export { Hint, Icon } from "./Icon";
export { ConfirmDialog, DateInput, Select, SuggestInput } from "./controls";

// --- 状态：状态点 + 两三个字（文字取自语言包 status.*） ----------------------------------

type Tone = "ok" | "warn" | "danger" | "info" | "";

const STATUS: Record<string, [Tone, string]> = {
  // 指标版本
  draft: ["", t("草稿")], pending_review: ["info", t("待复核")], published: ["ok", t("已发布")], affected: ["warn", t("受影响")],
  retired: ["danger", t("失效")], archived: ["", t("归档")], rejected: ["danger", t("已驳回")],
  // 回答
  answered: ["ok", t("已回答")], blocked: ["danger", t("已拦截")], clarify: ["info", t("待澄清")], denied: ["danger", t("已拒绝")],
  error: ["danger", t("出错")],
  // 变更、复核、任务
  open: ["warn", t("待处理")], resolved: ["ok", t("已处理")], queued: ["info", t("排队中")], running: ["info", t("进行中")],
  succeeded: ["ok", t("成功")], failed: ["danger", t("失败")],
  // 同一张表、粒度、指纹
  confirmed: ["ok", t("同一张表")], candidate: ["warn", t("待确认")], mismatch: ["danger", t("列不一致")],
  unresolved: ["", t("未匹配")], verified: ["ok", t("已核验")], unverified: ["warn", t("未核验")], violated: ["danger", t("不唯一")],
  stale: ["warn", t("需重新声明")], timeout: ["warn", t("核验超时")], ok: ["ok", t("正常")], broken: ["danger", t("上游缺失")],
  unknown: ["warn", t("证据不全")],
  // 关联路径、导入
  pending: ["info", t("待批准")], approved: ["ok", t("已批准")], needs_params: ["warn", t("待补参数")],
  needs_review: ["warn", t("待确认")], done: ["ok", t("完成")], parsed: ["ok", t("已解析")], partial: ["warn", t("部分解析")],
  unsupported: ["", t("不支持")], skipped: ["", t("跳过")], model: ["warn", t("模型候选")], written: ["ok", t("已写入")],
  accepted: ["ok", t("已接受")], modified: ["info", t("已修改")], active: ["ok", t("有效")], deleted: ["danger", t("已删除")],
  waiting: ["info", t("待回答")], never: ["", t("未采集")], superseded: ["", t("已替换")], intermediate: ["", t("中间段")],
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

/** 按 offset 翻页：列表按 page + 1 条取数，多出的那条只用来判断有没有下一页（没有总数也能翻）。 */
export function Pager({ offset, shown, page, hasNext, busy, onChange }: { offset: number; shown: number; page: number; hasNext: boolean;
  /** 新一页还在加载（仍显示上一页占位）：暂停向后翻，避免连点越过末页 */
  busy?: boolean; onChange: (offset: number) => void }) {
  if (offset === 0 && !hasNext) return null;
  return <div className="av-actions av-webhooks__pager">
    <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - page))}>{t("上一页")}</button>
    <span className="av-caption av-mono">{shown ? `${offset + 1}–${offset + shown}` : "—"}</span>
    <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={!hasNext || busy} onClick={() => onChange(offset + page)}>{t("下一页")}</button>
  </div>;
}

const PLAT: Record<string, string> = {
  starrocks: "StarRocks", mysql: "MySQL", postgres: "PostgreSQL", clickhouse: "ClickHouse", doris: "Doris", sqlserver: "SQL Server",
  oracle: "Oracle", hive: "Hive", fake: t("测试适配器"),
};

/** 引擎名：显示全称（与后端引擎登记的 label 一致），未知类型原样显示。 */
export function Plat({ kind }: { kind: string }) {
  return <span className="av-plat">{PLAT[kind] ?? kind}</span>;
}

export const KIND_LABEL: Record<string, string> = {
  table: t("表"), view: t("视图"), materialized_view: t("物化视图"), external_table: t("外部表"), catalog_table: t("Catalog 表"),
  design_time: t("设计时"),
};

export const RELATION_LABEL: Record<string, string> = {
  value_direct: t("直接"), value_computed: t("计算"), value_aggregated: t("聚合"), filter: t("过滤"), join_key: t("关联键"),
  group_by: t("分组"), table: t("表级"),
};

export const CARD_LABEL: Record<string, string> = { n1: "N:1", "11": "1:1", "1n": "1:N", nn: "N:N", unknown: t("未知") };

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
  const id = useId();
  return (
    <div className="av-seg" role="group" aria-label={label}>
      {options.map((o) => (
        <button key={o.value} type="button" aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
          {o.value === value ? <motion.span layoutId={`seg${id}`} className="av-seg__pill" aria-hidden /> : null}
          <span className="av-seg__label">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

export function Tabs<T extends string>({ value, options, onChange }: {
  value: T; options: { value: T; label: ReactNode }[]; onChange: (v: T) => void;
}) {
  const id = useId();
  return (
    <div className="av-tabs" role="tablist">
      {options.map((o) => (
        <button key={o.value} type="button" className="av-tab" role="tab" aria-selected={o.value === value}
          onClick={() => onChange(o.value)}>{o.label}
          {o.value === value ? <motion.span layoutId={`tab${id}`} className="av-tab__bar" aria-hidden /> : null}</button>
      ))}
    </div>
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

/**
 * 详情页顶部的层级：第一项是返回上一级的链接，其余是所在位置，只读。
 * 用在“列表 → 详情”的二级页，不用再点侧栏栏目才能退回列表。
 */
export function Crumbs({ back, items = [] }: { back: { label: string; to: string }; items?: ReactNode[] }) {
  return (
    <nav className="av-crumbs" aria-label={t("位置")}>
      <Link className="av-crumbs__back" to={back.to}><Icon name="back" small />{back.label}</Link>
      {items.map((item, i) => <span key={i} className="av-crumbs__item">{item}</span>)}
    </nav>
  );
}

/** 空态：三块叠放的图标块（中间是本处的图标）+ 一句话 + 一个主操作。 */
export function Empty({ text, action, compact, icon }: { text: string; action?: ReactNode; compact?: boolean; icon?: string }) {
  return (
    <div className={`av-empty${compact ? " av-empty--compact" : ""}`}>
      <span className="av-empty__art" aria-hidden>
        <Tile icon="table" /><Tile icon={icon ?? "search"} /><Tile icon="chart" />
      </span>
      <span className="av-empty__text">{text}</span>{action}
    </div>
  );
}

// --- 套件件（styles/kit.css）：图标块、进度轨、分段比例条、来源角标 --------------------------------------

export type KitTone = "neutral" | "accent" | "ok" | "warn" | "danger" | "flow" | "collect" | "ask" | "lineage" | "catalog" | "change";

/** 图标块：统计卡、空态、审计流、确认卡、表格行首共用一种形状。 */
export function Tile({ icon, tone = "neutral", size }: { icon: string; tone?: KitTone; size?: "sm" | "lg" }) {
  return <span className={`av-tile tone-${tone}${size ? ` av-tile--${size}` : ""}`} aria-hidden><Icon name={icon} /></span>;
}

/** 进度轨：value / max，超过 60% 提醒、85% 警示（可用 tone 覆盖）。 */
export function Meter({ value, max, tone, label }: { value: number; max: number; tone?: KitTone; label: string }) {
  const ratio = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
  const auto: KitTone = ratio > 0.85 ? "danger" : ratio > 0.6 ? "warn" : "accent";
  return <span className={`av-meter tone-${tone ?? auto}`} role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value}>
    <i style={{ width: `${ratio * 100}%` }} /></span>;
}

/** 分段比例条 + 图例：每段一种语气，数量为 0 的段不画。 */
export function Parts({ parts, label }: { parts: { label: string; value: number; tone: KitTone }[]; label: string }) {
  const shown = parts.filter((p) => p.value > 0);
  return <div className="av-parts-block">
    <div className="av-parts" role="img" aria-label={label}>
      {shown.map((p) => <i key={p.label} className={`tone-${p.tone}`} style={{ flexGrow: p.value }} />)}
    </div>
    <div className="av-legend">{shown.map((p) => <span key={p.label} className={`tone-${p.tone}`}>{p.label}<b>{p.value}</b></span>)}</div>
  </div>;
}

/** 列表加载时的占位行：保持布局，不先显示 0 或空态。 */
export function Skeleton({ rows = 5, height = 44 }: { rows?: number; height?: number }) {
  return (
    <div className="av-skeleton" aria-busy="true" aria-label={t("加载中")}>
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
        <span className="av-banner av-banner--danger"><Icon name="info" />{query.error instanceof Error ? query.error.message : t("加载失败")}</span>
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => query.refetch()}>
          <Icon name="refresh" />{t("重试")}
        </button>
      </div>
    );
  }
  const data = query.data as T;
  if (isEmpty?.(data)) return <Empty text={emptyText ?? t("暂无数据")} action={emptyAction} compact={compact} />;
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
  // Esc、点遮罩、✕ 先播放滑出，播完再通知调用方卸载；调用方直接卸载（如底栏“取消”）时立即消失
  const [closing, setClosing] = useState(false);
  const close = useRef(onClose);
  close.current = onClose;
  const done = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  // 调用方可以拒绝关闭（如保存进行中时 onClose 不卸载）：仍挂着就恢复显示，下次还能再关
  const finish = () => {
    if (done.current) return;
    done.current = true;
    close.current();
    window.setTimeout(() => { if (mounted.current) { done.current = false; setClosing(false); } }, 0);
  };
  const requestClose = () => setClosing(true);
  // 动画帧被暂停（后台标签页等）时也要能关上：滑出时长之后兜底收尾
  useEffect(() => {
    if (!closing) return;
    const timer = window.setTimeout(finish, 320);
    return () => window.clearTimeout(timer);
  }, [closing]);
  // 模态行为：打开时聚焦第一个可操作元素，Tab 在抽屉内循环，Escape 关闭，关闭后把焦点还给触发者
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const focusables = () => Array.from(ref.current?.querySelectorAll<HTMLElement>(
      "input, select, textarea, button, a[href], [tabindex]:not([tabindex='-1'])") ?? []).filter((el) => !el.hasAttribute("disabled"));
    (focusables().find((el) => el.tagName !== "BUTTON") ?? focusables()[0])?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setClosing(true);
      if (e.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); opener?.focus?.(); };
  }, []);
  // 挂到 body：面板的 overflow 与层叠上下文不能裁掉抽屉
  return createPortal(
    <div className="av-root">
      <motion.div className="av-backdrop" data-motion onClick={requestClose}
        initial={{ opacity: 0 }} animate={{ opacity: closing ? 0 : 1 }} transition={{ duration: closing ? 0.18 : 0.24 }} />
      <motion.aside ref={ref} className="av-drawer" data-motion role="dialog" aria-modal="true" aria-label={title} style={width ? { width } : undefined}
        initial={{ opacity: 0, x: 24 }} animate={closing ? { opacity: 0, x: 24 } : { opacity: 1, x: 0 }}
        transition={{ duration: closing ? 0.2 : 0.36 }}
        onAnimationComplete={() => { if (closing) finish(); }}>
        <PaneHeader title={title}>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label={t("关闭")} onClick={requestClose}>
            <Icon name="close" />
          </button>
        </PaneHeader>
        <div className="av-drawer__bd">{children}</div>
        {footer ? <div className="av-drawer__ft">{footer}</div> : null}
      </motion.aside>
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
    return <span className="av-coldoc" title={t("取自源表 {{source}}", { source: doc.source })}>{doc.comment}</span>;
  }
  if (doc.status === "inherited") {
    return (
      <span className="av-coldoc av-coldoc--gap" title={t("本列没有数据库注释；上游 {{source}} 的注释可沿用", { source: doc.source })}>
        <Pill tone="warn">{t("缺注释")}</Pill><span>{t("上游：{{comment}}", { comment: doc.comment })}</span>
      </span>
    );
  }
  return <span className="av-coldoc av-coldoc--gap" title={t("本列与上游都没有数据库注释")}><Pill tone="warn">{t("注释缺失")}</Pill></span>;
}
