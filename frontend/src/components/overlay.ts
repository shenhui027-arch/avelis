// 统一浮层、菜单键盘导航与弹出层管理：M2A.2（供 Drawer、ConfirmDialog、controls、菜单与 M2B 命令面板共用）。
import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type RefObject } from "react";

import { hasOverlayAbove, isTopOverlay, overlayCount, pushOverlay } from "./overlayStack";

export { hasOverlayAbove, isTopOverlay, overlayCount, pushOverlay };

const MENU_ITEM_SELECTOR = '[role="menuitem"], [role="menuitemradio"], [role="menuitemcheckbox"], [role="radio"]';

export function getEnabledMenuItems(menuEl: HTMLElement | null): HTMLElement[] {
  if (!menuEl) return [];
  return Array.from(menuEl.querySelectorAll<HTMLElement>(MENU_ITEM_SELECTOR)).filter(
    (el) => !el.hasAttribute("disabled") && el.getAttribute("aria-disabled") !== "true",
  );
}

/** 菜单内方向键 / Home / End 循环导航，Tab 收起菜单（不干扰菜单内嵌的其他控件，如 DateInput）。 */
export function handleMenuKeyDown(
  e: ReactKeyboardEvent<HTMLElement>,
  menuEl: HTMLElement | null,
  onTabOut?: () => void,
): boolean {
  if (e.nativeEvent.isComposing || e.keyCode === 229 || !menuEl) return false;
  const target = e.target as HTMLElement | null;
  const isMenuSurface = target === menuEl || !!target?.closest(MENU_ITEM_SELECTOR);
  if (!isMenuSurface) return false;

  if (e.key === "Tab") {
    if (onTabOut) {
      if ("inert" in menuEl) menuEl.inert = true;
      onTabOut();
    }
    return false;
  }

  const items = getEnabledMenuItems(menuEl);
  if (!items.length) return false;
  const current = target?.closest<HTMLElement>(MENU_ITEM_SELECTOR) ?? null;
  const idx = current ? items.indexOf(current) : -1;

  let next = -1;
  if (e.key === "ArrowDown") next = idx >= 0 ? (idx + 1) % items.length : 0;
  else if (e.key === "ArrowUp") next = idx >= 0 ? (idx - 1 + items.length) % items.length : items.length - 1;
  else if (e.key === "Home") next = 0;
  else if (e.key === "End") next = items.length - 1;

  if (next < 0) return false;
  e.preventDefault();
  e.stopPropagation();
  items[next].focus();
  return true;
}

/** 将激活的浮层注册到 LIFO 栈中；用 ref 保存回调，避免回调变化导致栈顺序重排。 */
export function useOverlay(active: boolean, onEscape: () => void): RefObject<symbol | null> {
  const escapeRef = useRef(onEscape);
  escapeRef.current = onEscape;
  const idRef = useRef<symbol | null>(null);
  useEffect(() => {
    if (!active) {
      idRef.current = null;
      return;
    }
    const handle = pushOverlay(() => escapeRef.current());
    idRef.current = handle.id;
    return () => {
      if (idRef.current === handle.id) idRef.current = null;
      handle.release();
    };
  }, [active]);
  return idRef;
}

export type PopoverOptions = {
  /** 打开时优先聚焦已选中的 menuitemradio / radio，而不是第一项。 */
  focusChecked?: boolean;
  /** 菜单内包含额外交互区（如 ChipMenu 嵌 DateInput）时，不阻止 Tab 移入额外区域。 */
  tabClosesMenu?: boolean;
};

/** 统一的弹出层与菜单 Hook：管理开关、浮层栈 Esc 焦点归还、点外部关闭、打开聚焦首项与方向键导航。 */
export function usePopover(options: PopoverOptions = {}) {
  const { focusChecked = false, tabClosesMenu = true } = options;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const initialFocus = useRef<"first" | "last">("first");

  const overlayId = useOverlay(open, () => {
    setOpen(false);
    triggerRef.current?.focus();
  });

  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      if (hasOverlayAbove(overlayId.current)) return;
      const target = e.target as Node | null;
      const inside = !!ref.current?.contains(target) || !!(target as Element | null)?.closest?.(".av-float-root");
      if (!inside) setOpen(false);
    };
    window.addEventListener("mousedown", onMouseDown);
    return () => window.removeEventListener("mousedown", onMouseDown);
  }, [open, overlayId]);

  useEffect(() => {
    if (!open) {
      initialFocus.current = "first";
      return;
    }
    const menuEl = menuRef.current;
    if (!menuEl) return;
    if ("inert" in menuEl) menuEl.inert = false;
    const items = getEnabledMenuItems(menuEl);
    if (!items.length) {
      menuEl.focus();
      return;
    }
    const checked = focusChecked ? items.find((el) => el.getAttribute("aria-checked") === "true") : undefined;
    const target = initialFocus.current === "last" ? items[items.length - 1] : (checked ?? items[0]);
    target?.focus();
  }, [open, focusChecked]);

  const onTriggerKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      initialFocus.current = e.key === "ArrowUp" ? "last" : "first";
      setOpen(true);
    }
  };

  const onMenuKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    handleMenuKeyDown(
      e,
      menuRef.current,
      tabClosesMenu
        ? () => {
            triggerRef.current?.focus();
            setOpen(false);
          }
        : undefined,
    );
  };

  return { open, setOpen, ref, triggerRef, menuRef, overlayId, onTriggerKeyDown, onMenuKeyDown };
}
