// Avelis UI 线性图标：16px 网格、1.5px 描边、圆角端点，颜色随 currentColor。
const PATHS: Record<string, string> = {
  home: "M2.5 7.5 8 3l5.5 4.5V13a.5.5 0 0 1-.5.5H10V10H6v3.5H3a.5.5 0 0 1-.5-.5z",
  lineage: "M4 4.5h3m2 7h3M4 4.5a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zm11 7a1.5 1.5 0 1 1-3 0 1.5 1.5 0 0 1 3 0zM7 4.5c2.5 0 2 7 4.5 7",
  catalog: "M3 3.5h10M3 8h10M3 12.5h6",
  change: "M3 5h8.5L9 2.5M13 11H4.5L7 13.5",
  ask: "M3 3.5h10v7H7l-3 2.5v-2.5H3z",
  settings: "M8 10a2 2 0 1 0 0-4 2 2 0 0 0 0 4zm5-2 1.2-.7-1-1.8-1.3.4a4.5 4.5 0 0 0-1-.6L10.5 4h-2l-.4 1.3a4.5 4.5 0 0 0-1 .6l-1.3-.4-1 1.8L6 8l-1.2.7 1 1.8 1.3-.4a4.5 4.5 0 0 0 1 .6l.4 1.3h2l.4-1.3a4.5 4.5 0 0 0 1-.6l1.3.4 1-1.8z",
  plus: "M8 3v10M3 8h10",
  refresh: "M13 8a5 5 0 1 1-1.5-3.5M13 3v2.5h-2.5",
  check: "M3.5 8.5 6.5 11.5 12.5 4.5",
  key: "M10 6.5a2.5 2.5 0 1 1-5 0 2.5 2.5 0 0 1 5 0zM8.5 8.5 13 13m-2-2 1.5-1.5",
  chip: "M5 5h6v6H5zM6.5 2.5v2.5m3-2.5v2.5m-3 6v2.5m3-2.5v2.5M2.5 6.5H5m-2.5 3H5m6-3h2.5m-2.5 3h2.5",
  chevron: "M5.5 6.5 8 9l2.5-2.5",
  trash: "M3.5 4.5h9M6.5 4.5V3h3v1.5m-5 0 .5 8.5h5l.5-8.5",
  logout: "M9.5 3H12.5v10H9.5M6.5 5 3.5 8l3 3M3.5 8h6",
  info: "M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zm0-3.5V7.5m0-2v-.01",
  shield: "M8 2.5 13 4.5v3.5c0 3-2.2 4.8-5 5.5-2.8-.7-5-2.5-5-5.5V4.5z",
};

export function Icon({ name, small, title }: { name: keyof typeof PATHS | string; small?: boolean; title?: string }) {
  return (
    <svg className={small ? "av-icon av-icon--sm" : "av-icon"} viewBox="0 0 16 16" aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}>
      {title ? <title>{title}</title> : null}
      <path d={PATHS[name] ?? PATHS.info} />
    </svg>
  );
}

/** ⓘ 提示：解释收进提示里，不在页面上写说明段落。 */
export function Hint({ text }: { text: string }) {
  return (
    <span className="av-hint" title={text} aria-label={text} tabIndex={0}>
      <Icon name="info" small />
    </span>
  );
}
