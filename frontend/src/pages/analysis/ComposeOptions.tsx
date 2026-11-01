// 输入框底栏的选项胶囊：图标 + 当前值 + 下拉箭头，点开是带说明的选项菜单（与模型选择同一套样式），不用浏览器原生下拉。
import type { ReactNode } from "react";

import { Icon } from "../../components/Icon";
import { usePopover } from "./ModelPicker";

export type ChipOption<T extends string> = { value: T; label: string; hint?: string };

export function ChipMenu<T extends string>({ icon, title, value, display, options, onChange, keepOpenFor, down, children }: {
  icon: string; title: string; value: T; display?: string; options: ChipOption<T>[];
  onChange: (value: T) => void; keepOpenFor?: T; down?: boolean; children?: ReactNode;
}) {
  const { open, setOpen, ref } = usePopover();
  const current = options.find((o) => o.value === value);
  return (
    <div className="av-mbar av-chip" ref={ref}>
      <button type="button" className="av-chip__btn" aria-haspopup="menu" aria-expanded={open} title={title}
        onClick={() => setOpen(!open)}>
        <Icon name={icon} small />
        <span className="av-chip__value">{display ?? current?.label}</span>
        <Icon name="chevron" small />
      </button>
      {open ? (
        <div className={`av-menu ${down ? "av-chip__menu--down" : "av-menu--up"} av-mbar__menu av-chip__menu`} role="menu" aria-label={title}>
          <div className="av-mbar__sec">{title}</div>
          {options.map((o) => (
            <button key={o.value} type="button" role="menuitemradio" aria-checked={o.value === value}
              className="av-menu__item av-chip__item"
              onClick={() => { onChange(o.value); if (o.value !== keepOpenFor) setOpen(false); }}>
              <span className="av-mbar__check">{o.value === value ? <Icon name="check" /> : null}</span>
              <span className="av-chip__text"><b>{o.label}</b>{o.hint ? <small>{o.hint}</small> : null}</span>
            </button>
          ))}
          {children ? <div className="av-chip__extra">{children}</div> : null}
        </div>
      ) : null}
    </div>
  );
}
