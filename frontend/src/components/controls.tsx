// 表单控件：下拉选择、日期、带建议的输入、二次确认。全部用 Avelis UI 的浮层样式，不用浏览器原生的
// <select>、<datalist>、type="date|time" 与 confirm()——原生弹出层由系统绘制，深色玻璃界面里会变成白底或系统风格。
// 浮层挂到 body 并按触发器位置 fixed 定位：面板的 overflow 与 backdrop-filter 不会把它裁掉。
import {
  Children, isValidElement, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
  type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactElement, type ReactNode, type RefObject,
} from "react";
import { createPortal } from "react-dom";

import { errorText } from "../api/hooks";
import { currentLanguage, t } from "../i18n";
import { Icon } from "./Icon";

// --- 浮层定位 -----------------------------------------------------------------------

const GAP = 4;
const MAX_LIST_HEIGHT = 320;
/** 日历整体高度（标题、星期、6 行日期、底栏与内边距），用于决定向上还是向下展开。 */
const DATEPICK_HEIGHT = 360;

type Place = { top?: number; bottom?: number; left: number; minWidth: number; maxHeight: number };

/** 浮层贴着触发器：下方放得下整个浮层（最高 height）就向下，否则放在空间更大的一侧并限高滚动；
 *  滚动与窗口变化时跟随，点外部关闭。 */
function useFloating(open: boolean, anchor: RefObject<HTMLElement | null>, floating: RefObject<HTMLElement | null>,
  onClose: () => void, minWidth = 0, height = MAX_LIST_HEIGHT): Place | null {
  const [place, setPlace] = useState<Place | null>(null);
  useLayoutEffect(() => {
    if (!open) { setPlace(null); return; }
    const update = (e?: Event) => {
      // 浮层自己的列表滚动不影响位置；位置没变时保留原对象，避免依赖 place 的效果反复把列表滚回选中项
      if (e?.type === "scroll" && e.target instanceof Node && floating.current?.contains(e.target)) return;
      const r = anchor.current?.getBoundingClientRect();
      if (!r) return;
      const below = window.innerHeight - r.bottom - GAP * 2;
      const above = r.top - GAP * 2;
      const width = Math.max(r.width, minWidth);
      const left = Math.max(GAP, Math.min(r.left, window.innerWidth - width - GAP));
      const next: Place = below >= height || below >= above
        ? { top: r.bottom + GAP, left, minWidth: width, maxHeight: Math.min(height, below) }
        : { bottom: window.innerHeight - r.top + GAP, left, minWidth: width, maxHeight: Math.min(height, above) };
      setPlace((prev) => (prev && prev.top === next.top && prev.bottom === next.bottom && prev.left === next.left
        && prev.minWidth === next.minWidth && prev.maxHeight === next.maxHeight ? prev : next));
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => { window.removeEventListener("resize", update); window.removeEventListener("scroll", update, true); };
  }, [open, anchor, minWidth, height]);
  useEffect(() => {
    if (!open) return;
    const outside = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!anchor.current?.contains(target) && !floating.current?.contains(target)) onClose();
    };
    window.addEventListener("mousedown", outside);
    return () => window.removeEventListener("mousedown", outside);
  }, [open, anchor, floating, onClose]);
  return place;
}

function Floating({ place, innerRef, className, children, ...rest }: {
  place: Place; innerRef: RefObject<HTMLDivElement | null>; className: string; children: ReactNode;
  id?: string; role?: string; "aria-label"?: string; onMouseDown?: (e: React.MouseEvent) => void; onKeyDown?: (e: ReactKeyboardEvent) => void;
}) {
  const style: CSSProperties = { top: place.top, bottom: place.bottom, left: place.left, minWidth: place.minWidth, maxHeight: place.maxHeight };
  return createPortal(<div className="av-root av-float-root"><div ref={innerRef} className={`av-menu av-popover ${className}`} style={style} {...rest}>{children}</div></div>, document.body);
}

// --- 下拉选择 -----------------------------------------------------------------------

type Option = { value: string; label: ReactNode; text: string; disabled: boolean };

