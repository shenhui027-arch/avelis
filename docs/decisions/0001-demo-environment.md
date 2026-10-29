# ADR 0001：演示环境的组成与约束

- 状态：已采纳
- 日期：2026-10-29

## 背景

v0.1 的验收必须在真实 StarRocks 上执行（视图定义变化、只读执行、外部表），同时要能把 StarRocks 外部表追到另一个数据库里的真实表。产品的元数据库要同时兼容 PostgreSQL 与 MySQL，并使用 Redis 协议做队列与缓存。开发机是 Windows + Docker Desktop，本机还有大量其他项目的 Compose 网络和容器。

## 决定

1. **六个容器**：StarRocks 3.5.14 all-in-one（FE + BE 单容器）；电商维表库 MySQL 8.4.10（`shop-mysql`）；Avelis 元数据库 PostgreSQL 18（`meta-pg`）与 MySQL 8.4.10（`meta-mysql`）；Valkey 8（Redis 协议）。StarRocks 镜像按 digest 固定：`sha256:5e1fa9c1a490f49457b8220b10ef2ea211af271b2626654ba151429f4603715f`；Valkey 按 digest 固定：`sha256:d2e18f3410b6f616de1417f570fa55261af2898b9c5b2cfb6781ce2373ea43d1`。
2. **电商数据分布**：事实表在 StarRocks，维表在 MySQL；StarRocks 同时用 JDBC Catalog 和旧式 `ENGINE=mysql` 外部表访问维表，以覆盖企业里两种常见写法。
3. **低资源**：StarRocks 容器上限 2.5 CPU、5.25 GB 内存且不使用额外 swap；首次启动前追加 FE 堆 1 GB、BE `mem_limit` 3 GB（`deploy/demo/starrocks/avelis-entrypoint.sh`，按标记幂等）。
4. **只绑本机**：19030 / 18030（StarRocks）、13306（维表库）、15432 / 13307（元数据库）、16379（Valkey），只绑 `127.0.0.1`，避开本机其他 StarRocks 测试环境常用的 9030 / 39130。
5. **固定子网**：默认地址池在本机已耗尽，演示网络显式使用 `10.240.17.0/24`，可用 `AVELIS_DEMO_SUBNET` 覆盖。
6. **JDBC 驱动锁定**：mysql-connector-j 9.7.0（2026-04 发布）。更新的 26.10.0 发布不足两周，不采用。下载后同时校验 Maven Central 公布的 SHA-1 与 SHA-256，记录在 `deploy/demo/drivers.lock`；jar 不进仓库。
7. **口令**：全部由 `scripts/demo.py init-env` 随机生成写入 `.env`（不进仓库、不打印）。StarRocks root 口令在 BE 注册完成后才设置；重启时由包装脚本以 `MYSQL_PWD` 传给镜像自带的启动程序。
8. **账号分离**：StarRocks `root` 只用于灌数；`avelis_collector` 采集元数据；`avelis_reader` 只读执行。MySQL 侧 `avelis_collector` 只读元数据，`sr_catalog`（`caching_sha2_password`）供 JDBC Catalog，`sr_legacy`（`mysql_native_password`）只供旧式外部表。
9. **可重置**：`demo.py seed` 删除并重建演示库到验收基线；破坏性的实机核验只在临时库 `avelis_probe` 进行，结束即删除。

## 后果

- StarRocks 通过 `shop-mysql:3306` 访问维表库，本机采集器通过 `127.0.0.1:13306` 访问同一个库。外部表身份匹配因此必须支持端点别名，并以 `server_uuid` 确认实例。
- 旧式外部表要求维表库开启 `mysql_native_password`（StarRocks BE 的 MySQL 客户端不支持 `caching_sha2_password`，见 [核验记录](../starrocks-notes.md) 第 12 条）。这是演示环境为模拟存量写法所做的妥协，生产中应优先使用 JDBC Catalog。
- 重置会让内表的 `TABLE_ID` 变化；验收以“重置后的基线”为起点，不依赖跨重置的 ID。
- 结论只代表单节点 Docker 环境，不代表多 FE/BE、存算分离或生产权限体系。
