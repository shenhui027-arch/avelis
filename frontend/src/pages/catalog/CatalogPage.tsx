// 目录：目录树（指标 / 资产 / 业务域 / 关联路径）| 详情。
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { useAssets, useDomains, useJoinPaths, useMetrics } from "../../api/hooks";
import { Count, Dot, Icon, KIND_LABEL, statusOf } from "../../components/ui";
import { useRoles } from "../../layout/Shell";
import { AssetDetail } from "./AssetDetail";
import { DomainDetail, NewDomain } from "./DomainDetail";
import { JoinPaths } from "./JoinPaths";
import { MetricDetail, NewMetric } from "./MetricDetail";

export function CatalogPage() {
  const { section = "metrics", id } = useParams();
  const navigate = useNavigate();
  const roles = useRoles();
  const metrics = useMetrics();
  const assets = useAssets();
  const domains = useDomains();
  const paths = useJoinPaths();
  const [query, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const [creatingDomain, setCreatingDomain] = useState(false);

  const assetGroups = useMemo(() => {
    const groups = new Map<string, NonNullable<typeof assets.data>>();
    for (const a of assets.data ?? []) {
      if (query && !a.qualified.includes(query)) continue;
      const key = `${a.datasource.name} · ${a.qualified.split(".").slice(0, 2).join(".")}`;
      groups.set(key, [...(groups.get(key) ?? []), a]);
    }
    return [...groups.entries()];
  }, [assets.data, query]);

  const metricList = (metrics.data ?? []).filter((m) => !query || m.name.includes(query) || m.code.includes(query));
  const firstMetric = metricList[0]?.id;
  const firstAsset = assets.data?.[0]?.id;
  const firstDomain = domains.data?.[0]?.id;
  const currentId = id ?? (section === "metrics" ? firstMetric : section === "assets" ? firstAsset
    : section === "domains" ? firstDomain : undefined);

  return (
    <div className="av-catalog">
      <section className="av-pane av-pane--rail" style={{ borderRight: "1px solid var(--line-subtle)" }} aria-label="目录">
        <div style={{ display: "flex", flexDirection: "column", gap: 12, padding: "16px 16px 8px" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
            <h1 style={{ margin: 0, font: "600 18px/26px var(--font-sans)" }}>目录</h1>
            <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!roles.has("steward") && !roles.has("admin")}
              onClick={() => setCreating(true)}><Icon name="plus" small />新建指标</button>
          </div>
          <div className="av-search" role="search"><Icon name="search" />
            <input aria-label="搜索指标、资产" placeholder="搜索指标、资产" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
        </div>
        <div className="av-pane__body">
          <nav className="av-tree" aria-label="目录树">
            <Link to="/catalog/metrics" className="av-tree__group"><Icon name="chart" small />指标<Count n={metricList.length} /></Link>
            {metricList.map((m) => {
              const latest = m.versions[0];
              const [tone] = statusOf(latest?.status);
              return (
                <Link key={m.id} to={`/catalog/metrics/${m.id}`} className="av-tree__item"
                  aria-current={section === "metrics" && currentId === m.id ? "page" : undefined}>
                  <Dot tone={tone} /><span>{m.name}</span>{latest ? <span className="av-tag">V{latest.version_no}</span> : null}
                </Link>
              );
            })}
            <Link to="/catalog/joins" className="av-tree__group" aria-current={section === "joins" ? "page" : undefined}>
              <Icon name="link" small />关联路径<Count n={paths.data?.length ?? 0} />
            </Link>
            <div style={{ display: "flex", alignItems: "center" }}>
              <Link to="/catalog/domains" className="av-tree__group" style={{ flex: 1 }}><Icon name="users" small />业务域<Count n={domains.data?.length ?? 0} /></Link>
              {roles.has("admin") ? <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon" aria-label="新建业务域" onClick={() => setCreatingDomain(true)}><Icon name="plus" small /></button> : null}
            </div>
            {(domains.data ?? []).map((d, i) => (
              <Link key={d.id} to={`/catalog/domains/${d.id}`} className="av-tree__item"
                aria-current={section === "domains" && currentId === d.id ? "page" : undefined}>
                <span className={`av-swatch av-swatch--${SWATCHES[i % SWATCHES.length]}`} /><span>{d.name}</span>
              </Link>
            ))}
            <Link to="/catalog/assets" className="av-tree__group"><Icon name="db" small />资产<Count n={assets.data?.length ?? 0} /></Link>
            {assetGroups.map(([group, items]) => (
              <div key={group}>
                <div className="av-tree__label">{group}</div>
                {items.map((a) => (
                  <Link key={a.id} to={`/catalog/assets/${a.id}`} className="av-tree__item" title={`${KIND_LABEL[a.kind] ?? a.kind} · ${a.qualified}`}
                    aria-current={section === "assets" && currentId === a.id ? "page" : undefined}>
                    <Icon name={a.kind === "view" ? "view" : "table"} small /><span className="av-mono">{a.qualified.split(".").pop()}</span>
                    {a.kind === "catalog_table" || a.kind === "external_table" ? <Icon name="link" small /> : null}
                  </Link>
                ))}
              </div>
            ))}
          </nav>
        </div>
      </section>
      <section className="av-pane" aria-label="详情">
        {section === "metrics" && currentId ? <MetricDetail key={currentId} metricId={currentId} /> : null}
        {section === "assets" && currentId ? <AssetDetail key={currentId} assetId={currentId} /> : null}
        {section === "domains" && currentId ? <DomainDetail key={currentId} domainId={currentId} /> : null}
        {section === "joins" ? <JoinPaths /> : null}
        {section === "metrics" && !currentId && metrics.isSuccess ? (
          <div className="av-empty"><span>还没有指标</span>
            <button type="button" className="av-btn av-btn--secondary" onClick={() => setCreating(true)}>新建指标</button></div>
        ) : null}
      </section>
      {creatingDomain ? <NewDomain onClose={() => setCreatingDomain(false)} onCreated={(did) => { setCreatingDomain(false); navigate(`/catalog/domains/${did}`); }} /> : null}
      {creating ? <NewMetric onClose={() => setCreating(false)} onCreated={(mid) => { setCreating(false); navigate(`/catalog/metrics/${mid}`); }} /> : null}
    </div>
  );
}

export const SWATCHES = ["violet", "teal", "sky", "rose", "lime", "sand"];
