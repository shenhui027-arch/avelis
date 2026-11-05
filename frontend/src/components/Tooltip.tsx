// 全局提示层：页面里的 data-tip（以及原生 title）统一显示成 Avelis 自己的提示，不用浏览器原生 tooltip。
// 原生 title 由系统绘制、延迟一秒多，深色界面里是白底；这里在悬停或键盘聚焦时把 title 移到 data-tip，
// 没有可见文字与 aria-label 的元素同时补上 aria-label，可访问名称不丢。
// 行为对照 Radix Tooltip：首次悬停延迟显示，在提示之间移动时立即切换；Esc、按下、滚动时收起。
import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

const SHOW_DELAY = 380;
const SKIP_DELAY = 300; // 刚收起一个提示后在这段时间内指向另一个，立即显示
const GAP = 8;
const EDGE = 8;

type Tip = { el: HTMLElement; text: string };

function tipTarget(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  const el = node.closest<HTMLElement>("[data-tip], [title]");
  if (!el || el instanceof SVGElement || el.closest("svg title")) return null;
  if (el.hasAttribute("title")) {
    const title = el.getAttribute("title") ?? "";
    el.removeAttribute("title");
    if (!title.trim()) return null;
    el.dataset.tip = title;
    if (!el.hasAttribute("aria-label") && !el.textContent?.trim()) el.setAttribute("aria-label", title);
  }
  return el.dataset.tip?.trim() ? el : null;
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
      if (current.current) hiddenAt.current = Date.now();
      current.current?.el.removeAttribute("aria-describedby");
      current.current = null;
      setTip(null);
    };
    const show = (el: HTMLElement, immediate: boolean) => {
      if (current.current?.el === el) return;
      window.clearTimeout(timer.current);
      const open = () => {
        current.current?.el.removeAttribute("aria-describedby");
        const next = { el, text: el.dataset.tip ?? "" };
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
    const key = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    const leaveWindow = (e: PointerEvent) => { if (!e.relatedTarget) hide(); };
    window.addEventListener("pointerover", over);
    window.addEventListener("pointerout", leaveWindow);
    window.addEventListener("focusin", focus);
    window.addEventListener("focusout", hide);
    window.addEventListener("pointerdown", hide);
    window.addEventListener("keydown", key);
    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    return () => {
      window.removeEventListener("pointerover", over);
      window.removeEventListener("pointerout", leaveWindow);
      window.removeEventListener("focusin", focus);
      window.removeEventListener("focusout", hide);
      window.removeEventListener("pointerdown", hide);
      window.removeEventListener("keydown", key);
      window.removeEventListener("scroll", hide, true);
      window.removeEventListener("resize", hide);
      window.clearTimeout(timer.current);
    };
  }, [id]);

  // 元素被移出页面（列表刷新、路由切换）时提示跟着收起
  useEffect(() => {
    if (!tip) return;
    const check = window.setInterval(() => { if (!tip.el.isConnected) { current.current = null; setTip(null); } }, 250);
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
