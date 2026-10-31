// 输入框里的模型与思考强度选择、上下文用量（像 Codex / Claude Code 一样按次选择）。
// 选择记在本机浏览器，下次默认沿用；列表里没有了就回到管理员指派的默认模型。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, unwrap, type Schemas } from "../../api/client";

type Run = Schemas["AnalysisRunOut"];
type Effort = { value: string; label: string };
export type AnalysisModel = { provider_id: string; provider_name: string; model_id: string; efforts: Effort[];
  context_window: number | null };
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

/** 当前选择：本机记住的优先，失效时回到默认；返回请求体里要带的字段。 */
export function useModelChoice(initial?: ModelChoice | null) {
  const list = useAnalysisModels();
  const [saved, setSaved] = useState<ModelChoice | null>(() => initial ?? readSaved());
  const models = list.data?.models ?? [];
  const find = (c: ModelChoice | null) => c && models.find((m) => m.provider_id === c.provider_id && m.model_id === c.model_id);
  const model = find(saved) ?? find(list.data?.default ?? null) ?? models[0];
  const preferred = find(saved) ? saved?.reasoning_effort : list.data?.default?.reasoning_effort;
  const effort = model?.efforts.some((e) => e.value === preferred) ? preferred ?? null : model?.efforts[0]?.value ?? null;
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

/** 输入框底栏的两个选择：模型、思考强度。没有可选模型时不显示。 */
export function ModelPicker({ state }: { state: ReturnType<typeof useModelChoice> }) {
  const { models, model, choice, set } = state;
  if (!model || !choice) return null;
  const keyOf = (m: AnalysisModel) => `${m.provider_id}\u0000${m.model_id}`;
  return (
    <>
      <label className="av-compose__opt"><span>模型</span>
        <select value={keyOf(model)} onChange={(e) => {
          const next = models.find((m) => keyOf(m) === e.target.value)!;
          set({ provider_id: next.provider_id, model_id: next.model_id,
            reasoning_effort: next.efforts.some((x) => x.value === choice.reasoning_effort) ? choice.reasoning_effort : next.efforts[0]?.value ?? null });
        }}>
          {models.map((m) => <option key={keyOf(m)} value={keyOf(m)}>{m.provider_name} · {m.model_id}</option>)}
        </select></label>
      {model.efforts.length ? <label className="av-compose__opt"><span>思考</span>
        <select value={choice.reasoning_effort ?? ""} onChange={(e) => set({ ...choice, reasoning_effort: e.target.value })}>
          {model.efforts.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
        </select></label> : null}
    </>
  );
}

const k = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k` : String(n));

/** 上下文用量：最近一次模型请求的输入 token / 模型上下文窗口（窗口未知时只显示用量）。 */
export function ContextMeter({ run }: { run: Run }) {
  const list = useAnalysisModels();
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
  return (
    <span className={`av-context${tone}`} title={window ? `最近一次请求的输入占上下文 ${Math.round((ratio ?? 0) * 100)}%`
      : "最近一次请求的输入 token；模型上下文窗口未在模型配置中填写"}>
      {ratio !== null ? (
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="8" cy="8" r="6" fill="none" stroke="var(--line-default)" strokeWidth="2" />
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"
            strokeDasharray={`${ratio * 37.7} 37.7`} transform="rotate(-90 8 8)" />
        </svg>) : null}
      <span className="av-mono">{k(used)}{window ? ` / ${k(window)}` : " tokens"}</span>
    </span>
  );
}
