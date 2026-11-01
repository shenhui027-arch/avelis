import { Fragment, type ReactNode } from "react";

import { useLanguage } from ".";

/** 语言变了就重新挂载整棵界面：模块级的标签表、工具函数里的 t() 都会按新语言重新取值。查询缓存不受影响。 */
export function LanguageRoot({ children }: { children: ReactNode }) {
  const lang = useLanguage();
  return <Fragment key={lang}>{children}</Fragment>;
}
