// 分析的人机协作（ADR 0013）：预检后确认依据、对报告质疑或补充、把确认过的内容保存为个人经验。
// 只有用户明确保存的内容才进入经验库；本次确认只对本次分析生效。
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useTimeZone } from "../../api/hooks";
import { Drawer, Empty, Icon, Pill, Seg, Select, shortName } from "../../components/ui";
import { formatTime } from "../../format";

type Ref = { object_type: string; object_id: string; version?: number | null };
export type ProposalItem = { id: string; kind: string; content: string; source: string; refs?: Ref[];
  experience_id?: string | null; experience_version?: number | null };
type SourceItem = ProposalItem & { status?: string; inherited?: boolean };
type Candidate = { kind: string; content: string; refs: Ref[];
  source: { run_id: string; checkpoint: string; user_text?: string } };
type Experience = Candidate & { id: string; status: string; version: number; created_at: string };
type Decision = "" | "confirmed" | "rejected";

const ITEM_KIND: Record<string, string> = { asset: "表", join: "关联", hypothesis: "假设", feedback: "补充" };
const SOURCE: Record<string, [string, "ok" | "info" | "warn" | ""]> = {
  upstream: ["元数据", "ok"], experience: ["经验", "info"], inferred: ["推断", "warn"], user: ["你的补充", ""],
};
const ITEM_STATUS: Record<string, string> = { confirmed: "已确认", rejected: "已否决", unconfirmed: "未确认", grounded: "有依据" };
const EXP_KIND: Record<string, string> = { join: "关联", driver: "驱动因素", alias: "别名", rule: "规则", preference: "偏好",
  negative: "否定" };

function SourcePill({ source }: { source: string }) {
  const [label, tone] = SOURCE[source] ?? [source, ""];
  return <Pill tone={tone}>{label}</Pill>;
}

/** 表项显示短名（悬停看全名），其余项原样显示。 */
function ItemContent({ item }: { item: ProposalItem }) {
  return item.kind === "asset"
    ? <span className="av-proposal__content av-mono" title={item.content}>{shortName(item.content)}</span>
    : <span className="av-proposal__content" title={refsTitle(item.refs)}>{item.content}</span>;
}

function refsTitle(refs?: Ref[]) {
  return (refs ?? []).map((r) => `${r.object_type === "asset" ? "资产" : "口径"} ${r.object_id}${r.version ? ` v${r.version}` : ""}`)
    .join("\n");
}

/** 预检之后的等待卡片：逐项确认或否决模型提出的表、关联与假设，可附补充，然后继续分析。 */
export function ConfirmPanel({ runId, items }: { runId: string; items: ProposalItem[] }) {
  // 有元数据或经验依据的项默认确认；模型推断必须由用户逐项决定
  const [decisions, setDecisions] = useState<Record<string, Decision>>(() =>
    Object.fromEntries(items.map((i) => [i.id, i.source === "inferred" ? "" : "confirmed"])));
  const [supplement, setSupplement] = useState("");
  const [error, setError] = useState<string | null>(null);
  const resume = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/resume", {
    params: { path: { run_id: runId } },
    body: { decisions: items.map((i) => ({ item_id: i.id, decision: decisions[i.id] })), supplement } as never })),
  ["analysis-run", "analysis-runs"]);
  const pending = items.filter((i) => !decisions[i.id]).length;
  return (
    <section className="av-analysis__confirm" aria-label="确认分析依据">
      <header className="av-analysis__confirm-hd">
        <h3>确认分析依据 {pending ? <Pill tone="warn">{pending} 项待定</Pill> : null}</h3>
        <button type="button" className="av-btn av-btn--ghost av-btn--sm"
          onClick={() => setDecisions(Object.fromEntries(items.map((i) => [i.id, "confirmed"])))}>全部确认</button>
      </header>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      {!items.length ? <p className="av-caption">没有需要确认的依据，可以直接继续，或补充你知道的规则。</p> : null}
      <ol className="av-proposals" hidden={!items.length}>
        {items.map((i) => (
          <li key={i.id} className={`av-proposal${decisions[i.id] === "rejected" ? " is-rejected" : ""}`}>
            <Pill tone="">{ITEM_KIND[i.kind] ?? i.kind}</Pill>
            <ItemContent item={i} />
            <SourcePill source={i.source} />
            <Seg<Decision> label={`第 ${i.id} 项`} value={decisions[i.id] ?? ""}
              options={[{ value: "confirmed", label: "确认" }, { value: "rejected", label: "否决" }]}
              onChange={(v) => setDecisions({ ...decisions, [i.id]: v })} />
          </li>
        ))}
      </ol>
      <form className="av-analysis__confirm-ft" onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        resume.mutate(undefined, { onError: (err) => setError(errorText(err)) });
      }}>
        <label htmlFor={`sup-${runId}`} className="av-sr">补充</label>
        <textarea id={`sup-${runId}`} className="av-input" rows={2} maxLength={4000} value={supplement}
          placeholder="补充（可选）：还要看的表、关联或业务规则" onChange={(e) => setSupplement(e.target.value)} />
        <button className="av-btn av-btn--primary" type="submit" disabled={resume.isPending || pending > 0}>
          <Icon name="play" />{resume.isPending ? "提交中…" : "继续分析"}</button>
      </form>
    </section>
  );
}

