"""真实 PostgreSQL 治理事件竞争：多进程扫描、管理员操作、封链和失败三方收尾。

只使用 meta_db 创建的独立库；网络发送替换为可同步的接收器，数据库路径保持真实。
固定种子的短延迟补充强制交错；SQL 超时、同步超时和进程超时均不能靠重试通过。
"""

from concurrent.futures import ThreadPoolExecutor, wait
from multiprocessing import get_context
from random import Random
from threading import Barrier, Event
from time import sleep

import pytest
from sqlalchemy import delete, event, select, text
from sqlalchemy.orm import Session

from avelis import audit, events, jobs
from avelis.api import events as api
from avelis.db import get_engine, new_id, session_scope, utcnow
from avelis.events import delivery
from avelis.history import integrity
from avelis.importer import batch as importer
from avelis.models import AuditEvent, Datasource, EventOutbox, ImportBatch, Job, User, WebhookSubscription
from tests.db.conftest import TEST_DB

pytestmark = pytest.mark.db
ROUNDS = range(20)
SYNC_TIMEOUT = 10


def _timeouts(connection):
    connection.exec_driver_sql("SET LOCAL lock_timeout = '3s'")
    connection.exec_driver_sql("SET LOCAL statement_timeout = '8s'")
    connection.exec_driver_sql("SET LOCAL idle_in_transaction_session_timeout = '15s'")


@pytest.fixture(scope="module", autouse=True)
def isolated_scans(meta_db):
    """屏蔽其他测试遗留的订阅，结束后恢复；不会扫描它们的 pending 条目。"""
    engine = get_engine()
    with session_scope() as session:
        assert session.scalar(text("SELECT current_database()")) == TEST_DB
        subscriptions = list(session.scalars(select(WebhookSubscription).where(WebhookSubscription.enabled.is_(True))))
        enabled_ids = [row.id for row in subscriptions]
        for row in subscriptions:
            row.enabled = False
    event.listen(engine, "begin", _timeouts)
    try:
        yield
    finally:
        event.remove(engine, "begin", _timeouts)
        with session_scope() as session:
            for row in session.scalars(select(WebhookSubscription).where(WebhookSubscription.id.in_(enabled_ids))):
                row.enabled = True


@pytest.fixture(autouse=True)
def no_hidden_failures(isolated_scans, caplog):
    errors = []

    def failed(context):
        errors.append(getattr(context.original_exception, "sqlstate", type(context.original_exception).__name__))

    engine = get_engine()
    event.listen(engine, "handle_error", failed)
    try:
        yield
    finally:
        event.remove(engine, "handle_error", failed)
    assert errors == [], f"数据库异常（含可能被业务捕获的异常）: {errors}"
    # 收尾阶段 caplog.records 只剩收尾日志；要显式取准备与运行阶段，才能抓到并发过程中被业务捕获后记下的 ERROR
    logged = [r for phase in ("setup", "call") for r in caplog.get_records(phase)]
    assert not [record.getMessage() for record in logged if record.levelno >= 40]


