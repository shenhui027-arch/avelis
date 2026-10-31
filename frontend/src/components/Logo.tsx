import { useId } from "react";

// 品牌标记“汇流”：两路上游汇入一个发光节点，组成字母 A；横线上的光点是正在流动的数据（规范见 design/brand/README.md）。
// 24px 以下去掉横线与光点，只留两条腿和顶点。装饰性图形对读屏隐藏，可访问名称由外层提供。

export function LogoMark({ size = 20 }: { size?: number }) {
  const id = useId();
  const small = size < 24;
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={`${id}l`} x1="14" y1="50" x2="30" y2="14" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#3d6cf0" /><stop offset="1" stopColor="#7b6bff" />
        </linearGradient>
        <linearGradient id={`${id}r`} x1="50" y1="50" x2="34" y2="14" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#22d3ee" /><stop offset="1" stopColor="#7b6bff" />
        </linearGradient>
        <radialGradient id={`${id}n`} cx=".36" cy=".34" r=".75">
          <stop offset="0" stopColor="#ffffff" /><stop offset=".32" stopColor="#b3a8ff" /><stop offset="1" stopColor="#6d5cff" />
        </radialGradient>
      </defs>
      <path d="M16.5 45 C19.5 35 23.5 24.5 28.4 17.8" fill="none" stroke={`url(#${id}l)`}
        strokeWidth={small ? 7 : 5.6} strokeLinecap="round" />
      <path d="M47.5 45 C44.5 35 40.5 24.5 35.6 17.8" fill="none" stroke={`url(#${id}r)`}
        strokeWidth={small ? 7 : 5.6} strokeLinecap="round" />
      {small ? null : <circle cx="32" cy="37.5" r="2.3" fill="#22d3ee" />}
      <circle cx="14" cy="51.5" r={small ? 4.6 : 4.1} fill="none" stroke="#3d6cf0" strokeWidth={small ? 3.4 : 2.6} />
      <circle cx="50" cy="51.5" r={small ? 4.6 : 4.1} fill="none" stroke="#22d3ee" strokeWidth={small ? 3.4 : 2.6} />
      <circle cx="32" cy="13" r={small ? 7.4 : 6.6} fill={`url(#${id}n)`} />
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