/** 理解阶段的追问：用户回答后在同一次分析里继续（回答也会整理成可保存的经验候选）。 */
export function ClarifyPanel({ runId, question }: { runId: string; question: string }) {
  const [answer, setAnswer] = useState("");
  const [error, setError] = useState<string | null>(null);
  const resume = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/resume", {
    params: { path: { run_id: runId } }, body: { answer } as never })), ["analysis-run", "analysis-runs"]);
  return (
    <section className="av-analysis__confirm" aria-label="回答追问">
      <header className="av-analysis__confirm-hd"><h3>需要你确认范围</h3></header>
      <p className="av-analysis__question">{question}</p>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <form className="av-analysis__confirm-ft" onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        resume.mutate(undefined, { onError: (err) => setError(errorText(err)) });
      }}>
        <label htmlFor={`ans-${runId}`} className="av-sr">回答</label>
        <textarea id={`ans-${runId}`} className="av-input" rows={2} maxLength={2000} value={answer} autoFocus
          placeholder="例如：指的是哪个口径、哪个范围" onChange={(e) => setAnswer(e.target.value)} />
        <button className="av-btn av-btn--primary" type="submit" disabled={resume.isPending || !answer.trim()}>
          <Icon name="play" />{resume.isPending ? "提交中…" : "继续分析"}</button>
      </form>
    </section>
  );
}

/** 报告的依据：每项标明来源与本次的确认结果；沿用上次的项单独标出。 */
export function SourcesPanel({ items, experiences }: { items?: SourceItem[]; experiences?: { id: string; version: number }[] }) {
  if (!items?.length && !experiences?.length) return null;
  const pending = (items ?? []).filter((i) => i.status === "unconfirmed").length;
  return (
    <details className="av-doc__more av-analysis__sources" aria-label="分析依据">
      <summary>分析依据 <span className="av-caption">{items?.length ?? 0} 项{pending ? ` · ${pending} 项未确认` : ""}</span>
        {experiences?.length ? <Link className="av-caption" to="/analysis/experiences">用到经验 {experiences.length} 条</Link> : null}</summary>
      <ol className="av-proposals av-proposals--compact">
        {(items ?? []).map((i, n) => (
          <li key={`${i.id ?? "u"}-${n}`} className={`av-proposal${i.status === "rejected" ? " is-rejected" : ""}`}>
            <Pill tone="">{ITEM_KIND[i.kind] ?? i.kind}</Pill>
            <ItemContent item={i} />
            <SourcePill source={i.source} />
            <span className={`av-caption av-proposal__status is-${i.status}`}>
              {i.inherited ? "沿用 · " : ""}{ITEM_STATUS[i.status ?? ""] ?? ""}</span>
          </li>
        ))}
      </ol>
    </details>
  );
}

/** 对已完成的报告质疑或补充规则：带着已确认的范围与依据重新分析一次。 */
/** 回答下方常驻的输入框：继续追问、指出哪里不对或补充规则，都会基于这次分析开一轮新的。 */
export function FollowupForm({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const followup = useAction(() => unwrap(api.POST("/api/analysis/runs/{run_id}/followup", {
    params: { path: { run_id: runId } }, body: { text } as never })), ["analysis-runs"]);
  return (
    <form className="av-analysis__followup av-compose" onSubmit={(e) => {
      e.preventDefault();
      if (followup.isPending || !text.trim()) return;
      setError(null);
      followup.mutate(undefined, { onSuccess: (r) => navigate(`/analysis/runs/${r.id}`), onError: (err) => setError(errorText(err)) });
    }}>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <label htmlFor={`fu-${runId}`} className="av-sr">质疑或补充</label>
      <textarea id={`fu-${runId}`} rows={1} maxLength={4000} value={text}
        placeholder="继续追问，或指出哪里不对、补充规则" onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }} />
      <button className="av-btn av-btn--primary av-btn--icon av-compose__send" type="submit" aria-label="发送"
        disabled={followup.isPending || !text.trim()}><Icon name="send" /></button>
    </form>
  );
}

const useExperiences = () => useQuery({ queryKey: ["analysis-experiences"],
  queryFn: () => unwrap(api.GET("/api/analysis/experiences")) as Promise<Experience[]> });

const candidateKey = (c: Candidate) => `${c.source.run_id}|${c.source.user_text ?? c.content}`;

/** 本次确认、否决、补充整理出的经验候选：可改类型与内容，明确保存后才进入个人经验。 */
export function CandidatesPanel({ candidates }: { candidates?: Candidate[] }) {
  const list = useExperiences();
  if (!candidates?.length) return null;
  const saved = new Set((list.data ?? []).map(candidateKey));
  return (
    <section className="av-analysis__candidates" aria-label="保存为经验">
      <h3>保存为经验</h3>
      <ol className="av-proposals">
        {candidates.map((c, i) => <CandidateRow key={`${i}-${candidateKey(c)}`} candidate={c} saved={saved.has(candidateKey(c))} />)}
      </ol>
    </section>
  );
}