@pytest.fixture
def world(factory):
    actor_id = factory.user(("admin",))
    with session_scope() as session:
        subscription = WebhookSubscription(
            name=f"concurrency-{new_id()}",
            url="https://8.8.8.8/old",
            event_types=["collect.completed", "lineage_import.failed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=actor_id,
        )
        session.add(subscription)
        session.flush()
        subscription_id = subscription.id
    yield actor_id, subscription_id
    with session_scope() as session:
        session.execute(delete(EventOutbox).where(EventOutbox.subscription_id == subscription_id))
        session.execute(delete(WebhookSubscription).where(WebhookSubscription.id == subscription_id))


def _enqueue(world, *, status="pending"):
    actor_id, subscription_id = world
    with session_scope() as session:
        row = events.enqueue_test(
            session, session.get(WebhookSubscription, subscription_id), session.get(User, actor_id)
        )
        row.status = status
        if status == "dead":
            row.attempts, row.last_error = 5, "timeout"
            row.dead_at = utcnow()
        session.flush()
        return row.id


def _parallel(*functions):
    executor = ThreadPoolExecutor(max_workers=len(functions))
    futures = [executor.submit(function) for function in functions]
    try:
        _, pending = wait(futures, timeout=25)
        assert not pending, "并发任务超时"
        return [future.result() for future in futures]
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _delivered(delivery_id, attempts=1):
    with session_scope() as session:
        row = session.get(EventOutbox, delivery_id)
        assert row is not None and row.status == "delivered"
        assert row.attempts == attempts and row.delivered_at is not None and row.last_error is None


def _verify_history():
    with get_engine().connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with Session(connection) as session:
            report = integrity.verify(session)
    assert report["ok"], report
    assert report["issue_count"] == 0
    return report


def _delivery_process(pipe, barrier, worker_index):
    """spawn 的独立解释器/连接池；仅继承 meta_db 已切换的环境，不传递或输出连接串。"""
    import logging

    engine = get_engine()
    event.listen(engine, "begin", _timeouts)
    errors, sent, backend_pids = [], [], set()
    scan_synchronized = False

    class Errors(logging.Handler):
        def emit(self, record):
            errors.append(record.getMessage())

    handler = Errors(level=logging.ERROR)
    logging.getLogger("avelis").addHandler(handler)

    def failed(context):
        errors.append(getattr(context.original_exception, "sqlstate", type(context.original_exception).__name__))

    def before_scan(connection, _cursor, statement, _params, _context, _many):
        nonlocal scan_synchronized
        if "FROM event_outbox JOIN webhook_subscription" in statement and "SKIP LOCKED" in statement:
            backend_pids.add(connection.scalar(text("SELECT pg_backend_pid()")))
            # 会话租约忙时可以继续扫描其他订阅；每轮只同步第一次认领，不能要求另一进程再次扫描。
            if not scan_synchronized:
                scan_synchronized = True
                barrier.wait(timeout=SYNC_TIMEOUT)
                sleep(Random(round_no * 3 + worker_index).uniform(0, 0.01))

    def receive(subscription, outbox, **_kwargs):
        sent.append(outbox.id)
        sleep(0.03)

    event.listen(engine, "handle_error", failed)
    event.listen(engine, "before_cursor_execute", before_scan)
    delivery.send_webhook = receive
    try:
        with session_scope() as session:
            assert session.scalar(text("SELECT current_database()")) == TEST_DB
        while (round_no := pipe.recv()) is not None:
            errors.clear()
            sent.clear()
            scan_synchronized = False
            count = delivery.deliver_due(batch_size=1, budget_seconds=15)
            pipe.send((count, list(sent), list(errors), sorted(backend_pids)))
    finally:
        engine.dispose()
        pipe.close()


def test_two_worker_processes_claim_once_for_twenty_rounds(world):
    context = get_context("spawn")
    barrier = context.Barrier(2)
    channels = [context.Pipe() for _ in range(2)]
    processes = [
        context.Process(target=_delivery_process, args=(child, barrier, index))
        for index, (_, child) in enumerate(channels)
    ]
    try:
        for process in processes:
            process.start()
        pids = set()
        for _round in ROUNDS:
            delivery_id = _enqueue(world)
            for parent, _ in channels:
                parent.send(_round)
            results = []
            for parent, _ in channels:
                assert parent.poll(30), f"worker 进程超时，轮次 {_round}"
                results.append(parent.recv())
            assert sorted(result[0] for result in results) == [0, 1]
            assert [item for result in results for item in result[1]] == [delivery_id]
            assert all(not result[2] for result in results), results
            pids.update(pid for result in results for pid in result[3])
            _delivered(delivery_id)
        assert len(pids) >= 2, "必须使用不同 PostgreSQL 后端进程"
        for parent, _ in channels:
            parent.send(None)
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        for parent, child in channels:
            parent.close()
            child.close()


def test_http_holds_subscription_lease_without_an_open_transaction(world, monkeypatch):
    first_id, second_id = _enqueue(world), _enqueue(world)
    with session_scope() as session:
        healthy = WebhookSubscription(
            name=f"healthy-{new_id()}",
            url="https://8.8.4.4/healthy",
            event_types=["collect.completed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=world[0],
        )
        session.add(healthy)
        session.flush()
        healthy_id = healthy.id
    sending, scanned = Event(), Event()
    sent, lease_connections, observed_states = [], [], []

    def capture_lease(connection, _cursor, statement, _params, _context, _many):
        if "pg_try_advisory_lock" in statement:
            lease_connections.append((connection, connection.scalar(text("SELECT pg_backend_pid()"))))

    def receive(subscription, outbox, **_kwargs):
        sent.append(outbox.id)
        if outbox.id == first_id:
            # 认领已提交，只有会话锁跨过 HTTP；真实 pg_stat_activity 也不得显示打开的事务。
            connection, backend_pid = lease_connections[-1]
            assert not connection.in_transaction()
            with get_engine().connect() as observer:
                observed_states.append(
                    observer.execute(
                        text("SELECT state, xact_start FROM pg_stat_activity WHERE pid = :pid"),
                        {"pid": backend_pid},
                    ).one()
                )
            sending.set()
            assert scanned.wait(SYNC_TIMEOUT)

    def other_scanner():
        assert sending.wait(SYNC_TIMEOUT)
        try:
            return delivery.deliver_due(batch_size=1, budget_seconds=15)
        finally:
            scanned.set()

    healthy_delivery = _enqueue((world[0], healthy_id))
    monkeypatch.setattr(delivery, "send_webhook", receive)
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", capture_lease)
    try:
        assert _parallel(lambda: delivery.deliver_due(batch_size=1, budget_seconds=15), other_scanner) == [1, 1]
        assert sent == [first_id, healthy_delivery]
        assert observed_states == [("idle", None)]
        _delivered(first_id)
        _delivered(healthy_delivery)
        with session_scope() as session:
            untouched = session.get(EventOutbox, second_id)
            assert untouched.status == "pending" and untouched.attempts == 0
        assert delivery.deliver_due(batch_size=1) == 1
        assert sent == [first_id, healthy_delivery, second_id]
        _delivered(second_id)
    finally:
        event.remove(engine, "before_cursor_execute", capture_lease)
        with session_scope() as session:
            session.execute(delete(EventOutbox).where(EventOutbox.subscription_id == healthy_id))
            session.execute(delete(WebhookSubscription).where(WebhookSubscription.id == healthy_id))


@pytest.mark.parametrize("round_no", ROUNDS)
@pytest.mark.parametrize("change", ["url", "event_types", "disable", "delete"])
def test_subscription_mutation_during_delivery(world, monkeypatch, round_no, change):
    delivery_id = _enqueue(world)
    sending, admin_committed = Event(), Event()
    sent = []

    def receive(subscription, outbox, **_kwargs):
        sent.append((subscription.url, outbox.id))
        sending.set()
        # HTTP 期间不持行锁；管理事务必须能先提交，包括删除订阅及其发件箱。
        assert admin_committed.wait(SYNC_TIMEOUT)
        sleep(Random(round_no).uniform(0.001, 0.02))

    def mutate():
        assert sending.wait(SYNC_TIMEOUT)
        with session_scope() as session:
            actor = session.get(User, world[0])
            if change == "delete":
                api.delete_subscription(world[1], user=actor, session=session)
            else:
                fields = {
                    "url": {"url": "https://8.8.4.4/new"},
                    "event_types": {"event_types": ["collect.failed"]},
                    "disable": {"enabled": False},
                }[change]
                api.update_subscription(world[1], api.SubscriptionUpdate(**fields), user=actor, session=session)
        admin_committed.set()

    monkeypatch.setattr(delivery, "send_webhook", receive)
    count, _ = _parallel(lambda: delivery.deliver_due(batch_size=1, budget_seconds=15), mutate)
    assert count == 1 and sent == [("https://8.8.8.8/old", delivery_id)]
    if change == "delete":
        with session_scope() as session:
            assert session.get(EventOutbox, delivery_id) is None
            assert session.get(WebhookSubscription, world[1]) is None
    else:
        _delivered(delivery_id)
    assert delivery.deliver_due(batch_size=1) == 0


@pytest.mark.parametrize("round_no", ROUNDS)
@pytest.mark.parametrize("change", ["url", "event_types", "disable", "delete"])
def test_scanner_skips_subscription_locked_by_admin(world, monkeypatch, round_no, change):
    delivery_id = _enqueue(world)
    admin_flushed, scan_finished = Event(), Event()
    sent = []

    def receive(subscription, outbox, **_kwargs):
        sent.append((subscription.url, outbox.id))

    def mutate():
        with session_scope() as session:
            actor = session.get(User, world[0])
            if change == "delete":
                api.delete_subscription(world[1], user=actor, session=session)
            else:
                fields = {
                    "url": {"url": "https://8.8.4.4/new"},
                    "event_types": {"event_types": ["collect.failed"]},
                    "disable": {"enabled": False},
                }[change]
                api.update_subscription(world[1], api.SubscriptionUpdate(**fields), user=actor, session=session)
            session.flush()
            admin_flushed.set()
            assert scan_finished.wait(SYNC_TIMEOUT)
            sleep(Random(round_no).uniform(0.001, 0.02))

    def scan():
        assert admin_flushed.wait(SYNC_TIMEOUT)
        count = delivery.deliver_due(batch_size=1, budget_seconds=15)
        scan_finished.set()
        return count

    monkeypatch.setattr(delivery, "send_webhook", receive)
    assert _parallel(mutate, scan) == [None, 0]
    assert sent == []
    if change == "delete":
        with session_scope() as session:
            assert session.get(WebhookSubscription, world[1]) is None
            assert session.get(EventOutbox, delivery_id) is None
    elif change == "disable":
        assert delivery.deliver_due(batch_size=1) == 0
        with session_scope() as session:
            row = session.get(EventOutbox, delivery_id)
            assert row.status == "pending" and row.attempts == 0
            api.update_subscription(
                world[1], api.SubscriptionUpdate(enabled=True), user=session.get(User, world[0]), session=session
            )
        assert delivery.deliver_due(batch_size=1) == 1
        _delivered(delivery_id)
    else:
        # event_types 只筛选新事件，不撤销已经入箱的事件快照。
        assert delivery.deliver_due(batch_size=1) == 1
        expected_url = "https://8.8.4.4/new" if change == "url" else "https://8.8.8.8/old"
        assert sent == [(expected_url, delivery_id)]
        _delivered(delivery_id)


def test_fair_scan_delivers_healthy_subscription_while_oldest_subscription_is_locked(world, monkeypatch):
    oldest_id = _enqueue(world)
    with session_scope() as session:
        healthy = WebhookSubscription(
            name=f"healthy-{new_id()}",
            url="https://8.8.4.4/healthy",
            event_types=["collect.completed"],
            enabled=True,
            secret_enc="unused-offline-fixture",
            created_by=world[0],
        )
        session.add(healthy)
        session.flush()
        healthy_id = healthy.id
    try:
        healthy_delivery = _enqueue((world[0], healthy_id))
        sent = []

        def receive(subscription, outbox, *, timeout_seconds, **kwargs):
            sent.append((subscription.id, outbox.id))
            # 忙订阅不能挡住健康订阅；并发请求不再按订阅数量切短期限。
            assert 0 < timeout_seconds <= 5

        monkeypatch.setattr(delivery, "send_webhook", receive)
        with session_scope() as session:
            session.scalar(select(WebhookSubscription).where(WebhookSubscription.id == world[1]).with_for_update())
            assert delivery.deliver_due() == 1
            assert sent == [(healthy_id, healthy_delivery)]
            oldest = session.get(EventOutbox, oldest_id)
            assert oldest.status == "pending" and oldest.attempts == 0
        _delivered(healthy_delivery)
    finally:
        with session_scope() as session:
            session.execute(delete(EventOutbox).where(EventOutbox.subscription_id == healthy_id))
            session.execute(delete(WebhookSubscription).where(WebhookSubscription.id == healthy_id))


@pytest.mark.parametrize("round_no", ROUNDS)
def test_business_outbox_and_delivery_commit_keep_history(world, monkeypatch, round_no):
    first_id = _enqueue(world)
    sending = Event()
    flushed = Barrier(3)
    sent, audit_ids = [], []

    def receive(_subscription, outbox, **_kwargs):
        sent.append(outbox.id)
        if outbox.id == first_id:
            sending.set()
            flushed.wait(timeout=SYNC_TIMEOUT)
            sleep(Random(round_no).uniform(0.001, 0.02))

    def write(index):
        assert sending.wait(SYNC_TIMEOUT)
        with session_scope() as session:
            row = audit.record(
                session, session.get(User, world[0]), "collect.completed", "datasource", new_id(), trigger="manual"
            )
            session.flush()
            audit_ids.append(row.id)
            flushed.wait(timeout=SYNC_TIMEOUT)
            sleep(Random(round_no * 3 + index).uniform(0.001, 0.02))

    monkeypatch.setattr(delivery, "send_webhook", receive)
    before = _verify_history()["checked_rows"]
    result = _parallel(
        lambda: delivery.deliver_due(batch_size=1, budget_seconds=15), lambda: write(1), lambda: write(2)
    )
    assert result[0] == 1
    assert delivery.deliver_due() == 2
    with session_scope() as session:
        rows = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == world[1])))
        assert len(rows) == 3 and {row.event_id for row in rows if row.id != first_id} == set(audit_ids)
        assert all(row.status == "delivered" and row.attempts == 1 for row in rows)
        assert sorted(sent) == sorted(row.id for row in rows)
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.id.in_(audit_ids))))
        assert len(audits) == 2 and all(row.chain_seq and row.row_hash for row in audits)
    assert _verify_history()["checked_rows"] == before + 2


