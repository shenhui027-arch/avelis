// 输入框底栏的模型、思考强度与上下文（参照 Claude Code：只写模型名与档位，上下文是一个小圆环）。
// 点模型名或档位打开同一个菜单；点圆环看用量，运行中可以手动压缩上下文。选择记在本机，下次默认沿用。
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useAction } from "../../api/hooks";
import { Icon } from "../../components/Icon";

type Run = Schemas["AnalysisRunOut"];
type Effort = { value: string; label: string };
export type AnalysisModel = { provider_id: string; provider_name: string; model_id: string; efforts: Effort[];
  context_window: number | null; context_window_source?: string | null };
export type ModelChoice = { provider_id: string; model_id: string; reasoning_effort: string | null };
type ModelList = { models: AnalysisModel[]; default: ModelChoice | null };

const KEY = "avelis.analysis.model";

export const useAnalysisModels = () => useQuery({ queryKey: ["analysis-models"], staleTime: 60_000,
  queryFn: () => unwrap(api.GET("/api/analysis/models")) as Promise<ModelList> });

function readSaved(): ModelChoice | null {
  try {
    return JSON.parse(localStorage.getItem(KEY) ?? "null");
  } catch {
    return null;
  }
}

/** 当前选择：传入的（续问沿用上一次）或本机记住的优先，失效时回到默认；body 是请求体里要带的字段。 */
export function useModelChoice(initial?: ModelChoice | null) {
  const list = useAnalysisModels();
  const [saved, setSaved] = useState<ModelChoice | null>(() => initial ?? readSaved());
  const models = list.data?.models ?? [];
  const find = (c: ModelChoice | null) => c && models.find((m) => m.provider_id === c.provider_id && m.model_id === c.model_id);
  const model = find(saved) ?? find(list.data?.default ?? null) ?? models[0];
  const preferred = find(saved) ? saved?.reasoning_effort : list.data?.default?.reasoning_effort;
  const fallback = list.data?.default?.reasoning_effort;
  const effort = model?.efforts.some((e) => e.value === preferred) ? preferred ?? null
    : model?.efforts.some((e) => e.value === fallback) ? fallback ?? null : null;
  const choice: ModelChoice | null = model ? { provider_id: model.provider_id, model_id: model.model_id, reasoning_effort: effort } : null;
  const set = (next: ModelChoice) => {
    setSaved(next);
    try {
      localStorage.setItem(KEY, JSON.stringify(next));
    } catch {
      // 拿不到存储时只在本次生效
    }
  };
  return { models, model, choice, set,
    body: choice ? { model: { provider_id: choice.provider_id, model_id: choice.model_id }, reasoning_effort: choice.reasoning_effort } : {} };
}

/** 点外部或按 Esc 关闭的弹出层。 */
function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", close);
    window.addEventListener("keydown", close);
    return () => { window.removeEventListener("mousedown", close); window.removeEventListener("keydown", close); };
  }, [open]);
  return { open, setOpen, ref };
}

/** 模型名与思考档位：一个轻量文字按钮，打开模型与档位菜单。没有可选模型时不显示。 */
export function ModelPicker({ state }: { state: ReturnType<typeof useModelChoice> }) {
  const { models, model, choice, set } = state;
  const { open, setOpen, ref } = usePopover();
  if (!model || !choice) return null;
  const effortLabel = model.efforts.find((e) => e.value === choice.reasoning_effort)?.label;
  const pick = (m: AnalysisModel) => set({ provider_id: m.provider_id, model_id: m.model_id,
    reasoning_effort: m.efforts.some((x) => x.value === choice.reasoning_effort) ? choice.reasoning_effort : null });
  return (
    <div className="av-mbar" ref={ref}>
      <button type="button" className="av-mbar__btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen(!open)}
        title="切换模型与思考强度">
        <span className="av-mbar__model">{model.model_id}</span>
        {effortLabel ? <span className="av-mbar__effort">{effortLabel}</span> : null}
      </button>
      {open ? (
        <div className="av-menu av-menu--up av-mbar__menu" role="menu">
          <div className="av-mbar__sec">模型</div>
          {models.map((m) => {
            const current = m.provider_id === choice.provider_id && m.model_id === choice.model_id;
            return (
              <button key={`${m.provider_id}/${m.model_id}`} type="button" role="menuitemradio" aria-checked={current}
                className="av-menu__item av-mbar__item" onClick={() => pick(m)}>
                <span className="av-mbar__check">{current ? <Icon name="check" /> : null}</span>
                <span>{m.model_id}</span>
                {models.filter((x) => x.model_id === m.model_id).length > 1 ? <span className="av-mbar__meta">{m.provider_name}</span> : <span />}
              </button>
            );
          })}
          {model.efforts.length ? <>
            <div className="av-mbar__sec">思考强度</div>
            <div className="av-mbar__efforts" role="radiogroup" aria-label="思考强度">
              {model.efforts.map((e) => (
                <button key={e.value} type="button" role="radio" aria-checked={e.value === choice.reasoning_effort}
                  onClick={() => set({ ...choice, reasoning_effort: e.value })}>{e.label}</button>
              ))}
            </div>
          </> : null}
        </div>
      ) : null}
    </div>
  );
}

