import { useId } from "react";

// 品牌标记“棱镜 A”：两块渐变棱面拼成字母 A，顶端是发光的依据节点；横梁是一条向外流出的数据线，
// 末端的节点代表“答案连着依据”。20px 以下去掉辉光与外流节点，只留两块棱面和横梁。
// 装饰性图形对读屏隐藏，可访问名称由外层提供。

export function LogoMark({ size = 20 }: { size?: number }) {
  const id = useId().replace(/[^\w-]/g, "");
  const small = size < 20;
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden="true" focusable="false" className="av-logo__mark">
      <defs>
        <linearGradient id={`${id}l`} x1="10" y1="56" x2="32" y2="6" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#3d6cf0" /><stop offset="1" stopColor="#8b7bff" />
        </linearGradient>
        <linearGradient id={`${id}r`} x1="54" y1="56" x2="32" y2="6" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#22d3ee" /><stop offset="1" stopColor="#8b7bff" />
        </linearGradient>
        <linearGradient id={`${id}b`} x1="16" y1="0" x2="58" y2="0" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#7b6bff" stopOpacity=".2" /><stop offset=".45" stopColor="#22d3ee" /><stop offset="1" stopColor="#a5f3fc" />
        </linearGradient>
        <radialGradient id={`${id}n`} cx=".4" cy=".35" r=".7">
          <stop offset="0" stopColor="#ffffff" /><stop offset=".45" stopColor="#c7bfff" /><stop offset="1" stopColor="#7b6bff" />
        </radialGradient>
        {small ? null : <filter id={`${id}g`} x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.2" result="b" /><feMerge><feMergeNode in="b" /><feMergeNode in="SourceGraphic" /></feMerge>
        </filter>}
      </defs>
      <path d="M32 6 L6 57 H20.5 L32 31 Z" fill={`url(#${id}l)`} />
      <path d="M32 6 L32 31 L43.5 57 H58 Z" fill={`url(#${id}r)`} />
      <path d="M32 6 L32 31 L43.5 57 H58 Z" fill="#000" opacity=".12" />
      <rect x="16" y="40.5" width={small ? 32 : 38} height="4.5" rx="2.25" fill={`url(#${id}b)`} filter={small ? undefined : `url(#${id}g)`} />
      {small ? null : <circle cx="56.5" cy="42.75" r="3.4" fill="#a5f3fc" filter={`url(#${id}g)`} />}
      <circle cx="32" cy="8.5" r={small ? 4.6 : 4.2} fill={`url(#${id}n)`} filter={small ? undefined : `url(#${id}g)`} />
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