function CandidateRow({ candidate, saved }: { candidate: Candidate; saved: boolean }) {
  const [kind, setKind] = useState(candidate.kind);
  const [content, setContent] = useState(candidate.content);
  const [error, setError] = useState<string | null>(null);
  const save = useAction(() => unwrap(api.POST("/api/analysis/experiences", {
    body: { ...candidate, kind, content } as never })), ["analysis-experiences"]);
  return (
    <li className="av-proposal av-proposal--edit">
      <Select label="经验类型" value={kind} onChange={setKind} disabled={saved} style={{ width: 104 }}>
        {Object.entries(EXP_KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
      </Select>
      <textarea className="av-input" rows={2} maxLength={4000} value={content} disabled={saved} aria-label="经验内容"
        title={refsTitle(candidate.refs)} onChange={(e) => setContent(e.target.value)} />
      {error ? <span className="av-claim__bad">{error}</span> : <span />}
      {saved ? <Pill tone="ok">已保存</Pill> : (
        <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={save.isPending || !content.trim()}
          onClick={() => save.mutate(undefined, { onError: (e) => setError(errorText(e)), onSuccess: () => setError(null) })}>
          保存</button>
      )}
    </li>
  );
}

/** 个人经验：分析时按口径与资产召回；可编辑（生成新版本）或撤销，旧版本留底。 */
export function ExperienceList() {
  const list = useExperiences();
  const tz = useTimeZone();
  const [all, setAll] = useState(false);
  const [editing, setEditing] = useState<Experience | null>(null);
  const [error, setError] = useState<string | null>(null);
  const revoke = useAction((id: string) => unwrap(api.POST("/api/analysis/experiences/{experience_id}/revoke", {
    params: { path: { experience_id: id } }, body: {} as never })), ["analysis-experiences"]);
  const rows = (list.data ?? []).filter((e) => all || e.status === "active");
  return (
    <section className="av-analysis__experiences">
      <div className="av-analysis__experiences-hd">
        <Seg label="显示范围" value={all ? "all" : "active"} onChange={(v) => setAll(v === "all")}
          options={[{ value: "active", label: "有效" }, { value: "all", label: "全部" }]} />
      </div>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      {list.isSuccess && !rows.length ? <Empty text="还没有个人经验：在分析中确认或补充后保存" compact /> : null}
      <div className="av-analysis__list">
        {rows.map((e) => (
          <div key={e.id} className={`av-row av-experience${e.status === "active" ? "" : " is-inactive"}`}>
            <Pill tone="">{EXP_KIND[e.kind] ?? e.kind}</Pill>
            <span className="av-row__main"><span className="av-row__title av-experience__content">{e.content}</span>
              <span className="av-row__meta" title={refsTitle(e.refs)}>
                v{e.version} · 引用 {e.refs.length} 项 · {formatTime(e.created_at, tz)}
                {e.status !== "active" ? ` · ${e.status === "revoked" ? "已撤销" : "已替代"}` : ""}</span></span>
            <span className="av-actions">
              {e.status === "active" ? <>
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={() => setEditing(e)}>编辑</button>
                <button type="button" className="av-btn av-btn--ghost av-btn--sm" disabled={revoke.isPending}
                  onClick={() => revoke.mutate(e.id, { onError: (err) => setError(errorText(err)) })}>撤销</button>
              </> : null}
            </span>
          </div>
        ))}
      </div>
      {editing ? <EditExperience experience={editing} onClose={() => setEditing(null)} /> : null}
    </section>
  );
}

function EditExperience({ experience, onClose }: { experience: Experience; onClose: () => void }) {
  const [kind, setKind] = useState(experience.kind);
  const [content, setContent] = useState(experience.content);
  const [error, setError] = useState<string | null>(null);
  const replace = useAction(() => unwrap(api.POST("/api/analysis/experiences/{experience_id}/replace", {
    params: { path: { experience_id: experience.id } },
    body: { kind, content, refs: experience.refs, source: experience.source } as never })), ["analysis-experiences"]);
  return (
    <Drawer title="编辑经验" onClose={onClose} footer={<div className="av-actions">
      <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={onClose}>取消</button>
      <button type="button" className="av-btn av-btn--primary av-btn--sm" disabled={replace.isPending || !content.trim()}
        onClick={() => replace.mutate(undefined, { onSuccess: onClose, onError: (e) => setError(errorText(e)) })}>保存新版本</button>
    </div>}>
      {error ? <div className="av-banner av-banner--danger">{error}</div> : null}
      <label className="av-field"><span className="av-label">类型</span>
        <Select label="经验类型" value={kind} onChange={setKind}>
          {Object.entries(EXP_KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </Select></label>
      <label className="av-field"><span className="av-label">内容</span>
        <textarea className="av-input" rows={6} maxLength={4000} value={content} onChange={(e) => setContent(e.target.value)} /></label>
      <span className="av-caption">旧版本保留在留底中</span>
    </Drawer>
  );
}
