// 血缘：画布模式 —— 列级血缘画布（@xyflow/react + dagre 自动布局）| 检查器。
// 节点按 Avelis UI 的 av-node 渲染；取值边为实线，过滤 / 关联键 / 分组这类影响关系为警告色虚线，
// 外部表与真实表是“同一张表”（点线 + 链条标记），导入的设计时表为虚线占位节点。点击列高亮列级路径。
import dagre from "@dagrejs/dagre";
import {
  Handle, Position, ReactFlow, ReactFlowProvider, useReactFlow, type Edge, type Node, type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router";

import { useAsset, useDomains, useGraph, type Graph } from "../../api/hooks";
import {
  Dot, Empty, Hint, Icon, KIND_LABEL, Plat, RELATION_LABEL, Status, statusOf, shortName,
} from "../../components/ui";
import { SWATCHES } from "../catalog/CatalogPage";

type GNode = Graph["nodes"][number];
type ColKey = string; // `${nodeId}::${column}`

const MAX_COLS = 6;
const VALUE = new Set(["value_direct", "value_computed", "value_aggregated", "table"]);

interface AssetData extends Record<string, unknown> {
  node: GNode;
  columns: { name: string; relation: string | null; influence: boolean }[];
  domainSwatch: string | null;
  selected: boolean;
  pathCols: Set<string>;
  link: { status: string; target: string } | null;
  onColumn: (col: string) => void;
}

function nodeHeight(cols: number, hasLink: boolean, hasGrain: boolean): number {
  const shown = Math.min(cols, MAX_COLS);
  return 54 + (shown ? shown * 27 + 9 : 0) + (cols > MAX_COLS ? 22 : 0) + (hasGrain ? 27 : 0) + (hasLink ? 31 : 0);
}

function AssetNode({ data }: NodeProps<Node<AssetData>>) {
  const { node, columns, domainSwatch, selected, pathCols, link } = data;
  const placeholder = node.kind === "design_time";
  const grain = node.grains[0];
  const [tone] = statusOf(grain?.status);
  const parts = node.qualified.split(".");
  return (
    <div className={`av-node${selected ? " is-selected" : ""}${placeholder ? " is-placeholder" : ""}`}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="av-node__hd">
        <span className="av-node__type"><Icon name={node.kind === "view" ? "view" : node.kind === "design_time" ? "upload" : "table"} /></span>
        <span className="av-node__id">
          <span className="av-node__path">
            {domainSwatch ? <span className={`av-swatch av-swatch--${domainSwatch}`} /> : null}
            {node.datasource ? <Plat kind={node.datasource.kind} /> : <span className="av-plat">导入</span>}
            {parts.slice(-3, -1).join(".")}
          </span>
          <span className="av-node__name">{parts[parts.length - 1]}</span>
        </span>
        <span className="av-tag">{KIND_LABEL[node.kind] ?? node.kind}</span>
      </div>
      {columns.length ? (
        <div className="av-node__cols">
          {columns.slice(0, MAX_COLS).map((c) => (
            <button key={c.name} type="button" className={`av-col nodrag${pathCols.has(c.name) ? " is-path" : c.influence ? " is-influence" : ""}`}
              style={{ border: "none", background: pathCols.has(c.name) ? undefined : "transparent", cursor: "pointer", textAlign: "left" }}
              onClick={(e) => { e.stopPropagation(); data.onColumn(c.name); }}>
              <span>{c.name}</span><span>{c.relation ? RELATION_LABEL[c.relation] : ""}</span>
            </button>
          ))}
          {columns.length > MAX_COLS ? <div className="av-node__more">+{columns.length - MAX_COLS} 列</div> : null}
        </div>
      ) : null}
      {grain ? <div className="av-node__grain">粒度 <b>{grain.columns.join(", ")}</b><Dot tone={tone} /></div> : null}
      {link ? <div className="av-node__link"><Icon name="link" small />同一张表 <b>{link.target}</b><Status value={link.status} /></div> : null}
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { asset: AssetNode };

export function LineagePage() {
  return <ReactFlowProvider><Canvas /></ReactFlowProvider>;
}

function Canvas() {
  const [params, setParams] = useSearchParams();
  const domains = useDomains();
  const domainIds = (params.get("domains") ?? "").split(",").filter(Boolean);
  const focusAsset = params.get("asset") ?? undefined;
  const graph = useGraph(domainIds, focusAsset);
  const [selected, setSelected] = useState<string | null>(focusAsset ?? null);
  const [column, setColumn] = useState<ColKey | null>(null);
  const flow = useReactFlow();

  const swatchOf = useMemo(() => {
    const map = new Map<string, string>();
    (domains.data ?? []).forEach((d, i) => map.set(d.id, SWATCHES[i % SWATCHES.length]));
    return map;
  }, [domains.data]);

  // 列级路径：从选中的列向上游、下游各走一遍（只走取值关系）
  const path = useMemo(() => {
    const cols = new Set<ColKey>();
    const edgeKeys = new Set<string>();
    if (!column || !graph.data) return { cols, edgeKeys };
    const up = new Map<ColKey, { from: ColKey; edge: string }[]>();
    const down = new Map<ColKey, { to: ColKey; edge: string }[]>();
    for (const e of graph.data.edges) {
      for (const c of e.columns) {
        if (!c.target_column || !c.source_column || !VALUE.has(c.relation)) continue;
        const s = `${e.source}::${c.source_column}`;
        const t = `${e.target}::${c.target_column}`;
        const key = `${e.source}->${e.target}`;
        up.set(t, [...(up.get(t) ?? []), { from: s, edge: key }]);
        down.set(s, [...(down.get(s) ?? []), { to: t, edge: key }]);
      }
    }
    for (const l of graph.data.identity_links) {
      if (!l.target) continue;
      const src = graph.data.nodes.find((n) => n.qualified === l.source);
      if (!src) continue;
      for (const m of ((l.column_match as { matched?: string[] }).matched ?? [])) {
        const a = `${src.id}::${m}`;
        const b = `${l.target.id}::${m}`;
        up.set(a, [...(up.get(a) ?? []), { from: b, edge: `id:${l.id}` }]);
        down.set(b, [...(down.get(b) ?? []), { to: a, edge: `id:${l.id}` }]);
      }
    }
    const walk = (start: ColKey, next: Map<ColKey, { from?: ColKey; to?: ColKey; edge: string }[]>) => {
      const stack = [start];
      while (stack.length) {
        const k = stack.pop()!;
        if (cols.has(k) && k !== start) continue;
        cols.add(k);
        for (const n of next.get(k) ?? []) {
          edgeKeys.add(n.edge);
          stack.push((n.from ?? n.to)!);
        }
      }
    };
    walk(column, up);
    walk(column, down as unknown as Map<ColKey, { from?: ColKey; edge: string }[]>);
    return { cols, edgeKeys };
  }, [column, graph.data]);

  const { nodes, edges } = useMemo(() => {
    if (!graph.data) return { nodes: [] as Node<AssetData>[], edges: [] as Edge[] };
    const g = graph.data;
    const colsByNode = new Map<string, Map<string, { relation: string | null; influence: boolean }>>();
    const touch = (id: string, col: string | null | undefined, relation: string | null, influence: boolean) => {
      if (!col) return;
      const m = colsByNode.get(id) ?? new Map();
      const prev = m.get(col);
      m.set(col, { relation: prev?.relation ?? relation, influence: (prev?.influence ?? false) || influence });
      colsByNode.set(id, m);
    };
    for (const e of g.edges) {
      for (const c of e.columns) {
        const value = VALUE.has(c.relation);
        touch(e.target, c.target_column, value ? c.relation : null, false);
        touch(e.source, c.source_column, null, !value);
      }
    }
    const linkOf = new Map(g.identity_links.filter((l) => l.target).map((l) => [l.source, l]));
    const layout = new dagre.graphlib.Graph();
    layout.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 96, marginx: 40, marginy: 40 });
    layout.setDefaultEdgeLabel(() => ({}));
    const built: Node<AssetData>[] = g.nodes.map((n) => {
      const cols = [...(colsByNode.get(n.id) ?? new Map()).entries()].map(([name, v]) => ({ name, ...v }))
        .sort((a, b) => Number(!!b.relation) - Number(!!a.relation));
      const l = linkOf.get(n.qualified);
      const link = l ? { status: l.status, target: shortName(l.target?.qualified) } : null;
      const height = nodeHeight(cols.length, !!link, n.grains.length > 0);
      layout.setNode(n.id, { width: 248, height });
      return {
        id: n.id, type: "asset", position: { x: 0, y: 0 }, width: 248, height,
        data: {
          node: n, columns: cols, link, selected: n.id === selected,
          domainSwatch: n.domain_id ? swatchOf.get(n.domain_id) ?? null : null,
          pathCols: new Set([...path.cols].filter((k) => k.startsWith(`${n.id}::`)).map((k) => k.split("::")[1])),
          onColumn: (col: string) => { setSelected(n.id); setColumn(`${n.id}::${col}`); },
        },
      };
    });
    const builtEdges: Edge[] = [];
    for (const e of g.edges) {
      const value = e.relations.some((r) => VALUE.has(r));
      const designTime = e.origins.every((o) => o !== "parsed" && o !== "manual");
      const key = `${e.source}->${e.target}`;
      const onPath = path.edgeKeys.has(key);
      layout.setEdge(e.source, e.target);
      builtEdges.push({
        id: key, source: e.source, target: e.target, type: "default",
        label: e.relations.includes("join_key") ? "关联" : undefined,
        labelStyle: { fill: "var(--warn)", fontSize: 11, fontFamily: "var(--font-mono)" },
        labelBgStyle: { fill: "var(--bg-overlay)" },
        style: {
          stroke: onPath ? "var(--signal)" : value ? "var(--edge-default)" : "var(--warn)",
          strokeWidth: onPath ? 2 : 1.5,
          strokeDasharray: !value ? "4 4" : designTime ? "6 4" : undefined,
          filter: onPath ? "drop-shadow(0 0 3px var(--signal))" : undefined,
        },
      });
    }
    for (const l of g.identity_links) {
      const src = g.nodes.find((n) => n.qualified === l.source);
      if (!src || !l.target || !g.nodes.some((n) => n.id === l.target!.id)) continue;
      layout.setEdge(l.target.id, src.id);
      const onPath = path.edgeKeys.has(`id:${l.id}`);
      builtEdges.push({
        id: `id:${l.id}`, source: l.target.id, target: src.id, label: "同一张表",
        labelStyle: { fill: "var(--text-tertiary)", fontSize: 11 }, labelBgStyle: { fill: "var(--bg-overlay)" },
        style: { stroke: onPath ? "var(--signal)" : "var(--edge-identity)", strokeDasharray: "1 5", strokeLinecap: "round", strokeWidth: 2 },
      });
    }
    dagre.layout(layout);
    for (const n of built) {
      const p = layout.node(n.id);
      n.position = { x: p.x - 124, y: p.y - (n.height ?? 0) / 2 };
    }
    return { nodes: built, edges: builtEdges };
  }, [graph.data, selected, path, swatchOf]);

  const toggleDomain = (id: string) => {
    const next = domainIds.includes(id) ? domainIds.filter((d) => d !== id) : [...domainIds, id];
    const p = new URLSearchParams();
    if (next.length) p.set("domains", next.join(","));
    setParams(p);
    setColumn(null);
  };
  const selectedNode = graph.data?.nodes.find((n) => n.id === selected);

  return (
    <div className="av-split" style={{ height: "calc(100vh - 48px)" }}>
      <div className="av-canvas" style={{ position: "relative" }}>
        <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.2} maxZoom={1.6}
          proOptions={{ hideAttribution: true }} nodesDraggable={false} nodesConnectable={false}
          onNodeClick={(_, n) => { setSelected(n.id); setColumn(null); }} onPaneClick={() => setColumn(null)}
          style={{ background: "transparent" }} />
        <div className="av-float" style={{ top: 16, left: 16, flexWrap: "wrap", maxWidth: "calc(100% - 32px)" }}>
          <span className="av-section-label" style={{ padding: "0 4px" }}>业务域</span>
          <button type="button" className="av-btn av-btn--sm av-btn--ghost" aria-pressed={!domainIds.length && !focusAsset}
            style={!domainIds.length && !focusAsset ? { background: "var(--bg-active)", color: "var(--text-primary)" } : undefined}
            onClick={() => { setParams(new URLSearchParams()); setColumn(null); }}>全部</button>
          {(domains.data ?? []).map((d) => (
            <button key={d.id} type="button" className="av-btn av-btn--sm av-btn--ghost" aria-pressed={domainIds.includes(d.id)}
              style={domainIds.includes(d.id) ? { background: "var(--bg-active)", color: "var(--text-primary)" } : undefined}
              onClick={() => toggleDomain(d.id)}>
              <span className={`av-swatch av-swatch--${swatchOf.get(d.id)}`} />{d.name}</button>
          ))}
          <Hint text="多选业务域即串联显示；跨域的边沿血缘与“同一张表”继续展开" />
          <span className="av-float__sep" />
          <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label="放大" onClick={() => flow.zoomIn()}><Icon name="zoomin" /></button>
          <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label="缩小" onClick={() => flow.zoomOut()}><Icon name="zoomout" /></button>
          <button type="button" className="av-btn av-btn--sm av-btn--ghost av-btn--icon" aria-label="适应画布" onClick={() => flow.fitView()}><Icon name="fit" /></button>
          <span className="av-float__sep" />
          <Link className="av-btn av-btn--sm av-btn--primary" to="/lineage/import"><Icon name="upload" small />导入</Link>
        </div>
        <div className="av-float" style={{ bottom: 16, left: 16, gap: 12, padding: "6px 12px" }}>
          <Legend stroke="var(--edge-default)" label="取值" />
          <Legend stroke="var(--warn)" dash="4 4" label="过滤 / 关联 / 分组" />
          <Legend stroke="var(--edge-identity)" dash="1 5" label="同一张表" />
          <Legend stroke="var(--edge-default)" dash="6 4" label="导入（设计时）" />
          <Legend stroke="var(--signal)" label="选中路径" />
        </div>
        {graph.isSuccess && !graph.data.nodes.length ? <div style={{ position: "absolute", inset: 0 }}><Empty text="没有可见的血缘" /></div> : null}
      </div>
      <aside className="av-inspector" style={{ height: "100%", overflow: "auto" }} aria-label="检查器">
        {selectedNode ? <Inspector node={selectedNode} graph={graph.data!} column={column?.split("::")[1] ?? null} /> : <Empty text="点击节点或列查看详情" />}
      </aside>
    </div>
  );
}

