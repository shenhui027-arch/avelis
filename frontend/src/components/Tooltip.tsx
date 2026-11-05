// 全局提示层：页面里的 data-tip（以及原生 title）统一显示成 Avelis 自己的提示，不用浏览器原生 tooltip。
// 原生 title 由系统绘制、延迟一秒多，深色界面里是白底。data-tip 由 React 管理，随状态增删；title 只在提示显示期间
// 借用（暂时移除以免原生提示叠出来），收起时原样放回，不留下与 React 状态脱节的副本。会随状态出现、消失的说明写 data-tip。
// 行为对照 Radix Tooltip：首次悬停延迟显示，在提示之间移动时立即切换；Esc、按下、滚动时收起。
// 触屏没有悬停：轻点 ⓘ（.av-hint）打开或收起它的提示。
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const SHOW_DELAY = 380;
const SKIP_DELAY = 300; // 刚收起一个提示后在这段时间内指向另一个，立即显示
const GAP = 8;
const EDGE = 8;

/** 借用的 title：收起时放回；为没有可见文字的元素临时补的 aria-label 一并撤掉。 */
type Borrowed = { title: string; label: boolean };
type Tip = { el: HTMLElement; text: string; borrowed?: Borrowed };

function tipTarget(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  const el = node.closest<HTMLElement>("[data-tip], [title]");
  if (!el || el instanceof SVGElement) return null;
  return (el.dataset.tip ?? el.getAttribute("title") ?? "").trim() ? el : null;
}

function borrow(el: HTMLElement): Tip {
  const tip = el.dataset.tip?.trim();
  if (tip) return { el, text: tip };
  const title = el.getAttribute("title") ?? "";
  const label = !el.hasAttribute("aria-label") && !el.textContent?.trim();
  el.removeAttribute("title");
  if (label) el.setAttribute("aria-label", title);
  return { el, text: title, borrowed: { title, label } };
}

function giveBack(tip: Tip) {
  tip.el.removeAttribute("aria-describedby");
  if (!tip.borrowed) return;
  // 显示期间 React 写入了新的 title：以它为准，不覆盖
  if (!tip.el.hasAttribute("title")) tip.el.setAttribute("title", tip.borrowed.title);
  if (tip.borrowed.label && tip.el.getAttribute("aria-label") === tip.borrowed.title) tip.el.removeAttribute("aria-label");
}

export function TooltipLayer() {
  const [tip, setTip] = useState<Tip | null>(null);
  const [pos, setPos] = useState<{ left: number; top: number; side: "top" | "bottom" } | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const id = useId();
  const timer = useRef<number | undefined>(undefined);
  const hiddenAt = useRef(0);
  const current = useRef<Tip | null>(null);

  useEffect(() => {
    const hide = () => {
      window.clearTimeout(timer.current);
      timer.current = undefined;
      if (current.current) { hiddenAt.current = Date.now(); giveBack(current.current); }
      current.current = null;
      setTip(null);
    };
    const show = (el: HTMLElement, immediate: boolean) => {
      if (current.current?.el === el) return;
      window.clearTimeout(timer.current);
      const open = () => {
        timer.current = undefined;
        if (current.current) giveBack(current.current);
        if (!el.isConnected || !tipTarget(el)) { current.current = null; setTip(null); return; }
        const next = borrow(el);
        current.current = next;
        if (!el.hasAttribute("aria-label") || el.getAttribute("aria-label") !== next.text) el.setAttribute("aria-describedby", id);
        setTip(next);
      };
      if (immediate || Date.now() - hiddenAt.current < SKIP_DELAY || current.current) open();
      else timer.current = window.setTimeout(open, SHOW_DELAY);
    };
    const over = (e: PointerEvent) => {
      if (e.pointerType === "touch") return;
      const el = tipTarget(e.target);
      if (el) show(el, false);
      else if (current.current || timer.current) hide();
    };
    const focus = (e: FocusEvent) => {
      const el = tipTarget(e.target);
      if (el && el.matches(":focus-visible")) show(el, true);
    };
    // 只在提示所属的元素失焦时收起：轻点 ⓘ 时前一个元素的失焦不能把刚打开的提示关掉
    const blur = (e: FocusEvent) => { if (current.current?.el === e.target) hide(); };
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    // 触屏与笔：轻点 ⓘ 切换它的提示，点别处收起
    const press = (e: PointerEvent) => {
      const hint = e.pointerType !== "mouse" && e.target instanceof Element ? e.target.closest<HTMLElement>(".av-hint") : null;
      if (hint && current.current?.el !== hint) { hide(); show(hint, true); return; }
      hide();
    };
    const leaveWindow = (e: PointerEvent) => { if (!e.relatedTarget) hide(); };
    window.addEventListener("pointerover", over);
    window.addEventListener("pointerout", leaveWindow);
    window.addEventListener("focusin", focus);
    window.addEventListener("focusout", blur);
    window.addEventListener("pointerdown", press);
    window.addEventListener("keydown", key);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("pointerover", over);
      window.removeEventListener("pointerout", leaveWindow);
      window.removeEventListener("focusin", focus);
      window.removeEventListener("focusout", blur);
      window.removeEventListener("pointerdown", press);
      window.removeEventListener("keydown", key);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      window.clearTimeout(timer.current);
    };
  }, [id]);

  // 元素被移出页面（列表刷新、路由切换）时提示跟着收起
  useEffect(() => {
    if (!tip) return;
    const check = window.setInterval(() => { if (!tip.el.isConnected) { giveBack(tip); current.current = null; setTip(null); } }, 250);
    return () => window.clearInterval(check);
  }, [tip]);

  useLayoutEffect(() => {
    if (!tip || !box.current) { setPos(null); return; }
    const r = tip.el.getBoundingClientRect();
    const b = box.current.getBoundingClientRect();
    const side = r.top - b.height - GAP >= EDGE ? "top" : "bottom";
    const top = side === "top" ? r.top - b.height - GAP : r.bottom + GAP;
    const left = Math.max(EDGE, Math.min(r.left + r.width / 2 - b.width / 2, window.innerWidth - b.width - EDGE));
    setPos({ left, top, side });
  }, [tip]);

  if (!tip) return null;
  return createPortal(
    <div className="av-root av-float-root">
      <div ref={box} id={id} role="tooltip" className="av-tooltip" data-side={pos?.side}
        style={pos ? { left: pos.left, top: pos.top } : { left: -9999, top: -9999, visibility: "hidden" }}>{tip.text}</div>
    </div>,
    document.body,
  );
}
