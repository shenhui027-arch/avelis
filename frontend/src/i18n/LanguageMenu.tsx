import { Icon } from "../components/Icon";
import { Select } from "../components/controls";
import { LANGUAGES, setLanguage, t, useLanguage } from ".";

/** 用户菜单里的语言分组：与主题分组同样的单选项。 */
export function LanguageMenuGroup() {
  const lang = useLanguage();
  return (
    <div className="av-menu__group" role="group" aria-label={t("语言")}>
      {LANGUAGES.map((l) => (
        <button key={l.code} type="button" role="menuitemradio" aria-checked={lang === l.code} lang={l.code}
          className="av-menu__item" onClick={() => setLanguage(l.code)}>
          <Icon name={lang === l.code ? "check" : "globe"} />{l.label}
        </button>
      ))}
    </div>
  );
}

/** 登录页右上角的语言选择：与其他下拉同一个 Select 组件，键盘与读屏都可用。 */
export function LanguageSelect({ className }: { className?: string }) {
  const lang = useLanguage();
  return (
    <span className={className}>
      <Icon name="globe" small />
      <Select label={t("语言")} value={lang} style={{ width: "auto" }} onChange={(v) => setLanguage(v as typeof lang)}>
        {LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
      </Select>
    </span>
  );
}
