// 输入框底栏的选项胶囊与技能引用选择器（“+” 按钮与 “/” 斜杠唤起，不用浏览器原生下拉）。
import { useQuery } from "@tanstack/react-query";
import {
  useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
  type ChangeEvent, type CSSProperties, type KeyboardEvent, type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText } from "../../api/hooks";
import { Icon } from "../../components/Icon";
import { t } from "../../i18n";
import { usePopover } from "./ModelPicker";

export type ChipOption<T extends string> = { value: T; label: string; hint?: string };

export function ChipMenu<T extends string>({ icon, title, value, display, options, onChange, keepOpenFor, down, children }: {
  icon: string; title: string; value: T; display?: string; options: ChipOption<T>[];
  onChange: (value: T) => void; keepOpenFor?: T; down?: boolean; children?: ReactNode;
}) {
  const { open, setOpen, ref } = usePopover();
  const current = options.find((o) => o.value === value);
  return (
    <div className="av-mbar av-chip" ref={ref}>
      <button type="button" className="av-chip__btn" aria-haspopup="menu" aria-expanded={open} title={title}
        onClick={() => setOpen(!open)}>
        <Icon name={icon} small />
        <span className="av-chip__value">{display ?? current?.label}</span>
        <Icon name="chevron" small />
      </button>
      {open ? (
        <div className={`av-menu ${down ? "av-chip__menu--down" : "av-menu--up"} av-mbar__menu av-chip__menu`} role="menu" aria-label={title}>
          <div className="av-mbar__sec">{title}</div>
          {options.map((o) => (
            <button key={o.value} type="button" role="menuitemradio" aria-checked={o.value === value}
              className="av-menu__item av-chip__item"
              onClick={() => { onChange(o.value); if (o.value !== keepOpenFor) setOpen(false); }}>
              <span className="av-mbar__check">{o.value === value ? <Icon name="check" /> : null}</span>
              <span className="av-chip__text"><b>{o.label}</b>{o.hint ? <small>{o.hint}</small> : null}</span>
            </button>
          ))}
          {children ? <div className="av-chip__extra">{children}</div> : null}
        </div>
      ) : null}
    </div>
  );
}

// --- 技能引用（“+” 按钮与输入框 “/” 共用同一个菜单） --------------------------------------

type Skill = Schemas["SkillOut"];
type SlashToken = { start: number; end: number; query: string };
type MenuPlace = { top?: number; bottom?: number; left: number; width: number; maxHeight: number };

const MAX_SKILLS = 5;

/** 识别光标处的 `/筛选词`：`/` 须在开头或空白字符之后，避免日期或路径误触。 */
function findSlashToken(text: string, caret: number | null): SlashToken | null {
  if (caret === null || caret < 1) return null;
  const before = text.slice(0, caret);
  const m = /(?:^|\s)\/([^\s/]*)$/.exec(before);
  if (!m) return null;
  const query = m[1];
  const start = caret - query.length - 1;
  let end = caret;
  while (end < text.length && !/\s/.test(text[end])) end += 1;
  return { start, end, query };
}

