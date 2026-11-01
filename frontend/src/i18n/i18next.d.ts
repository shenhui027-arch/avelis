// 键以简体中文语言包为准：写错键名或漏了插值参数时 tsc 报错。
import "i18next";

import type zhCN from "./locales/zh-CN.json";

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: { translation: typeof zhCN };
    returnNull: false;
  }
}
