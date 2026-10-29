// 目录入口展示对象列表；只有明确的 URL id 才进入对象详情。
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { useAssets, useDomains, useJoinPaths, useMetrics, type Asset } from "../../api/hooks";
import { Count, Empty, Icon, KIND_LABEL, QueryState, Status } from "../../components/ui";
import { useRoles } from "../../layout/Shell";
import { AssetDetail } from "./AssetDetail";
import { DomainDetail, NewDomain } from "./DomainDetail";
import { JoinPaths } from "./JoinPaths";
import { MetricDetail, NewMetric } from "./MetricDetail";

const SECTIONS = [
  { key: "metrics", label: "指标", icon: "chart" },
  { key: "assets", label: "资产", icon: "db" },
  { key: "domains", label: "业务域", icon: "users" },
  { key: "joins", label: "关联路径", icon: "link" },
] as const;
type Section = typeof SECTIONS[number]["key"];

export function CatalogPage() {
  const { section = "metrics", id } = useParams();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const roles = useRoles();
  const metrics = useMetrics();
  const assets = useAssets();
  const domains = useDomains();
  const paths = useJoinPaths();
  const [queries, setQueries] = useState<Record<string, string>>({});
  const [creating, setCreating] = useState(false);
  const [creatingDomain, setCreatingDomain] = useState(false);
  const canCreateMetric = roles.has("steward") || roles.has("admin");
  const current = SECTIONS.find((item) => item.key === section);
  const query = queries[section] ?? "";
  const term = query.trim().toLocaleLowerCase();
  const matches = (...values: (string | null | undefined)[]) => !term || values.some((value) => value?.toLocaleLowerCase().includes(term));
  const metricList = (metrics.data ?? []).filter((m) => matches(m.name, m.code, ...m.aliases));
  const assetList = (assets.data ?? []).filter((a) => matches(a.qualified, a.datasource.name, KIND_LABEL[a.kind]));
  const domainList = (domains.data ?? []).filter((d) => matches(d.name, d.code, d.description));
  const currentQuery = section === "assets" ? assets : section === "domains" ? domains : metrics;
  const filteredIds = section === "assets" ? assetList : section === "domains" ? domainList : metricList;
  const excluded = !!id && !!term && currentQuery.isSuccess && currentQuery.data.some((item) => item.id === id)
    && !filteredIds.some((item) => item.id === id);
  const clearSearch = <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setQueries({ ...queries, [section]: "" })}>清除搜索</button>;
  const createMetric = canCreateMetric ? <button type="button" className="av-btn av-btn--primary" onClick={() => setCreating(true)}><Icon name="plus" />新建指标</button> : null;
  const createDomain = roles.has("admin") ? <button type="button" className="av-btn av-btn--primary" onClick={() => setCreatingDomain(true)}><Icon name="plus" />新建业务域</button> : null;
  const closeMetric = () => {
    setCreating(false);
    if (searchParams.has("create")) {
      const next = new URLSearchParams(searchParams);
      next.delete("create");
      setSearchParams(next, { replace: true });
    }
  };
  const counts: Record<Section, number | undefined> = {
    metrics: metrics.isSuccess ? metrics.data.length : undefined,
    assets: assets.isSuccess ? assets.data.length : undefined,
    domains: domains.isSuccess ? domains.data.length : undefined,
    joins: paths.isSuccess ? paths.data.length : undefined,
  };

  return (
    <div className="av-catalog av-catalog-page" data-section={section}>
      <section className="av-pane av-pane--rail av-catalog-page__rail" aria-label="目录导航">
        <header className="av-catalog-page__rail-header"><h1>目录</h1></header>
        <nav className="av-catalog-nav" aria-label="目录分类">
          {SECTIONS.map((item) => <Link key={item.key} to={`/catalog/${item.key}`} aria-current={section === item.key ? "page" : undefined}>
            <Icon name={item.icon} /><span>{item.label}</span>{counts[item.key] !== undefined ? <Count n={counts[item.key]!} /> : null}
          </Link>)}
        </nav>
        {current && section !== "joins" ? <>
          <div className="av-catalog-page__search">
            <div className="av-search" role="search"><Icon name="search" />
              <input aria-label={`搜索${current.label}`} placeholder={`搜索${current.label}`} value={query}
                onChange={(e) => setQueries({ ...queries, [section]: e.target.value })} />
            </div>
          </div>
          <div className="av-pane__body av-catalog-page__objects">
            {section === "metrics" ? <QueryState query={metrics} compact isEmpty={() => metricList.length === 0}
              emptyText={term ? "没有匹配指标" : "还没有指标"} emptyAction={term ? clearSearch : undefined}>
              {() => <nav className="av-tree" aria-label="指标对象">{metricList.map((m) => <Link key={m.id} to={`/catalog/metrics/${m.id}`}
                className="av-tree__item" aria-current={id === m.id ? "page" : undefined}><Icon name="chart" small /><span>{m.name}</span></Link>)}</nav>}
            </QueryState> : null}
            {section === "assets" ? <QueryState query={assets} compact isEmpty={() => assetList.length === 0}
              emptyText={term ? "没有匹配资产" : "还没有资产"} emptyAction={term ? clearSearch : undefined}>
              {() => <CatalogAssetTree key={`${id ?? "list"}:${term}`} assets={assetList} selectedId={id} searching={!!term} />}
            </QueryState> : null}
            {section === "domains" ? <QueryState query={domains} compact isEmpty={() => domainList.length === 0}
              emptyText={term ? "没有匹配业务域" : "还没有业务域"} emptyAction={term ? clearSearch : undefined}>
              {() => <nav className="av-tree" aria-label="业务域对象">{domainList.map((d, i) => <Link key={d.id} to={`/catalog/domains/${d.id}`}
                className="av-tree__item" aria-current={id === d.id ? "page" : undefined}>
                <span className={`av-swatch av-swatch--${SWATCHES[i % SWATCHES.length]}`} /><span>{d.name}</span></Link>)}</nav>}
            </QueryState> : null}
          </div>
        </> : null}
      </section>
      <section className="av-pane av-catalog-page__main" aria-label={id ? "对象详情" : "对象列表"}>
        {excluded ? <div className="av-catalog-page__filter-note" role="status"><Icon name="info" /><span>当前对象不在搜索结果中</span>{clearSearch}</div> : null}
        {current && section !== "joins" && !id ? <>
          <header className="av-catalog-page__list-header"><div><h1>{current.label}</h1><span className="av-catalog-page__hint">选择对象查看详情</span></div>
            <div className="av-actions">{section === "metrics" ? createMetric : section === "domains" ? createDomain : null}</div>
          </header>
          <div className="av-pane__body">
            {section === "metrics" ? <QueryState query={metrics} isEmpty={() => metricList.length === 0}
              emptyText={term ? "没有匹配指标" : "还没有指标"} emptyAction={term ? clearSearch : createMetric}>
              {() => <div className="av-table-wrap"><table className="av-table av-catalog-table"><thead><tr><th>指标</th><th>编码</th><th>状态</th><th>负责人</th><th className="end">操作</th></tr></thead>
                <tbody>{metricList.map((m) => <tr key={m.id}><td><Link to={`/catalog/metrics/${m.id}`}>{m.name}</Link></td><td className="av-mono">{m.code}</td>
                  <td><Status value={m.versions[0]?.status} /></td><td>{m.owner.display_name}</td><td className="end"><Link className="av-btn av-btn--ghost av-btn--sm" to={`/catalog/metrics/${m.id}`}>查看详情</Link></td></tr>)}</tbody></table></div>}
            </QueryState> : null}
            {section === "assets" ? <QueryState query={assets} isEmpty={() => assetList.length === 0}
              emptyText={term ? "没有匹配资产" : "还没有资产"} emptyAction={term ? clearSearch : <Link className="av-btn av-btn--secondary" to="/settings/datasources">查看数据源</Link>}>
              {() => <div className="av-table-wrap"><table className="av-table av-catalog-table"><thead><tr><th>资产</th><th>类型</th><th>数据源</th><th>状态</th><th className="end">操作</th></tr></thead>
                <tbody>{assetList.map((a) => <tr key={a.id}><td className="av-mono"><Link to={`/catalog/assets/${a.id}`}>{a.qualified}</Link></td><td>{KIND_LABEL[a.kind] ?? a.kind}</td>
                  <td>{a.datasource.name}</td><td><Status value={a.status} /></td><td className="end"><Link className="av-btn av-btn--ghost av-btn--sm" to={`/catalog/assets/${a.id}`}>查看详情</Link></td></tr>)}</tbody></table></div>}
            </QueryState> : null}
            {section === "domains" ? <QueryState query={domains} isEmpty={() => domainList.length === 0}
              emptyText={term ? "没有匹配业务域" : "还没有业务域"} emptyAction={term ? clearSearch : createDomain}>
              {() => <div className="av-table-wrap"><table className="av-table av-catalog-table"><thead><tr><th>业务域</th><th>编码</th><th>说明</th><th>成员</th><th className="end">操作</th></tr></thead>
                <tbody>{domainList.map((d) => <tr key={d.id}><td><Link to={`/catalog/domains/${d.id}`}>{d.name}</Link></td><td className="av-mono">{d.code}</td><td>{d.description || "—"}</td>
                  <td className="num">{d.members.length}</td><td className="end"><Link className="av-btn av-btn--ghost av-btn--sm" to={`/catalog/domains/${d.id}`}>查看详情</Link></td></tr>)}</tbody></table></div>}
            </QueryState> : null}
          </div>
        </> : null}
        {section === "metrics" && id ? <MetricDetail key={id} metricId={id} /> : null}
        {section === "assets" && id ? <AssetDetail key={id} assetId={id} /> : null}
        {section === "domains" && id ? <DomainDetail key={id} domainId={id} /> : null}
        {section === "joins" ? <JoinPaths /> : null}
        {!current ? <Empty text="目录分类不存在" action={<Link className="av-btn av-btn--secondary" to="/catalog/metrics">返回指标目录</Link>} /> : null}
      </section>
      {creatingDomain && roles.has("admin") ? <NewDomain onClose={() => setCreatingDomain(false)} onCreated={(did) => { setCreatingDomain(false); navigate(`/catalog/domains/${did}`); }} /> : null}
      {(creating || (section === "metrics" && searchParams.get("create") === "1")) && canCreateMetric
        ? <NewMetric onClose={closeMetric} onCreated={(mid) => { closeMetric(); navigate(`/catalog/metrics/${mid}`); }} /> : null}
    </div>
  );
}