const k = (n: number) => (n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${+(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k` : String(n));

/** 上下文：一个小圆环（最近一次请求的输入 token / 上下文窗口）。点开看用量；运行中可以手动压缩。 */
export function ContextMeter({ run, live }: { run: Run; live?: boolean }) {
  const list = useAnalysisModels();
  const { open, setOpen, ref } = usePopover();
  const [note, setNote] = useState<string | null>(null);
  const compact = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/compact", {
    params: { path: { run_id: run.id } } })), ["analysis-run"]);
  const steps = run.steps ?? [];
  const last = [...steps].reverse().find((s) => s.kind === "model" && (s.output as { input_tokens?: number } | null)?.input_tokens);
  const used = Number((last?.output as { input_tokens?: number } | null)?.input_tokens ?? 0);
  if (!used) return null;
  const choice = (run as Run & { model_choice?: ModelChoice | null }).model_choice;
  const modelId = choice?.model_id ?? run.model_adapter?.split(":").slice(1).join(":");
  const window = (list.data?.models ?? []).find((m) => m.model_id === modelId
    && (!choice || m.provider_id === choice.provider_id))?.context_window ?? null;
  const ratio = window ? Math.min(1, used / window) : null;
  const tone = ratio === null ? "" : ratio > 0.85 ? " is-danger" : ratio > 0.6 ? " is-warn" : "";
  const label = window ? `上下文 ${k(used)} / ${k(window)}（${Math.max(1, Math.round((ratio ?? 0) * 100))}%）` : `上下文 ${k(used)} tokens`;
  return (
    <div className={`av-mbar${tone}`} ref={ref}>
      <button type="button" className="av-mbar__btn av-mbar__ring" aria-haspopup="dialog" aria-expanded={open} aria-label={label}
        title={label} onClick={() => setOpen(!open)}>
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
          <circle cx="9" cy="9" r="6.5" fill="none" stroke="var(--line-strong)" strokeWidth="2" />
          <circle cx="9" cy="9" r="6.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
            strokeDasharray={`${Math.max(0.04, ratio ?? 0.04) * 40.8} 40.8`} transform="rotate(-90 9 9)" />
        </svg>
      </button>
      {open ? (
        <div className="av-menu av-menu--up av-mbar__menu av-mbar__ctx" role="dialog" aria-label="上下文">
          <div className="av-mbar__sec">上下文</div>
          <div className="av-mbar__usage"><b>{k(used)}</b>{window ? <span> / {k(window)} tokens</span> : <span> tokens</span>}</div>
          {ratio !== null ? <div className="av-mbar__bar"><i style={{ width: `${Math.max(1, ratio * 100)}%` }} /></div> : null}
          {live ? (
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={compact.isPending}
              onClick={() => compact.mutate(undefined, {
                onSuccess: () => setNote("将在下一步之前压缩"), onError: (e) => setNote(errorText(e)) })}>
              <Icon name="fit" small />{compact.isPending ? "提交中…" : "压缩上下文"}</button>
          ) : null}
          {note ? <p className="av-mbar__hint">{note}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
