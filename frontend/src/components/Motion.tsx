// 动效小件（Motion）：首屏错落进入、数字补间。只回应“进入了什么、哪个数变了”，不做循环装饰。
// 全站一条缓动曲线，与 motion.css 的 --ease-out 一致；MotionConfig reducedMotion="user" 时自动关闭位移。
import { animate, motion, useReducedMotion, type Variants } from "motion/react";
import { Link } from "react-router";
import { useEffect, useRef, type ComponentProps, type ReactNode } from "react";

export const EASE = [0.22, 1, 0.36, 1] as const;

const group: Variants = { hidden: {}, show: { transition: { staggerChildren: 0.05, delayChildren: 0.03 } } };
const piece: Variants = { hidden: { opacity: 0, y: 10 }, show: { opacity: 1, y: 0, transition: { duration: 0.42, ease: EASE } } };

/** 一组错落进入的内容：子元素用 Reveal（或 motion 组件加 variants={revealItem}）。最多五六项，只在首次出现时播放。 */
export function RevealGroup({ className, children, as = "div", ...rest }: {
  className?: string; children: ReactNode; as?: "div" | "section" | "nav"; "aria-label"?: string;
}) {
  const Tag = motion[as];
  return <Tag className={className} variants={group} initial="hidden" animate="show" {...rest}>{children}</Tag>;
}

export const revealItem = piece;

export function Reveal({ className, children }: { className?: string; children: ReactNode }) {
  return <motion.div className={className} variants={piece}>{children}</motion.div>;
}

export const MotionLink = motion.create(Link);

/** 数字从上一个值补间到新值（首次从 0 开始）；不是纯数字时原样显示。 */
export function CountUp({ value, ...rest }: { value: string | number } & ComponentProps<"span">) {
  const ref = useRef<HTMLSpanElement>(null);
  const from = useRef(0);
  const reduce = useReducedMotion();
  const text = String(value);
  const target = /^-?\d+(\.\d+)?$/.test(text.trim()) ? Number(text) : null;
  const decimals = target === null ? 0 : (text.split(".")[1]?.length ?? 0);
  useEffect(() => {
    const el = ref.current;
    if (!el || target === null) return;
    const show = (v: number) => { el.textContent = v.toFixed(decimals); };
    if (reduce) { show(target); from.current = target; return; }
    // from 记录屏幕上正在显示的值：效果被中断重跑（StrictMode、数值又变）时从当前显示处接着补间
    const controls = animate(from.current, target, { duration: 0.8, ease: EASE, onUpdate: (v) => { show(v); from.current = v; } });
    return () => controls.stop();
  }, [target, decimals, reduce]);
  return <span ref={ref} {...rest}>{target === null ? text : (0).toFixed(decimals)}</span>;
}
