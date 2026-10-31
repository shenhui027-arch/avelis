// 分析页的两种图：趋势（折线）与贡献（以 0 为中线的横向条）。内联 SVG，颜色取设计令牌。
import { useNumberStyle } from "../../api/hooks";

const PALETTE = ["var(--accent)", "var(--danger)", "var(--ok)", "var(--warn)", "#8E4EC6", "#12A594"];

// 与后端 grounding.compact 一致（ADR 0014）：两种体系都从绝对值 1 万起缩写；cn 用万、亿，intl 用 K、M、B
function fmt(n: number, style: "cn" | "intl" = "cn"): string {
  const abs = Math.abs(n);
  const scales: [number, string][] = style === "intl" ? [[1e9, "B"], [1e6, "M"], [1e3, "K"]] : [[1e8, "亿"], [1e4, "万"]];
  if (abs >= 1e4) for (const [scale, suffix] of scales) if (abs >= scale) return `${(n / scale).toFixed(scale >= 1e8 ? 2 : 1)}${suffix}`;
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

export function BarChart({ bars, label }: { bars: { label: string; value: number }[]; label: string }) {
  const style = useNumberStyle();
  const width = 340, row = 22, left = 84, right = 56, top = 18;
  const height = top + bars.length * row + 8;
  const max = Math.max(1e-9, ...bars.map((b) => Math.abs(b.value)));
  const hasNeg = bars.some((b) => b.value < 0);
  const plot = width - left - right;
  const zero = left + (hasNeg ? plot / 2 : 0);
  const scale = (hasNeg ? plot / 2 : plot) / max;
  return (
    <svg className="av-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label}条形图`}>
      <text x={left} y={11} className="av-chart__label">{label}</text>
      <line x1={zero} x2={zero} y1={top - 4} y2={height - 6} className="av-chart__axis" />
      {bars.map((b, i) => {
        const w = Math.abs(b.value) * scale;
        const y = top + i * row;
        return (
          <g key={i}>
            <text x={left - 6} y={y + 13} textAnchor="end" className="av-chart__tick">{b.label.slice(0, 8)}</text>
            <rect x={b.value < 0 ? zero - w : zero} y={y + 3} width={Math.max(w, 1)} height={row - 8} rx={2}
              fill={b.value < 0 ? "var(--danger)" : "var(--accent)"} opacity={0.85} />
            <text x={width - 2} y={y + 13} textAnchor="end" className="av-chart__value">{fmt(b.value, style)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function LineChart({ series, label }: { series: { name: string; points: { x: string; y: number }[] }[]; label: string }) {
  const style = useNumberStyle();
  const width = 320, height = 160, left = 40, right = 8, top = 22, bottom = 22;
  const xs = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort();
  const ys = series.flatMap((s) => s.points.map((p) => p.y));
  const lo = Math.min(0, ...ys), hi = Math.max(...ys, 1e-9);
  const x = (v: string) => left + (xs.length > 1 ? (xs.indexOf(v) / (xs.length - 1)) * (width - left - right) : 0);
  const y = (v: number) => top + (1 - (v - lo) / (hi - lo || 1)) * (height - top - bottom);
  const ticks = xs.filter((_, i) => i % Math.max(1, Math.ceil(xs.length / 5)) === 0);
  // 同一年内省略年份；跨年或非“年-…”形式的期间（如周、季编码）原样显示
  const sameYear = xs.every((v) => /^\d{4}-/.test(v)) && new Set(xs.map((v) => v.slice(0, 4))).size === 1;
  const tick = (v: string) => (sameYear ? v.slice(5) : v);
  return (
    <svg className="av-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label}趋势图`}>
      <text x={left} y={11} className="av-chart__label">{label}</text>
      {series.map((s, i) => <text key={s.name} x={left + 70 + i * 62} y={11} className="av-chart__tick" fill={PALETTE[i % PALETTE.length]}>{s.name.slice(0, 6)}</text>)}
      <line x1={left} x2={width - right} y1={height - bottom} y2={height - bottom} className="av-chart__axis" />
      <text x={left - 4} y={y(hi) + 4} textAnchor="end" className="av-chart__tick">{fmt(hi, style)}</text>
      <text x={left - 4} y={y(lo) + 4} textAnchor="end" className="av-chart__tick">{fmt(lo, style)}</text>
      {ticks.map((t) => <text key={t} x={x(t)} y={height - 6} textAnchor="middle" className="av-chart__tick">{tick(t)}</text>)}
      {series.map((s, i) => (
        <polyline key={s.name} fill="none" stroke={PALETTE[i % PALETTE.length]} strokeWidth={1.6}
          points={[...s.points].sort((a, b) => a.x.localeCompare(b.x)).map((p) => `${x(p.x)},${y(p.y)}`).join(" ")} />
      ))}
    </svg>
  );
}