function textOf(node: ReactNode): string {
  if (node == null || typeof node === "boolean") return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

/** 子元素沿用 <option value disabled>文字</option> 的写法，只当作选项数据读取，不会渲染成原生下拉。 */
function readOptions(children: ReactNode): Option[] {
  return Children.toArray(children).filter((c): c is ReactElement<{ value?: string | number; disabled?: boolean; children?: ReactNode }> =>
    isValidElement(c) && c.type === "option").map((c) => {
    const text = textOf(c.props.children);
    return { value: String(c.props.value ?? text), label: c.props.children, text, disabled: !!c.props.disabled };
  });
}

export function Select({ value, onChange, children, disabled, label, style, id, className, invalid, describedBy }: {
  value: string; onChange: (v: string) => void; children: ReactNode; disabled?: boolean; label?: string;
  style?: CSSProperties; id?: string; className?: string; invalid?: boolean; describedBy?: string;
}) {
  const options = readOptions(children);
  const selected = options.findIndex((o) => o.value === value);
  const current = options[selected] ?? options[0];
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const anchor = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const typed = useRef({ text: "", at: 0 });
  const listId = useId();
  const close = useMemo(() => () => setOpen(false), []);
  const place = useFloating(open, anchor, list, close);

  const enabled = (i: number) => i >= 0 && i < options.length && !options[i].disabled;
  const step = (from: number, dir: 1 | -1) => {
    for (let i = from + dir; i >= 0 && i < options.length; i += dir) if (enabled(i)) return i;
    return from;
  };
  const show = () => { if (disabled || !options.length) return; setActive(enabled(selected) ? selected : step(-1, 1)); setOpen(true); };
  const pick = (i: number) => { if (!enabled(i)) return; setOpen(false); anchor.current?.focus(); if (options[i].value !== value) onChange(options[i].value); };
  // 列表挂载（place 就绪）后再滚到选中项，长列表打开时选中项可见
  useEffect(() => { if (open && place) list.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" }); }, [open, place, active]);

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (disabled) return;
    const keys: Record<string, () => void> = {
      ArrowDown: () => (open ? setActive(step(active, 1)) : show()),
      ArrowUp: () => (open ? setActive(step(active, -1)) : show()),
      Home: () => open && setActive(step(-1, 1)),
      End: () => open && setActive(step(options.length, -1)),
      Enter: () => (open ? pick(active) : show()),
      " ": () => (open ? pick(active) : show()),
      Escape: () => setOpen(false),
    };
    if (e.key === "Tab") { setOpen(false); return; }
    if (keys[e.key] && (e.key !== "Escape" || open)) { e.preventDefault(); e.stopPropagation(); keys[e.key](); return; }
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      // 按字母跳到以它开头的选项，与原生下拉一致
      const now = Date.now();
      typed.current = { text: (now - typed.current.at < 600 ? typed.current.text : "") + e.key.toLowerCase(), at: now };
      const hit = options.findIndex((o, i) => enabled(i) && o.text.toLowerCase().startsWith(typed.current.text));
      if (hit >= 0) { if (open) setActive(hit); else if (options[hit].value !== value) onChange(options[hit].value); }
    }
  };

  return (
    <span className={`av-select-wrap${className ? ` ${className}` : ""}`} style={{ width: "100%", ...style }}>
      <button ref={anchor} type="button" id={id} className="av-select av-select--button" role="combobox" aria-haspopup="listbox"
        aria-expanded={open} aria-controls={open ? listId : undefined} aria-label={label} aria-invalid={invalid || undefined}
        aria-describedby={describedBy} aria-activedescendant={open && active >= 0 ? `${listId}-${active}` : undefined}
        disabled={disabled} onClick={() => (open ? setOpen(false) : show())} onKeyDown={onKeyDown}>
        <span className="av-select__value">{current?.label}</span>
      </button>
      <Icon name="chevron" small />
      {open && place ? (
        <Floating place={place} innerRef={list} id={listId} role="listbox" aria-label={label} className="av-listbox"
          onMouseDown={(e) => e.preventDefault()}>
          {options.map((o, i) => (
            <div key={`${o.value}-${i}`} id={`${listId}-${i}`} data-index={i} role="option" aria-selected={o.value === value}
              aria-disabled={o.disabled || undefined} className={`av-menu__item av-listbox__item${i === active ? " is-active" : ""}${className?.includes("av-mono") ? " av-mono" : ""}`}
              onMouseEnter={() => enabled(i) && setActive(i)} onClick={() => pick(i)}>
              <span className="av-listbox__check">{o.value === value ? <Icon name="check" small /> : null}</span>
              <span className="av-listbox__label">{o.label}</span>
            </div>
          ))}
        </Floating>
      ) : null}
    </span>
  );
}

// --- 日期 ---------------------------------------------------------------------------

const pad = (n: number) => String(n).padStart(2, "0");
const iso = (y: number, m: number, d: number) => `${y}-${pad(m + 1)}-${pad(d)}`;
function parseDate(value: string): { y: number; m: number; d: number } | null {
  const hit = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return hit ? { y: Number(hit[1]), m: Number(hit[2]) - 1, d: Number(hit[3]) } : null;
}
function today(): { y: number; m: number; d: number } {
  const n = new Date();
  return { y: n.getFullYear(), m: n.getMonth(), d: n.getDate() };
}
/** 日期按日历天计算（UTC 中立），不受浏览器时区影响。 */
function shift(y: number, m: number, d: number, days: number) {
  const n = new Date(Date.UTC(y, m, d + days));
  return { y: n.getUTCFullYear(), m: n.getUTCMonth(), d: n.getUTCDate() };
}
const daysIn = (y: number, m: number) => new Date(Date.UTC(y, m + 1, 0)).getUTCDate();

/** 日期输入：值是 YYYY-MM-DD 字符串（空串表示未选），与原生 date 输入相同。 */
export function DateInput({ value, onChange, label, placeholder, disabled, invalid, title, clearable = true }: {
  value: string; onChange: (v: string) => void; label?: string; placeholder?: string; disabled?: boolean;
  invalid?: boolean; title?: string; clearable?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const picked = parseDate(value);
  const [focus, setFocus] = useState(() => picked ?? today());
  const anchor = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useMemo(() => () => setOpen(false), []);
  const place = useFloating(open, anchor, panel, close, 280, DATEPICK_HEIGHT);
  const lang = currentLanguage();
  const monthTitle = new Intl.DateTimeFormat(lang, { year: "numeric", month: "long", timeZone: "UTC" }).format(new Date(Date.UTC(focus.y, focus.m, 1)));
  // 周一开头
  const weekdays = Array.from({ length: 7 }, (_, i) => new Intl.DateTimeFormat(lang, { weekday: "narrow", timeZone: "UTC" }).format(new Date(Date.UTC(2024, 0, 1 + i))));
  const lead = (new Date(Date.UTC(focus.y, focus.m, 1)).getUTCDay() + 6) % 7;
  const cells = Array.from({ length: 42 }, (_, i) => shift(focus.y, focus.m, 1, i - lead));
  const now = today();
  const same = (a: { y: number; m: number; d: number } | null, b: { y: number; m: number; d: number }) => !!a && a.y === b.y && a.m === b.m && a.d === b.d;

  const openPanel = () => { if (disabled) return; setFocus(picked ?? today()); setOpen(true); };
  const choose = (c: { y: number; m: number; d: number } | null) => { onChange(c ? iso(c.y, c.m, c.d) : ""); setOpen(false); anchor.current?.focus(); };
  const month = (delta: number) => setFocus((f) => {
    const y = f.y + Math.floor((f.m + delta) / 12), m = ((f.m + delta) % 12 + 12) % 12;
    return { y, m, d: Math.min(f.d, daysIn(y, m)) };
  });
  useEffect(() => { if (open && place) panel.current?.querySelector<HTMLButtonElement>("[data-focus='true']")?.focus(); }, [open, place, focus]);

  const onGridKey = (e: ReactKeyboardEvent) => {
    const moves: Record<string, () => void> = {
      ArrowLeft: () => setFocus((f) => shift(f.y, f.m, f.d, -1)), ArrowRight: () => setFocus((f) => shift(f.y, f.m, f.d, 1)),
      ArrowUp: () => setFocus((f) => shift(f.y, f.m, f.d, -7)), ArrowDown: () => setFocus((f) => shift(f.y, f.m, f.d, 7)),
      PageUp: () => month(-1), PageDown: () => month(1),
      Escape: () => { setOpen(false); anchor.current?.focus(); },
    };
    // 日历挂在 body 上，Tab 会跑出所在抽屉：收起日历、焦点回到触发按钮，再由浏览器按顺序移到下一个控件
    if (e.key === "Tab") { anchor.current?.focus(); setOpen(false); return; }
    if (moves[e.key]) { e.preventDefault(); e.stopPropagation(); moves[e.key](); }
  };

  return (
    <span className="av-select-wrap" style={{ width: "100%" }}>
      <button ref={anchor} type="button" className="av-select av-select--button av-date" aria-haspopup="dialog" aria-expanded={open}
        aria-label={label} aria-invalid={invalid || undefined} title={title} disabled={disabled}
        onClick={() => (open ? setOpen(false) : openPanel())}
        onKeyDown={(e) => { if (e.key === "ArrowDown") { e.preventDefault(); openPanel(); } }}>
        <span className={`av-select__value${value ? " av-mono" : " av-select__placeholder"}`}>{value || placeholder || t("选择日期")}</span>
      </button>
      <Icon name="calendar" small />
      {open && place ? (
        <Floating place={place} innerRef={panel} role="dialog" aria-label={label ?? t("选择日期")} className="av-datepick" onKeyDown={onGridKey}>
          <div className="av-datepick__hd">
            <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label={t("上个月")} onClick={() => month(-1)}><Icon name="back" small /></button>
            <span className="av-datepick__title">{monthTitle}</span>
            <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon av-datepick__next" aria-label={t("下个月")} onClick={() => month(1)}><Icon name="back" small /></button>
          </div>
          <div className="av-datepick__grid" role="grid">
            {weekdays.map((w, i) => <span key={`w${i}`} className="av-datepick__wd" aria-hidden="true">{w}</span>)}
            {cells.map((c) => (
              <button key={iso(c.y, c.m, c.d)} type="button" tabIndex={same(focus, c) ? 0 : -1} data-focus={same(focus, c)}
                aria-label={iso(c.y, c.m, c.d)} aria-pressed={same(picked, c)}
                className={`av-datepick__day${c.m !== focus.m ? " is-outside" : ""}${same(now, c) ? " is-today" : ""}`}
                onClick={() => choose(c)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); choose(focus); } }}>{c.d}</button>
            ))}
          </div>
          <div className="av-datepick__ft">
            <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => choose(now)}>{t("今天")}</button>
            {clearable && value ? <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => choose(null)}>{t("清除")}</button> : null}
          </div>
        </Floating>
      ) : null}
    </span>
  );
}

