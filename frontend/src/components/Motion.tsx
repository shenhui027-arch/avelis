// 动效小件（Motion）：首屏错落进入、数字补间。只回应“进入了什么、哪个数变了”，不做循环装饰。
// 全站一条缓动曲线，与 motion.css 的 --ease-out 一致；MotionConfig reducedMotion="user" 时自动关闭位移。
import NumberFlow from "@number-flow/react";
import { motion, type Variants } from "motion/react";
import { Link } from "react-router";
import { useEffect, useState, type ComponentProps, type ReactNode } from "react";

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

/** 数字变化时逐位滚动（@number-flow/react）；不是纯数字时原样显示。减少动态效果时由库自行关闭动画。 */
export function CountUp({ value, ...rest }: { value: string | number } & ComponentProps<"span">) {
  const text = String(value).trim();
  const target = /^-?\d+(\.\d+)?$/.test(text) ? Number(text) : null;
  if (target === null) return <span {...rest}>{text}</span>;
  return <Rolling target={target} decimals={text.split(".")[1]?.length ?? 0} {...rest} />;
}

/** 首次挂载从 0 滚到目标值，之后每次变化从旧值滚到新值。 */
function Rolling({ target, decimals, ...rest }: { target: number; decimals: number } & ComponentProps<"span">) {
  const [shown, setShown] = useState(0);
  useEffect(() => { const id = requestAnimationFrame(() => setShown(target)); return () => cancelAnimationFrame(id); }, [target]);
  return <span {...rest}><NumberFlow value={shown} format={{ minimumFractionDigits: decimals, maximumFractionDigits: decimals, useGrouping: false }}
    transformTiming={{ duration: 700, easing: "cubic-bezier(.22, 1, .36, 1)" }} spinTiming={{ duration: 700, easing: "cubic-bezier(.22, 1, .36, 1)" }} /></span>;
}
