// 分析运行的 SSE 流订阅与状态管理：按 runId 引用计数去重，避免同一页多个 useRun(id) 重复建立连接。
import type { QueryClient } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";

import type { Schemas } from "../../api/client";
import { consumeSse } from "../../api/sse";

type Run = Schemas["AnalysisRunOut"];
type Step = NonNullable<Run["steps"]>[number];
type Live = Schemas["AnalysisLiveOut"];

export type RunStreamState = "idle" | "connecting" | "connected" | "fallback";

const TERMINAL_STATUSES = new Set(["succeeded", "failed", "stopped", "clarify", "waiting"]);

type Entry = {
  state: RunStreamState;
  refs: number;
  abort: AbortController | null;
  started: boolean;
  finished: boolean;
  listeners: Set<() => void>;
};

const registry = new Map<string, Entry>();

function getOrCreate(runId: string): Entry {
  let entry = registry.get(runId);
  if (!entry) {
    entry = { state: "idle", refs: 0, abort: null, started: false, finished: false, listeners: new Set() };
    registry.set(runId, entry);
  }
  return entry;
}

function notify(entry: Entry) {
  for (const fn of entry.listeners) fn();
}

function setEntryState(runId: string, entry: Entry, next: RunStreamState) {
  if (entry.state === next) return;
  entry.state = next;
  notify(entry);
  if (registry.get(runId) !== entry) return;
}

function mergeStep(steps: Step[] | undefined, incoming: Step): Step[] {
  const list = [...(steps ?? [])];
  const idx = list.findIndex((s) => s.seq === incoming.seq);
  if (idx >= 0) list[idx] = incoming;
  else list.push(incoming);
  return list.sort((a, b) => a.seq - b.seq);
}

function invalidateRunRelated(qc: QueryClient, runId: string) {
  void qc.invalidateQueries({ queryKey: ["analysis-run", runId] });
  void qc.invalidateQueries({ queryKey: ["analysis-runs"] });
  void qc.invalidateQueries({ queryKey: ["analysis-conversations"] });
  void qc.invalidateQueries({ queryKey: ["analysis-conversation"] });
}

