import { Fragment, type ReactNode } from "react";

import { useLanguage } from ".";

/** 只在拿不到本机存储、切换语言没法整页重新加载时起作用：按语言重新挂载界面。 */
export function LanguageRoot({ children }: { children: ReactNode }) {
  const lang = useLanguage();
  return <Fragment key={lang}>{children}</Fragment>;
}
