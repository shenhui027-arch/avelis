// 品牌标记“汇流 A”：两条棱面汇成字母 A，横梁是一条流过的数据线（赭石），顶点是答案连着的依据节点。
// 实色、无辉光：颜色取自设计令牌，深浅主题自动适配。20px 以下去掉横梁末端节点。
// 装饰性图形对读屏隐藏，可访问名称由外层提供。

export function LogoMark({ size = 20 }: { size?: number }) {
  const small = size < 20;
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false" className="av-logo__mark">
      <path d="M32 6 L6 57 H20.5 L32 31 Z" fill="var(--accent)" />
      <path d="M32 6 L32 31 L43.5 57 H58 Z" fill="var(--text-primary)" opacity=".86" />
      <rect x="16" y="40.5" width={small ? 32 : 36} height="4.5" rx="2.25" fill="var(--flow)" />
      {small ? null : <circle cx="56" cy="42.75" r="3.2" fill="var(--flow)" />}
      <circle cx="32" cy="8.5" r={small ? 4.6 : 4.2} fill="var(--flow)" />
    </svg>
  );
}

/** 标记 + 字标，用于侧边栏与登录页。 */
export function Logo({ size = 28 }: { size?: number }) {
  return (
    <span className="av-logo">
      <LogoMark size={size} />
      <b>Avelis</b>
    </span>
  );
}