function startStream(runId: string, entry: Entry, qc: QueryClient) {
  if (entry.started || entry.finished) return;
  entry.started = true;
  const ctrl = new AbortController();
  entry.abort = ctrl;
  setEntryState(runId, entry, "connecting");

  const cached = qc.getQueryData<Run>(["analysis-run", runId]);
  const afterSeq = (cached?.steps ?? []).reduce((max, s) => Math.max(max, Number(s.seq) || 0), 0);
  const url = `/api/analysis/runs/${encodeURIComponent(runId)}/stream?after_seq=${afterSeq}`;

  void consumeSse<Run>(url, {
    signal: ctrl.signal,
    onOpen: () => {
      if (!ctrl.signal.aborted && !entry.finished) setEntryState(runId, entry, "connected");
    },
    onEvent: (ev) => {
      if (ctrl.signal.aborted) return;
      const payload = (ev.data && typeof ev.data === "object" ? ev.data : {}) as Record<string, unknown>;
      switch (ev.event) {
        case "step": {
          const step = payload.step as Step | undefined;
          if (!step || typeof step.seq !== "number") break;
          qc.setQueryData<Run>(["analysis-run", runId], (prev) => {
            if (!prev) return prev;
            const live = prev.live && (prev.live.after_seq ?? 0) < step.seq ? null : prev.live;
            return { ...prev, steps: mergeStep(prev.steps ?? undefined, step), live };
          });
          break;
        }
        case "status": {
          const status = typeof payload.status === "string" ? payload.status : undefined;
          const phase = payload.phase === "model" || payload.phase === "tool" ? payload.phase : undefined;
          const seq = typeof payload.after_seq === "number" ? payload.after_seq : undefined;
          const tool = typeof payload.tool === "string" ? payload.tool : null;
          const args = (payload.args && typeof payload.args === "object" ? payload.args : undefined) as Record<string, unknown> | undefined;
          const restricted = !!payload.restricted;
          qc.setQueryData<Run>(["analysis-run", runId], (prev) => {
            if (!prev) return prev;
            let live: Live | null | undefined = prev.live;
            if (phase) {
              const sameLive = prev.live && prev.live.phase === phase && prev.live.after_seq === (seq ?? prev.live.after_seq)
                && (prev.live.tool ?? null) === tool;
              live = sameLive
                ? { ...prev.live!, restricted }
                : {
                    phase,
                    after_seq: seq ?? (prev.steps ?? []).reduce((m, s) => Math.max(m, s.seq), 0),
                    started_at: new Date().toISOString(),
                    elapsed_ms: 0,
                    tool,
                    args,
                    thinking: "",
                    text: "",
                    restricted,
                  };
            }
            return { ...prev, ...(status ? { status } : {}), live };
          });
          if (status && TERMINAL_STATUSES.has(status)) {
            invalidateRunRelated(qc, runId);
          }
          break;
        }
        case "thinking": {
          const content = typeof payload.content === "string" ? payload.content : undefined;
          const delta = typeof payload.delta === "string" ? payload.delta : "";
          const phase = payload.phase === "tool" ? "tool" : "model";
          const seq = typeof payload.after_seq === "number" ? payload.after_seq : undefined;
          qc.setQueryData<Run>(["analysis-run", runId], (prev) => {
            if (!prev) return prev;
            const cur = prev.live;
            const nextThinking = content !== undefined ? content : `${cur?.thinking ?? ""}${delta}`;
            return {
              ...prev,
              live: {
                phase: cur?.phase ?? phase,
                after_seq: seq ?? cur?.after_seq ?? 0,
                started_at: cur?.started_at ?? new Date().toISOString(),
                elapsed_ms: cur?.elapsed_ms ?? 0,
                tool: cur?.tool ?? null,
                args: cur?.args ?? undefined,
                text: cur?.text ?? "",
                thinking: nextThinking,
                restricted: false,
              },
            };
          });
          break;
        }
        case "delta": {
          const content = typeof payload.content === "string" ? payload.content : undefined;
          const delta = typeof payload.delta === "string" ? payload.delta : "";
          const phase = payload.phase === "tool" ? "tool" : "model";
          const seq = typeof payload.after_seq === "number" ? payload.after_seq : undefined;
          qc.setQueryData<Run>(["analysis-run", runId], (prev) => {
            if (!prev) return prev;
            const cur = prev.live;
            const nextText = content !== undefined ? content : `${cur?.text ?? ""}${delta}`;
            return {
              ...prev,
              live: {
                phase: cur?.phase ?? phase,
                after_seq: seq ?? cur?.after_seq ?? 0,
                started_at: cur?.started_at ?? new Date().toISOString(),
                elapsed_ms: cur?.elapsed_ms ?? 0,
                tool: cur?.tool ?? null,
                args: cur?.args ?? undefined,
                thinking: cur?.thinking ?? "",
                text: nextText,
                restricted: false,
              },
            };
          });
          break;
        }
        case "error": {
          const code = typeof payload.code === "string" ? payload.code : "";
          const message = typeof payload.message === "string" ? payload.message : "";
          if (code === "analysis_failed") {
            qc.setQueryData<Run>(["analysis-run", runId], (prev) => (prev ? { ...prev, status: "failed", error: message || prev.error } : prev));
          } else {
            setEntryState(runId, entry, "fallback");
            ctrl.abort();
          }
          break;
        }
        case "done": {
          entry.finished = true;
          if ("id" in payload && "status" in payload) {
            qc.setQueryData(["analysis-run", runId], payload as unknown as Run);
          } else {
            void qc.invalidateQueries({ queryKey: ["analysis-run", runId] });
          }
          void qc.invalidateQueries({ queryKey: ["analysis-runs"] });
          void qc.invalidateQueries({ queryKey: ["analysis-conversations"] });
          void qc.invalidateQueries({ queryKey: ["analysis-conversation"] });
          setEntryState(runId, entry, "idle");
          break;
        }
      }
    },
  })
    .then((res) => {
      if (res.mode === "json" && res.data) {
        entry.finished = true;
        qc.setQueryData(["analysis-run", runId], res.data);
        invalidateRunRelated(qc, runId);
        setEntryState(runId, entry, "idle");
        return;
      }
      if (!entry.finished && !ctrl.signal.aborted) {
        setEntryState(runId, entry, "fallback");
      }
    })
    .catch(() => {
      if (!entry.finished && !ctrl.signal.aborted) {
        setEntryState(runId, entry, "fallback");
      }
    });
}

export function useRunStreamState(runId?: string): RunStreamState {
  return useSyncExternalStore(
    (onStoreChange) => {
      if (!runId) return () => {};
      const entry = getOrCreate(runId);
      entry.listeners.add(onStoreChange);
      return () => {
        entry.listeners.delete(onStoreChange);
        if (entry.refs === 0 && entry.listeners.size === 0) registry.delete(runId);
      };
    },
    () => (runId ? (registry.get(runId)?.state ?? "idle") : "idle"),
    () => "idle",
  );
}

export function useRunStream(runId: string | undefined, enabled: boolean, qc: QueryClient) {
  useEffect(() => {
    if (!runId || !enabled) return;
    const entry = getOrCreate(runId);
    entry.refs += 1;
    startStream(runId, entry, qc);
    return () => {
      entry.refs = Math.max(0, entry.refs - 1);
      if (entry.refs === 0) {
        entry.abort?.abort();
        entry.abort = null;
        entry.started = false;
        entry.finished = false;
        setEntryState(runId, entry, "idle");
        if (entry.listeners.size === 0) registry.delete(runId);
      }
    };
  }, [runId, enabled, qc]);
}
