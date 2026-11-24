// LIFO 浮层栈：模态框、抽屉、下拉、日历与弹出菜单共用一个 Escape 栈，保证按 Esc 只关最顶层。
type Entry = { id: symbol; onEscape: () => void };

const stack: Entry[] = [];
let attached = false;

export function handleOverlayKeyDown(e: Pick<KeyboardEvent, "defaultPrevented" | "key" | "isComposing" | "keyCode" | "preventDefault" | "stopPropagation">) {
  if (e.defaultPrevented || e.key !== "Escape" || e.isComposing || e.keyCode === 229 || !stack.length) return;
  e.preventDefault();
  e.stopPropagation();
  stack[stack.length - 1].onEscape();
}

function syncListener() {
  if (typeof window === "undefined") return;
  if (stack.length > 0 && !attached) {
    window.addEventListener("keydown", handleOverlayKeyDown);
    attached = true;
  } else if (stack.length === 0 && attached) {
    window.removeEventListener("keydown", handleOverlayKeyDown);
    attached = false;
  }
}

export function pushOverlay(onEscape: () => void): { id: symbol; release: () => void } {
  const id = Symbol("overlay");
  stack.push({ id, onEscape });
  syncListener();
  let released = false;
  return {
    id,
    release: () => {
      if (released) return;
      released = true;
      const idx = stack.findIndex((item) => item.id === id);
      if (idx >= 0) stack.splice(idx, 1);
      syncListener();
    },
  };
}

export function hasOverlayAbove(id: symbol | null | undefined): boolean {
  if (!id) return false;
  const idx = stack.findIndex((item) => item.id === id);
  return idx >= 0 && idx < stack.length - 1;
}

export function isTopOverlay(id: symbol | null | undefined): boolean {
  if (!id || !stack.length) return false;
  return stack[stack.length - 1].id === id;
}

export function overlayCount(): number {
  return stack.length;
}