function Legend({ stroke, dash, label }: { stroke: string; dash?: string; label: string }) {
  return (
    <span className="av-caption" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <svg width="24" height="6" aria-hidden="true"><line x1="0" y1="3" x2="24" y2="3" stroke={stroke} strokeWidth="2" strokeDasharray={dash} strokeLinecap="round" /></svg>{label}
    </span>
  );
}

function Inspector({ node, graph, column }: { node: GNode; graph: Graph; column: string | null }) {
  const asset = useAsset(node.kind === "design_time" ? undefined : node.id);
  const incoming = graph.edges.filter((e) => e.target === node.id);
  const outgoing = graph.edges.filter((e) => e.source === node.id);
  const name = (id: string) => graph.nodes.find((n) => n.id === id)?.qualified ?? id.replace(/^ref:/, "");
  const colIn = column ? incoming.flatMap((e) => e.columns.filter((c) => c.target_column === column).map((c) => ({ ...c, from: name(e.source) }))) : [];
  const influences = incoming.flatMap((e) => e.columns.filter((c) => !c.target_column).map((c) => ({ ...c, from: name(e.source) })));
  const colOut = column ? outgoing.flatMap((e) => e.columns.filter((c) => c.source_column === column).map((c) => ({ ...c, to: name(e.target) }))) : [];
  const col = asset.data?.columns.find((c) => c.name === column);
  return (
    <>
      <div className="av-inspector__hd">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <span className="av-section-label">{KIND_LABEL[node.kind] ?? node.kind}{node.datasource ? ` · ${node.datasource.name}` : ""}</span>
          {node.kind !== "design_time" ? <Link className="av-btn av-btn--secondary av-btn--sm" to={`/catalog/assets/${node.id}`}>资产</Link> : null}
        </div>
        <span className="av-mono" style={{ fontWeight: 500 }}>{node.qualified}{column ? <span style={{ color: "var(--signal)" }}>.{column}</span> : null}</span>
        {node.note ? <span className="av-caption">{node.note}</span> : null}
      </div>
      <div className="av-inspector__bd">
        {column ? (
          <>
            {col ? <dl className="av-kv"><dt>类型</dt><dd className="av-mono">{col.data_type}</dd>
              <dt>推导</dt><dd><Status value={col.fingerprint_status ?? "unknown"} /> <span className="av-mono av-muted">{col.fingerprint?.slice(0, 10)}</span></dd>
              {col.fingerprint_reason ? <><dt>原因</dt><dd>{col.fingerprint_reason}</dd></> : null}
              <dt>业务含义</dt><dd>{col.confirmations.find((c) => c.valid)?.meaning ?? <span className="av-muted">未确认</span>}</dd></dl> : null}
            <Flow title="来源" items={colIn.map((c) => ({ key: `${c.from}.${c.source_column}`, left: `${shortName(c.from)}.${c.source_column}`, rel: c.relation, origin: c.origin, expr: c.expression }))} />
            <Flow title="下游" items={colOut.map((c) => ({ key: `${c.to}.${c.target_column}`, left: `${shortName(c.to)}.${c.target_column ?? "（影响）"}`, rel: c.relation, origin: c.origin, expr: c.expression }))} />
          </>
        ) : (
          <>
            {node.grains.length ? <dl className="av-kv"><dt>粒度</dt><dd>{node.grains.map((g) => <span key={g.columns.join()} className="av-tag" style={{ marginRight: 4 }}>{g.columns.join(", ")} · <Status value={g.status} /></span>)}</dd></dl> : null}
            <Flow title="上游" items={[...new Map(incoming.map((e) => [e.source, e])).values()].map((e) => ({ key: e.source, left: shortName(name(e.source)), rel: e.relations.find((r) => VALUE.has(r)) ?? e.relations[0], origin: e.origins.join("、"), expr: `${e.columns.length} 条列级边` }))} />
            <Flow title="下游" items={[...new Map(outgoing.map((e) => [e.target, e])).values()].map((e) => ({ key: e.target, left: shortName(name(e.target)), rel: e.relations.find((r) => VALUE.has(r)) ?? e.relations[0], origin: e.origins.join("、"), expr: `${e.columns.length} 条列级边` }))} />
          </>
        )}
        {influences.length ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <span className="av-section-label">影响条件 <Hint text="过滤、关联键、分组决定哪些行参与，但值不复制到输出" /></span>
            {influences.map((c, i) => <span key={i} className="av-chain__item"><span className="av-tag">{RELATION_LABEL[c.relation]}</span><span>{shortName(c.from)}.{c.source_column}</span></span>)}
          </div>
        ) : null}
        <span className="av-caption">点击节点中的列，高亮该列的上下游路径。</span>
      </div>
    </>
  );
}

const ORIGIN: Record<string, string> = { parsed: "解析", manual: "人工", import: "导入解析", import_manual: "导入·人工", model_confirmed: "模型·已确认" };

function Flow({ title, items }: { title: string; items: { key: string; left: string; rel: string; origin: string; expr?: string | null }[] }) {
  if (!items.length) return null;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <span className="av-section-label">{title}</span>
      {items.map((it) => (
        <div key={it.key} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <span className="av-chain__item"><span>{it.left}</span><span className="av-tag" style={{ marginLeft: "auto" }}>{RELATION_LABEL[it.rel] ?? it.rel}</span></span>
          <span className="av-caption">{it.origin.split("、").map((o) => ORIGIN[o] ?? o).join("、")}{it.expr ? ` · ${it.expr}` : ""}</span>
        </div>
      ))}
    </div>
  );
}