@pytest.mark.parametrize("round_no", ROUNDS)
def test_failure_guard_reclaim_and_reconcile_emit_once(world, round_no):
    with session_scope() as session:
        source = Datasource(name=f"three-way-{new_id()}", kind="postgres", host="offline.invalid", port=1)
        session.add(source)
        session.flush()
        batch = ImportBatch(datasource_id=source.id, status="running", created_by=world[0])
        session.add(batch)
        session.flush()
        batch_id = batch.id
        job_id = jobs._enqueue(session, "import", {"batch_id": batch_id}, world[0])
        batch.job_id = job_id

    class OfflineFailure(Exception):
        pass

    # 同一 Job 的不同 attempt 都应有事件；同一 attempt 的三方写入只能产生一个。
    for attempt in (1, 2):
        with session_scope() as session:
            job = session.get(Job, job_id)
            job.status, job.attempts, job.error = "failed", attempt, "offline timeout"
            job.finished_at = utcnow()
            job.result = {"abandoned_governance": "pending"}
            batch = session.get(ImportBatch, batch_id)
            batch.status, batch.error, batch.progress = "running", None, {}
        barrier = Barrier(3)

        def start(index):
            barrier.wait(timeout=SYNC_TIMEOUT)
            sleep(Random(round_no * 7 + attempt * 3 + index).uniform(0, 0.015))

        def guarded():
            start(0)
            with pytest.raises(OfflineFailure):
                with events.failure_guard(
                    "lineage_import.failed", world[0], "import_batch", batch_id, job_id=job_id, attempt=attempt
                ):
                    raise OfflineFailure("offline failure")

        def reclaim():
            start(1)
            jobs._record_abandoned_governance(job_id, "import", {"batch_id": batch_id}, {}, "offline timeout")

        def reconcile():
            start(2)
            with session_scope() as session:
                importer.reconcile_batch(session, session.get(ImportBatch, batch_id))

        _parallel(guarded, reclaim, reconcile)
        with session_scope() as session:
            job, batch = session.get(Job, job_id), session.get(ImportBatch, batch_id)
            assert job.status == batch.status == "failed"
            assert job.result["abandoned_governance"] == "done"
            assert batch.progress["phase"] == "failed"
            audits = list(
                session.scalars(
                    select(AuditEvent).where(
                        AuditEvent.action == "lineage_import.failed", AuditEvent.object_id == batch_id
                    )
                )
            )
            keys = [(row.action, row.object_id, row.detail["job_id"], row.detail["attempt"]) for row in audits]
            assert len(keys) == len(set(keys)) == attempt
            assert sorted(row.detail["attempt"] for row in audits) == list(range(1, attempt + 1))
            rows = list(session.scalars(select(EventOutbox).where(EventOutbox.subscription_id == world[1])))
            assert len(rows) == attempt and {row.event_id for row in rows} == {row.id for row in audits}
    _verify_history()


