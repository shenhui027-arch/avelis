import type { KeyboardEvent } from "react";

export function isImeComposing(e: KeyboardEvent<HTMLElement> | globalThis.KeyboardEvent): boolean {
  const native = "nativeEvent" in e ? e.nativeEvent : e;
  return !!(native.isComposing || native.keyCode === 229);
}

export function submitOnEnter(e: KeyboardEvent<HTMLElement>, mode: "mod-enter" | "enter"): boolean {
  if (isImeComposing(e) || e.key !== "Enter") return false;
  if (mode === "mod-enter") return e.metaKey || e.ctrlKey;
  return !e.shiftKey;
}

export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to legacy textarea fallback
    }
  }
  if (typeof document === "undefined") return false;
  const el = document.createElement("textarea");
  el.value = text;
  el.setAttribute("readonly", "");
  el.style.position = "fixed";
  el.style.opacity = "0";
  document.body.appendChild(el);
  el.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  } finally {
    el.remove();
  }
  return ok;
}