// --- 带建议的输入 -------------------------------------------------------------------

/** 可自由输入的文本框，输入时给出候选（替代原生 datalist）。 */
export function SuggestInput({ value, onChange, suggestions, className, label, required, placeholder, disabled, invalid, describedBy }: {
  value: string; onChange: (v: string) => void; suggestions: string[]; className?: string; label?: string;
  required?: boolean; placeholder?: string; disabled?: boolean; invalid?: boolean; describedBy?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const anchor = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const listId = useId();
  const close = useMemo(() => () => setOpen(false), []);
  const needle = value.trim().toLowerCase();
  const items = suggestions.filter((s) => s.toLowerCase().includes(needle) && s !== value).slice(0, 50);
  const visible = open && items.length > 0;
  const place = useFloating(visible, anchor, list, close);
  const pick = (s: string) => { onChange(s); setOpen(false); setActive(-1); };
  useEffect(() => { if (visible && place) list.current?.querySelector(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" }); }, [visible, place, active]);

  return (
    <>
      <input ref={anchor} className={className ?? "av-input"} value={value} required={required} placeholder={placeholder} disabled={disabled}
        aria-label={label} aria-invalid={invalid || undefined} aria-describedby={describedBy} role="combobox" aria-autocomplete="list" aria-expanded={visible} aria-controls={visible ? listId : undefined}
        aria-activedescendant={visible && active >= 0 ? `${listId}-${active}` : undefined} autoComplete="off"
        onFocus={() => { setOpen(true); setActive(-1); }} onChange={(e) => { onChange(e.target.value); setOpen(true); setActive(-1); }}
        onKeyDown={(e) => {
          // 输入法组合中（选词、上屏）的方向键和 Enter 交给输入法，不当作候选导航或选择
          if (e.nativeEvent.isComposing || e.keyCode === 229) return;
          if (e.key === "ArrowDown") { e.preventDefault(); setOpen(true); setActive((a) => Math.min(a + 1, items.length - 1)); }
          else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
          else if (e.key === "Enter" && visible && active >= 0 && active < items.length) { e.preventDefault(); pick(items[active]); }
          else if (e.key === "Escape" && visible) { e.preventDefault(); e.stopPropagation(); setOpen(false); }
          else if (e.key === "Tab") setOpen(false);
        }} />
      {visible && place ? (
        <Floating place={place} innerRef={list} id={listId} role="listbox" aria-label={label} className="av-listbox" onMouseDown={(e) => e.preventDefault()}>
          {items.map((s, i) => (
            <div key={s} id={`${listId}-${i}`} data-index={i} role="option" aria-selected={i === active}
              className={`av-menu__item av-listbox__item av-mono${i === active ? " is-active" : ""}`}
              onMouseEnter={() => setActive(i)} onClick={() => pick(s)}>
              <span className="av-listbox__label">{s}</span>
            </div>
          ))}
        </Floating>
      ) : null}
    </>
  );
}

// --- 二次确认 -----------------------------------------------------------------------

/** 二次确认对话框（替代 window.confirm）：挂到 body，Esc 或点遮罩取消。 */
export function ConfirmDialog({ title, body, confirm, danger, onConfirm, onCancel, children }: {
  title: string; body?: string; confirm: string; danger?: boolean; onConfirm: () => Promise<void> | void; onCancel: () => void;
  children?: ReactNode;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDivElement>(null);
  const cancel = useRef(onCancel);
  cancel.current = onCancel;
  // 首次渲染时记下触发者：autoFocus 在 effect 之前就把焦点移进了对话框
  const [opener] = useState(() => document.activeElement as HTMLElement | null);
  // 模态：Tab 只在对话框内循环，Esc 取消，关闭后焦点回到打开它的按钮（与 Drawer 一致）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { cancel.current(); return; }
      if (e.key !== "Tab") return;
      const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])") ?? [])
        .filter((el) => !el.hasAttribute("disabled"));
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      const inside = dialog.current?.contains(document.activeElement);
      if (e.shiftKey && (document.activeElement === first || !inside)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !inside)) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener("keydown", onKey);
    return () => { window.removeEventListener("keydown", onKey); opener?.focus?.(); };
  }, [opener]);
  return createPortal(
    <div className="av-root av-dialog__scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div ref={dialog} className="av-dialog" role="alertdialog" aria-modal="true" aria-label={title}>
        <h2>{title}</h2>
        {body ? <p>{body}</p> : null}
        {children}
        {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
        <div className="av-actions">
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={onCancel} disabled={busy}>{t("取消")}</button>
          <button type="button" className={`av-btn av-btn--sm ${danger ? "av-btn--danger" : "av-btn--primary"}`} autoFocus disabled={busy}
            onClick={async () => { setBusy(true); try { await onConfirm(); } catch (e) { setError(errorText(e)); setBusy(false); } }}>
            {busy ? t("处理中…") : confirm}</button>
        </div>
      </div>
    </div>, document.body);
}
