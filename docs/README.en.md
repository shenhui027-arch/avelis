# Avelis Documentation

English | [简体中文](README.md) | [繁體中文](README.zh-TW.md)

Grouped by reader. The project README is available in [English](../README.en.md), [简体中文](../README.md) and [繁體中文](../README.zh-TW.md). Before writing documentation, read the [documentation guide](documentation-guide.en.md).

## Using and operating

| Document | Contents |
|---|---|
| [Product tour](product-tour.en.md) | Screenshots and descriptions of sign-in, workbench, Ask and analysis sessions, export, datasets, personal experience, lineage, catalog and settings |
| [Deployment guide](deployment.en.md) | Docker Compose, external PostgreSQL / Redis, deploying without Docker, upgrades, backups, troubleshooting |
| [Configuration reference](configuration.en.md) | The configuration file and every `AVELIS_*` environment variable, with defaults and whether they are required |

## Developing

| Document | Contents |
|---|---|
| [Development guide](development.en.md) | Backend and frontend commands, the local demo environment, unit tests, one-command acceptance |
| [Architecture and governance](architecture.en.md) | Components, data model, key mechanisms, governance rules, operation and known limits |
| [Engine verification records](engines/) (Simplified Chinese) | Database engines verified on real instances: [StarRocks 3.5.14](engines/starrocks.md) |
| [Documentation guide](documentation-guide.en.md) | Structure, languages, terminology, links and images for the README and docs |
| [Contributing guide](../CONTRIBUTING.md) | Development setup, pull request process, testing requirements |

## Design decisions (ADR)

Architecture decision records are kept in their original Simplified Chinese only.

| No. | Decision |
|---|---|
| [0001](decisions/0001-demo-environment.md) | Composition and constraints of the demo environment |
| [0002](decisions/0002-backend-foundations.md) | Backend foundations and how the governance model is implemented |
| [0003](decisions/0003-postgres-agent-model-settings.md) | PostgreSQL as the only metadata database; Ask becomes a LangGraph agent; models configured in the UI |
| [0004](decisions/0004-engine-adapters.md) | Engine adapters: how multi-database lineage is extended |
| [0005](decisions/0005-ask-expressiveness.md) | Ask expressiveness: multiple metrics, ranking, comparison and derived metrics |
| [0006](decisions/0006-column-meaning-from-db-comments.md) | Database column comments define what a column means |
| [0007](decisions/0007-analysis-agent.md) | Analysis agent: the model does the multi-step thinking, governance guards where every number comes from |
| [0008](decisions/0008-history-integrity.md) | Change history: attribution to database users, search and export, tamper evidence and retention |
| [0009](decisions/0009-grounded-numbers-and-evaluation.md) | Report numbers rendered by reference, automatic attribution, exploratory queries and the evaluation set |
| [0010](decisions/0010-business-neutral-product.md) | Product code decoupled from any specific business |
| [0011](decisions/0011-production-scale-context.md) | On-demand context at production scale |
| [0012](decisions/0012-analysis-pipeline-system-assembled-context.md) | Analysis pipeline: the system assembles context, the model reasons in few rounds |
| [0013](decisions/0013-accuracy-from-governed-metadata.md) | Conversational confirmation and learning from experience |

## Archive

[archive/](archive/) (Simplified Chinese) keeps the v0.1 development plan, milestone acceptance records and analysis-agent experiment records as evidence for design and acceptance. They are no longer maintained.

## Glossary

The full three-language glossary is in the [documentation guide](documentation-guide.en.md#terminology).

| Term | Meaning |
|---|---|
| Data source | A database instance that Avelis collects and queries (StarRocks, PostgreSQL, …), with separate collector and read-only query accounts |
| Asset | A table, view, external table or other object in a data source, identified by physical ID |
| Metadata database | Avelis's own PostgreSQL database holding assets, lineage, metric definitions, audit records and agent sessions |
| Lineage | How assets and columns are derived from each other, from view parsing, imported SQL / code and manual confirmation |
| Metric definition (metric) | A business-approved calculation with versions and publish / review states; Ask only uses published definitions |
| Business domain | The unit of ownership and permission for assets and metric definitions; members are owners, maintainers or can-query |
| Ask | Ask in natural language; SQL is generated deterministically from published metric definitions and run read-only, and the answer carries its sources and evidence |
| Analysis | A multi-step analysis agent that answers “why did this change, what is related, what should we do”; every number in its report is verified |
| Experience | Rules, joins and similar knowledge confirmed or corrected by users during analyses; governed and recalled in later analyses |
| Change history | Historical versions of metadata and lineage, append-only and sealed into a hash chain; verifiable, searchable and exportable |
| Worker | The background process that runs collection, scheduled tasks and history purges |