@pytest.mark.parametrize("round_no", ROUNDS)
@pytest.mark.parametrize("winner", ["scan", "redeliver"])
def test_dead_letter_redelivery_and_scanner_claim_once(world, monkeypatch, round_no, winner):
    delivery_id = _enqueue(world, status="dead")
    redelivery_flushed, scan_finished = Event(), Event()
    sent = []

    def receive(_subscription, outbox, **_kwargs):
        sent.append(outbox.id)

    def redeliver():
        with session_scope() as session:
            item = api.redeliver(delivery_id, user=session.get(User, world[0]), session=session)
            assert item.dead_at is None
            redelivery_flushed.set()
            assert scan_finished.wait(SYNC_TIMEOUT)
            sleep(Random(round_no).uniform(0.001, 0.02))

    def scan():
        assert redelivery_flushed.wait(SYNC_TIMEOUT)
        # 重投事务尚未提交，扫描不能看到或认领它；也不能等在订阅锁上。
        count = delivery.deliver_due(batch_size=1, budget_seconds=15)
        scan_finished.set()
        return count

    monkeypatch.setattr(delivery, "send_webhook", receive)
    if winner == "scan":
        assert _parallel(redeliver, scan) == [None, 0]
    else:
        # 扫描正开始时提交重投，覆盖 READ COMMITTED 下的前后快照边界。
        barrier = Barrier(2)

        def committed_redelivery():
            with session_scope() as session:
                item = api.redeliver(delivery_id, user=session.get(User, world[0]), session=session)
                assert item.dead_at is None
                barrier.wait(timeout=SYNC_TIMEOUT)

        def concurrent_scan():
            barrier.wait(timeout=SYNC_TIMEOUT)
            sleep(Random(round_no).uniform(0, 0.015))
            return delivery.deliver_due(batch_size=1, budget_seconds=15)

        _parallel(committed_redelivery, concurrent_scan)
    delivery.deliver_due(batch_size=1)
    assert sent == [delivery_id]
    _delivered(delivery_id)
    assert delivery.deliver_due(batch_size=1) == 0