export function useSkillPicker(text: string, onChangeText: (next: string) => void) {
  const [skills, setSkills] = useState<string[]>([]);
  const [plusOpen, setPlusOpen] = useState(false);
  const [plusQuery, setPlusQuery] = useState("");
  const [caret, setCaret] = useState<number | null>(null);
  const [dismissedSlash, setDismissedSlash] = useState<number | null>(null);
  const [active, setActive] = useState(-1);
  const [place, setPlace] = useState<MenuPlace | null>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const plusBtnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const filterInputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();

  const slashToken = useMemo(() => findSlashToken(text, caret), [text, caret]);
  const slashOpen = !plusOpen && !!slashToken && dismissedSlash !== slashToken.start;
  const open = plusOpen || slashOpen;
  const mode: "plus" | "slash" | null = plusOpen ? "plus" : slashOpen ? "slash" : null;

  const enabledQuery = useQuery({
    queryKey: ["skills", "list", "enabled", ""],
    enabled: open,
    queryFn: () => unwrap(api.GET("/api/skills", { params: { query: { view: "enabled", q: undefined } } })),
  });

  const enabledSkills: Skill[] = enabledQuery.data ?? [];
  const filterTerm = (mode === "slash" ? slashToken?.query ?? "" : plusQuery).trim().toLocaleLowerCase();
  const filtered = useMemo(
    () => !filterTerm
      ? enabledSkills
      : enabledSkills.filter((s) =>
          s.name.toLocaleLowerCase().includes(filterTerm) || s.description.toLocaleLowerCase().includes(filterTerm)),
    [enabledSkills, filterTerm],
  );

  const atLimit = skills.length >= MAX_SKILLS;
  const canSelect = (s: Skill) => !atLimit && !skills.includes(s.name);

  useEffect(() => {
    if (!open) {
      setActive(-1);
      return;
    }
    const first = filtered.findIndex((s) => !atLimit && !skills.includes(s.name));
    setActive(first);
  }, [open, filtered, atLimit, skills]);

  useEffect(() => {
    if (!open || active < 0) return;
    listRef.current?.querySelector(`[data-opt-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  useEffect(() => {
    if (plusOpen) {
      const id = window.requestAnimationFrame(() => filterInputRef.current?.focus());
      return () => window.cancelAnimationFrame(id);
    }
  }, [plusOpen]);

  const closeMenu = () => {
    setPlusOpen(false);
    if (slashToken) setDismissedSlash(slashToken.start);
  };

  useLayoutEffect(() => {
    if (!open) {
      setPlace(null);
      return;
    }
    const update = (e?: Event) => {
      if (e?.type === "scroll" && e.target instanceof Node && menuRef.current?.contains(e.target)) return;
      const r = plusBtnRef.current?.getBoundingClientRect();
      if (!r) return;
      const gap = 6;
      const margin = 8;
      const maxH = 320;
      const width = Math.min(340, window.innerWidth - margin * 2);
      const left = Math.max(margin, Math.min(r.left, window.innerWidth - width - margin));
      const below = window.innerHeight - r.bottom - gap - margin;
      const above = r.top - gap - margin;
      const next: MenuPlace = below >= 260 || below >= above
        ? { top: r.bottom + gap, left, width, maxHeight: Math.min(maxH, Math.max(160, below)) }
        : { bottom: window.innerHeight - r.top + gap, left, width, maxHeight: Math.min(maxH, Math.max(160, above)) };
      setPlace(next);
    };
    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target) || plusBtnRef.current?.contains(target)) return;
      if (mode === "slash" && textareaRef.current?.contains(target)) return;
      const fromPlus = mode === "plus";
      setPlusOpen(false);
      if (slashToken) setDismissedSlash(slashToken.start);
      if (fromPlus) {
        const clickedFocusable = target instanceof Element
          && !!target.closest('input, textarea, select, button, a[href], [tabindex]:not([tabindex="-1"])');
        if (!clickedFocusable) {
          plusBtnRef.current?.focus();
          window.requestAnimationFrame(() => {
            if (document.activeElement === document.body) plusBtnRef.current?.focus();
          });
        }
      }
    };
    window.addEventListener("mousedown", onMouseDown);
    return () => window.removeEventListener("mousedown", onMouseDown);
  }, [open, mode, slashToken]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.isComposing || e.keyCode === 229 || e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      const fromPlus = mode === "plus";
      setPlusOpen(false);
      if (slashToken) setDismissedSlash(slashToken.start);
      if (fromPlus) plusBtnRef.current?.focus();
      else textareaRef.current?.focus();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [open, mode, slashToken]);

  const stepActive = (dir: 1 | -1) => {
    if (!filtered.length || atLimit) return;
    const len = filtered.length;
    const start = active >= 0 ? active : dir === 1 ? -1 : len;
    for (let step = 1; step <= len; step += 1) {
      const idx = ((start + dir * step) % len + len) % len;
      if (canSelect(filtered[idx])) {
        setActive(idx);
        return;
      }
    }
  };

  const pickSkill = (name: string) => {
    if (atLimit || skills.includes(name)) return;
    const wasSlash = mode === "slash" && slashToken;
    setSkills((prev) => (prev.includes(name) || prev.length >= MAX_SKILLS ? prev : [...prev, name]));
    if (wasSlash) {
      const before = text.slice(0, wasSlash.start);
      const after = text.slice(wasSlash.end);
      const nextText = before.endsWith(" ") && after.startsWith(" ") ? before + after.slice(1) : before + after;
      const nextCaret = before.length;
      onChangeText(nextText);
      setCaret(nextCaret);
      setDismissedSlash(null);
      window.requestAnimationFrame(() => {
        const el = textareaRef.current;
        if (el) {
          el.focus();
          el.setSelectionRange(nextCaret, nextCaret);
        }
      });
    } else {
      setPlusOpen(false);
      window.requestAnimationFrame(() => textareaRef.current?.focus());
    }
  };

  const handleMenuNavKey = (e: KeyboardEvent<HTMLElement>): boolean => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return false;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      const fromPlus = mode === "plus";
      closeMenu();
      if (fromPlus) plusBtnRef.current?.focus();
      else textareaRef.current?.focus();
      return true;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      stepActive(1);
      return true;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      stepActive(-1);
      return true;
    }
    const enterPick = e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.shiftKey;
    const slashTabPick = mode === "slash" && e.key === "Tab" && !e.shiftKey;
    if (enterPick || slashTabPick) {
      const target = active >= 0 ? filtered[active] : undefined;
      if (target && canSelect(target)) {
        e.preventDefault();
        pickSkill(target.name);
        return true;
      }
      if (enterPick && mode === "slash") {
        e.preventDefault();
        closeMenu();
        return true;
      }
    }
    return false;
  };

  const onMenuKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      const fromPlus = mode === "plus";
      closeMenu();
      if (fromPlus) plusBtnRef.current?.focus();
      else textareaRef.current?.focus();
      return;
    }
    if (mode === "plus" && e.key === "Tab") {
      const items = Array.from(
        menuRef.current?.querySelectorAll<HTMLElement>(
          'input, button, a[href], [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((el) => !el.hasAttribute("disabled"));
      if (!items.length) {
        e.preventDefault();
        closeMenu();
        plusBtnRef.current?.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const activeEl = document.activeElement;
      const inside = !!activeEl && !!menuRef.current?.contains(activeEl);
      if ((e.shiftKey && (activeEl === first || !inside)) || (!e.shiftKey && (activeEl === last || !inside))) {
        e.preventDefault();
        closeMenu();
        plusBtnRef.current?.focus();
      }
      return;
    }
    if (mode === "plus" && (e.key === "ArrowDown" || e.key === "ArrowUp") && e.target !== filterInputRef.current) {
      e.preventDefault();
      stepActive(e.key === "ArrowDown" ? 1 : -1);
      filterInputRef.current?.focus();
    }
  };

  const onTextareaKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!slashOpen) return false;
    return handleMenuNavKey(e);
  };

  const activeOptId = open && active >= 0 && filtered[active] ? `${listboxId}-opt-${active}` : undefined;

  const syncCaret = () => {
    const el = textareaRef.current;
    if (!el) return;
    setCaret(el.selectionStart === el.selectionEnd ? el.selectionStart : null);
  };

  const textareaProps = {
    role: slashOpen ? ("combobox" as const) : undefined,
    "aria-autocomplete": slashOpen ? ("list" as const) : undefined,
    "aria-activedescendant": slashOpen ? activeOptId : undefined,
    "aria-controls": slashOpen ? listboxId : undefined,
    "aria-expanded": slashOpen || undefined,
    onChange: (e: ChangeEvent<HTMLTextAreaElement>) => {
      const val = e.target.value;
      const pos = e.target.selectionStart;
      onChangeText(val);
      setCaret(pos);
      const nextSlash = findSlashToken(val, pos);
      if (nextSlash) {
        if (plusOpen) setPlusOpen(false);
        if (dismissedSlash !== null && dismissedSlash !== nextSlash.start) setDismissedSlash(null);
      } else {
        setDismissedSlash(null);
      }
    },
    onFocus: syncCaret,
    onSelect: syncCaret,
    onClick: syncCaret,
    onBlur: () => {
      // 延迟清空光标状态，避免点击菜单项或菜单内操作时卸掉菜单
      window.setTimeout(() => {
        const activeEl = document.activeElement;
        if (activeEl !== textareaRef.current && !menuRef.current?.contains(activeEl)) {
          setCaret(null);
        }
      }, 120);
    },
  };

  return {
    skills,
    remove: (name: string) => setSkills((prev) => prev.filter((s) => s !== name)),
    clear: () => { setSkills([]); setPlusOpen(false); setPlusQuery(""); setDismissedSlash(null); },
    textareaRef,
    plusBtnRef,
    menuRef,
    listRef,
    filterInputRef,
    listboxId,
    open,
    mode,
    plusOpen,
    setPlusOpen,
    plusQuery,
    setPlusQuery,
    slashToken,
    setDismissedSlash,
    enabledQuery,
    enabledSkills,
    filtered,
    active,
    setActive,
    activeOptId,
    atLimit,
    canSelect,
    pickSkill,
    closeMenu,
    handleMenuNavKey,
    onMenuKeyDown,
    onTextareaKeyDown,
    textareaProps,
    place,
  };
}

export type SkillPickerState = ReturnType<typeof useSkillPicker>;

/** 输入框上方已选中的技能标签（最多 5 个）。 */
export function SkillTags({ skills, onRemove }: { skills: string[]; onRemove: (name: string) => void }) {
  if (!skills.length) return null;
  return (
    <div className="av-compose__skills" aria-label={t("已引用的技能")}>
      {skills.map((name) => (
        <span key={name} className="av-tag av-compose__skill-tag">
          <Icon name="skill" small />
          <span>{name}</span>
          <button
            type="button"
            className="av-compose__skill-rm"
            aria-label={t("移除技能 {{name}}", { name })}
            onClick={() => onRemove(name)}
          >
            <Icon name="close" small />
          </button>
        </span>
      ))}
    </div>
  );
}

/** 输入框左侧 “+” 按钮及复用的技能弹出菜单。 */
export function SkillPickerTrigger({ state }: { state: SkillPickerState }) {
  const {
    plusBtnRef, menuRef, listRef, filterInputRef, textareaRef, listboxId,
    open, mode, plusOpen, setPlusOpen, plusQuery, setPlusQuery,
    slashToken, setDismissedSlash, enabledQuery, enabledSkills, filtered,
    active, setActive, activeOptId, atLimit, canSelect, pickSkill, closeMenu,
    handleMenuNavKey, onMenuKeyDown, place, skills,
  } = state;

  const style: CSSProperties | undefined = place
    ? { top: place.top, bottom: place.bottom, left: place.left, width: place.width, maxHeight: place.maxHeight }
    : undefined;

  return (
    <>
      <button
        ref={plusBtnRef}
        type="button"
        className="av-btn av-btn--ghost av-btn--icon av-compose__plus-btn"
        aria-label={t("引用技能")}
        title={t("引用技能")}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        onClick={() => {
          if (plusOpen) {
            setPlusOpen(false);
          } else {
            setPlusQuery("");
            setPlusOpen(true);
            if (slashToken) setDismissedSlash(slashToken.start);
          }
        }}
      >
        <Icon name="plus" />
      </button>

      {open && place ? createPortal(
        <div className="av-root av-float-root">
          <div
            ref={menuRef}
            className="av-menu av-popover av-skill-picker"
            style={style}
            onKeyDown={onMenuKeyDown}
            onMouseDown={(e) => {
              if (mode === "slash") {
                if (!(e.target instanceof HTMLInputElement)) e.preventDefault();
              } else if (mode === "plus" && !(e.target instanceof Element && e.target.closest("input, button, a[href]"))) {
                e.preventDefault();
              }
            }}
          >
            {mode === "plus" ? (
              <div className="av-skill-picker__search">
                <Icon name="search" small />
                <input
                  ref={filterInputRef}
                  value={plusQuery}
                  onChange={(e) => setPlusQuery(e.target.value)}
                  onKeyDown={(e) => { handleMenuNavKey(e); }}
                  placeholder={t("筛选技能")}
                  aria-label={t("筛选技能")}
                  role="combobox"
                  aria-expanded={true}
                  aria-controls={listboxId}
                  aria-activedescendant={activeOptId}
                  autoComplete="off"
                />
              </div>
            ) : null}

            {atLimit ? (
              <div className="av-skill-picker__limit">{t("最多引用 5 个技能")}</div>
            ) : null}

            <div ref={listRef} id={listboxId} role="listbox" aria-label={t("引用技能")} className="av-skill-picker__list">
              {enabledQuery.isPending ? (
                <div className="av-skill-picker__empty"><span>{t("加载中…")}</span></div>
              ) : enabledQuery.isError && !enabledQuery.data ? (
                <div className="av-skill-picker__empty" role="alert">
                  <span className="av-skill-picker__err">{errorText(enabledQuery.error)}</span>
                  <button
                    type="button"
                    className="av-btn av-btn--secondary av-btn--sm"
                    onMouseDown={(e) => { if (mode === "slash") e.preventDefault(); }}
                    onClick={() => {
                      void enabledQuery.refetch();
                      if (mode === "plus") filterInputRef.current?.focus();
                      else textareaRef.current?.focus();
                    }}
                  >
                    <Icon name="refresh" small />{t("重试")}
                  </button>
                </div>
              ) : !enabledSkills.length ? (
                <div className="av-skill-picker__empty">
                  <span>{t("暂无启用的技能")}</span>
                  <Link className="av-btn av-btn--secondary av-btn--sm" to="/skills/market" onClick={closeMenu}>
                    {t("去技能市场")}
                  </Link>
                </div>
              ) : !filtered.length ? (
                <div className="av-skill-picker__empty"><span>{t("没有匹配的技能")}</span></div>
              ) : (
                filtered.map((s, i) => {
                  const isSelected = skills.includes(s.name);
                  const selectable = canSelect(s);
                  const isDisabled = !selectable;
                  return (
                    <div
                      key={s.id}
                      id={`${listboxId}-opt-${i}`}
                      data-opt-index={i}
                      role="option"
                      aria-selected={isSelected}
                      aria-disabled={isDisabled || undefined}
                      className={`av-skill-picker__opt${i === active ? " is-active" : ""}${isSelected ? " is-selected" : ""}${isDisabled ? " is-disabled" : ""}`}
                      onMouseEnter={() => { if (selectable) setActive(i); }}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => { if (selectable) pickSkill(s.name); }}
                    >
                      <span className="av-skill-picker__check">
                        {isSelected ? <Icon name="check" small /> : <Icon name="skill" small />}
                      </span>
                      <span className="av-skill-picker__text">
                        <span className="av-skill-picker__name">{s.name}</span>
                        <span className="av-skill-picker__desc">{s.description}</span>
                      </span>
                    </div>
                  );
                })
              )}
            </div>

            <div className="av-skill-picker__ft">
              <Link className="av-skill-picker__manage" to="/skills/market" onClick={closeMenu}>
                <Icon name="skill" small />{t("管理技能")}
              </Link>
            </div>
          </div>
        </div>,
        document.body,
      ) : null}
    </>
  );
}