function CatalogAssetTree({ assets, selectedId, searching }: { assets: Asset[]; selectedId?: string; searching: boolean }) {
  const sources = new Map<string, { name: string; catalogs: Map<string, Map<string, Asset[]>> }>();
  for (const asset of assets) {
    const parts = asset.qualified.split(".");
    const catalog = parts.length > 2 ? parts.slice(0, -2).join(".") : "默认 Catalog";
    const schema = parts.length > 1 ? parts.at(-2)! : "默认 Schema";
    const source = sources.get(asset.datasource.id) ?? { name: asset.datasource.name, catalogs: new Map<string, Map<string, Asset[]>>() };
    const schemas = source.catalogs.get(catalog) ?? new Map<string, Asset[]>();
    schemas.set(schema, [...(schemas.get(schema) ?? []), asset]);
    source.catalogs.set(catalog, schemas);
    sources.set(asset.datasource.id, source);
  }
  const containsSelected = (items: Asset[]) => searching || items.some((a) => a.id === selectedId);
  return <nav className="av-catalog-asset-tree" aria-label="资产对象">{[...sources].map(([sourceId, source]) => {
    const sourceAssets = [...source.catalogs.values()].flatMap((schemas) => [...schemas.values()].flat());
    return <details key={sourceId} open={containsSelected(sourceAssets)}><summary><Icon name="db" small /><span>{source.name}</span><Count n={sourceAssets.length} /></summary>
      {[...source.catalogs].map(([catalog, schemas]) => <details key={catalog} open={containsSelected([...schemas.values()].flat())}>
        <summary><span>{catalog}</span></summary>{[...schemas].map(([schema, items]) => <details key={schema} open={containsSelected(items)}>
          <summary><span>{schema}</span><Count n={items.length} /></summary>{items.map((a) => <Link key={a.id} to={`/catalog/assets/${a.id}`}
            className="av-tree__item" title={a.qualified} aria-current={selectedId === a.id ? "page" : undefined}>
            <Icon name={a.kind === "view" ? "view" : "table"} small /><span className="av-mono">{a.qualified.split(".").pop()}</span></Link>)}
        </details>)}
      </details>)}
    </details>;
  })}</nav>;
}

export const SWATCHES = ["violet", "teal", "sky", "rose", "lime", "sand"];
