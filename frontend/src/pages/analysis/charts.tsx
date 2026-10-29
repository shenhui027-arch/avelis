// 分析页的两种图：趋势（折线）与贡献（以 0 为中线的横向条）。内联 SVG，颜色取设计令牌。
const PALETTE = ["var(--accent)", "var(--danger)", "var(--ok)", "var(--warn)", "#8E4EC6", "#12A594"];

function fmt(n: number): string {
  const abs = Math.abs(n);
  if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(n / 1e4).toFixed(1)}万`;
  return Number.isInteger(n) ? String(n) : n.toFixed(2);
}

export function BarChart({ bars, label }: { bars: { label: string; value: number }[]; label: string }) {
  const width = 320, row = 22, left = 112, top = 18;
  const height = top + bars.length * row + 8;
  const max = Math.max(1e-9, ...bars.map((b) => Math.abs(b.value)));
  const hasNeg = bars.some((b) => b.value < 0);
  const plot = width - left - 8;
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
            <text x={left - 6} y={y + 13} textAnchor="end" className="av-chart__tick">{b.label.slice(0, 10)}</text>
            <rect x={b.value < 0 ? zero - w : zero} y={y + 3} width={Math.max(w, 1)} height={row - 8} rx={2}
              fill={b.value < 0 ? "var(--danger)" : "var(--accent)"} opacity={0.85} />
            <text x={b.value < 0 ? zero - w - 4 : zero + w + 4} y={y + 13} textAnchor={b.value < 0 ? "end" : "start"}
              className="av-chart__value">{fmt(b.value)}</text>
          </g>
        );
      })}
    </svg>
  );
}

export function LineChart({ series, label }: { series: { name: string; points: { x: string; y: number }[] }[]; label: string }) {
  const width = 320, height = 160, left = 40, right = 8, top = 22, bottom = 22;
  const xs = [...new Set(series.flatMap((s) => s.points.map((p) => p.x)))].sort();
  const ys = series.flatMap((s) => s.points.map((p) => p.y));
  const lo = Math.min(0, ...ys), hi = Math.max(...ys, 1e-9);
  const x = (v: string) => left + (xs.length > 1 ? (xs.indexOf(v) / (xs.length - 1)) * (width - left - right) : 0);
  const y = (v: number) => top + (1 - (v - lo) / (hi - lo || 1)) * (height - top - bottom);
  const ticks = xs.filter((_, i) => i % Math.max(1, Math.ceil(xs.length / 5)) === 0);
  return (
    <svg className="av-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${label}趋势图`}>
      <text x={left} y={11} className="av-chart__label">{label}</text>
      {series.map((s, i) => <text key={s.name} x={left + 70 + i * 62} y={11} className="av-chart__tick" fill={PALETTE[i % PALETTE.length]}>{s.name.slice(0, 6)}</text>)}
      <line x1={left} x2={width - right} y1={height - bottom} y2={height - bottom} className="av-chart__axis" />
      <text x={left - 4} y={y(hi) + 4} textAnchor="end" className="av-chart__tick">{fmt(hi)}</text>
      <text x={left - 4} y={y(lo) + 4} textAnchor="end" className="av-chart__tick">{fmt(lo)}</text>
      {ticks.map((t) => <text key={t} x={x(t)} y={height - 6} textAnchor="middle" className="av-chart__tick">{t.slice(5)}</text>)}
      {series.map((s, i) => (
        <polyline key={s.name} fill="none" stroke={PALETTE[i % PALETTE.length]} strokeWidth={1.6}
          points={[...s.points].sort((a, b) => a.x.localeCompare(b.x)).map((p) => `${x(p.x)},${y(p.y)}`).join(" ")} />
      ))}
    </svg>
  );
}
