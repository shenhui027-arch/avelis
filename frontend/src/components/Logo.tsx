// 品牌标记“有依据的 A”：A 的轮廓 + 通往方形依据节点的支路（设计稿与规范见 design/brand/README.md）。
// 单色版继承 currentColor，随深浅主题着色；装饰性图形对读屏隐藏，可访问名称由外层提供。

export function LogoMark({ size = 20 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M14 5h4l10 22h-4.5L16 10.5 8.5 27H4Z" />
      <path d="M10 19h5v2h-5Z" />
      <path d="M13 18h4v4h-4Z" />
    </svg>
  );
}
