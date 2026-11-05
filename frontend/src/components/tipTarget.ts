// 提示层借用 title 的规则：提示显示期间暂时移除原生 title，没有可见文字的元素临时补 aria-label，收起时原样放回。

/** 借用的 title：收起时放回；为没有可见文字的元素临时补的 aria-label 一并撤掉。 */
export type Borrowed = { title: string; label: boolean };
export type Tip = { el: HTMLElement; text: string; borrowed?: Borrowed; seq: number };
let seq = 0;

export function tipTarget(node: EventTarget | null): HTMLElement | null {
  if (!(node instanceof Element)) return null;
  const el = node.closest<HTMLElement>("[data-tip], [title]");
  if (!el || el instanceof SVGElement) return null;
  return (el.dataset.tip ?? el.getAttribute("title") ?? "").trim() ? el : null;
}

export function borrow(el: HTMLElement): Tip {
  const tip = el.dataset.tip?.trim();
  if (tip) return { el, text: tip, seq: ++seq };
  const title = el.getAttribute("title") ?? "";
  // 只看渲染出来的文字：被 display: none 藏起的标签（如收起侧边栏的导航文字）不提供可访问名称，
  // 这时名称来自 title，借走 title 期间要临时补 aria-label，否则读屏器读不到名称（#122）。
  const visible = (el.innerText ?? el.textContent ?? "").trim();
  const label = !el.hasAttribute("aria-label") && !visible;
  el.removeAttribute("title");
  if (label) el.setAttribute("aria-label", title);
  return { el, text: title, borrowed: { title, label }, seq: ++seq };
}

export function giveBack(tip: Tip) {
  tip.el.removeAttribute("aria-describedby");
  if (!tip.borrowed) return;
  // 显示期间 React 写入了新的 title：以它为准，不覆盖
  if (!tip.el.hasAttribute("title")) tip.el.setAttribute("title", tip.borrowed.title);
  if (tip.borrowed.label && tip.el.getAttribute("aria-label") === tip.borrowed.title) tip.el.removeAttribute("aria-label");
}
