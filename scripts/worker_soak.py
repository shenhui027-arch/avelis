"""worker 长时间运行验证（只用于本地演示环境）。

在 backend 目录运行：
  uv run python ../scripts/worker_soak.py --minutes 60

做法：
- 把各数据源的采集间隔临时改为 1 分钟（结束后恢复）；建议先 `uv run python -m avelis.bootstrap demo --metrics`，
  这样视图变化会产生受影响的口径与复核任务；
- 以子进程启动 `python -m avelis.jobs`（轮询 20 秒）。
运行期间：
- 每分钟记录一次：worker 是否存活、内存、采集次数与失败数、任务状态、各数据源最近一次采集距今多久；
- 在 1/3 处重启 Valkey，验证 Redis 中断后任务不丢、worker 不退出；
- 在 2/3 处用 root 把 shop.daily_sales 改为净额，验证不手动采集也能由轮询发现变化，并生成影响与复核任务。
结束时：daily_sales 改回原定义，worker 停止。结果写入 .demo/worker-soak.json，并在终端输出摘要。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select, update

from avelis.config import REPO_ROOT
from avelis.db import session_scope
from avelis.models import ChangeEvent, ChangeSet, CollectionRun, Datasource, Job, ReviewTask

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import demo  # noqa: E402

V1 = "SELECT order_date, region, SUM(paid_amount) AS sales_amount FROM shop.orders GROUP BY order_date, region"
V2 = ("SELECT order_date, region, SUM(paid_amount - refund_amount) AS sales_amount "
      "FROM shop.orders GROUP BY order_date, region")


def rss_mb(pid: int) -> float | None:
    """进程常驻内存（MB）。Windows 上 venv 的 python.exe 是启动器，实际解释器是它的子进程：取两者中较大者。"""
    try:
        if os.name == "nt":
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid} or ParentProcessId={pid}' "
                                  "| Measure-Object WorkingSetSize -Maximum).Maximum"],
                                 capture_output=True, text=True).stdout.strip()
            return round(int(out) / 1024 / 1024, 1) if out else None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except (ValueError, OSError):
        return None
    return None


def alter_view(sql: str) -> None:
    conn = demo.sr_root(demo.load_env())
    with conn.cursor() as cur:
        cur.execute(f"ALTER VIEW shop.daily_sales AS {sql}")
    conn.close()


def sample(started: datetime, pid: int, alive: bool) -> dict:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope() as s:
        runs = s.execute(select(CollectionRun.status, func.count()).where(CollectionRun.started_at >= started)
                         .group_by(CollectionRun.status)).all()
        jobs = s.execute(select(Job.status, func.count()).where(Job.created_at >= started)
                         .group_by(Job.status)).all()
        lag = {}
        for ds in s.scalars(select(Datasource)):
            last = s.scalar(select(func.max(CollectionRun.started_at)).where(CollectionRun.datasource_id == ds.id))
            lag[ds.name] = round((now - last.replace(tzinfo=None)).total_seconds()) if last else None
        change_sets = s.scalar(select(func.count()).select_from(ChangeSet).where(ChangeSet.created_at >= started))
    return {"at": now.isoformat(timespec="seconds"), "worker_alive": alive, "rss_mb": rss_mb(pid) if alive else None,
            "runs": dict(runs), "jobs": dict(jobs), "seconds_since_last_run": lag, "change_sets": change_sets}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=int, default=60)
    args = parser.parse_args()
    total = args.minutes * 60
    env = demo.load_env()
    started = datetime.now(UTC).replace(tzinfo=None)

    with session_scope() as s:
        original = {ds.id: ds.collect_interval_minutes for ds in s.scalars(select(Datasource))}
        s.execute(update(Datasource).values(collect_interval_minutes=1))
    worker_env = {**os.environ, "AVELIS_WORKER_SCHEDULE_SECONDS": "20", "PYTHONIOENCODING": "utf-8"}
    log_path = demo.STATE / "worker-soak.log"
    demo.STATE.mkdir(exist_ok=True)
    log_file = log_path.open("w", encoding="utf-8")
    worker = subprocess.Popen([sys.executable, "-m", "avelis.jobs"], cwd=REPO_ROOT / "backend", env=worker_env,
                              stdout=log_file, stderr=subprocess.STDOUT)
    samples, events = [], []
    t0 = time.monotonic()
    redis_done = view_done = False
    try:
        while (elapsed := time.monotonic() - t0) < total:
            if not redis_done and elapsed >= total / 3:
                subprocess.run(["docker", "restart", f"{env['COMPOSE_PROJECT_NAME']}-valkey-1"], check=True,
                               capture_output=True)
                events.append({"at": round(elapsed), "event": "重启 Valkey"})
                redis_done = True
            if not view_done and elapsed >= total * 2 / 3:
                alter_view(V2)
                events.append({"at": round(elapsed), "event": "daily_sales 改为净额（未手动采集）",
                               "since": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds")})
                view_done = True
            samples.append(sample(started, worker.pid, worker.poll() is None))
            print(json.dumps(samples[-1], ensure_ascii=False), flush=True)
            time.sleep(60)
        samples.append(sample(started, worker.pid, worker.poll() is None))
    finally:
        worker_alive_at_end = worker.poll() is None
        worker.terminate()
        worker.wait(timeout=30)
        log_file.close()
        with session_scope() as s:
            for ds_id, minutes in original.items():
                s.execute(update(Datasource).where(Datasource.id == ds_id).values(collect_interval_minutes=minutes))

    detected = None
    if view_done:
        since = datetime.fromisoformat(next(e["since"] for e in events if "since" in e))
        with session_scope() as s:
            hit = s.execute(select(ChangeSet.created_at, ChangeSet.id).join(ChangeEvent, ChangeEvent.change_set_id
                                                                             == ChangeSet.id)
                            .where(ChangeSet.created_at >= since, ChangeEvent.kind == "definition_changed")
                            .order_by(ChangeSet.created_at).limit(1)).first()
            if hit:
                tasks = s.scalar(select(func.count()).select_from(ReviewTask).where(ReviewTask.change_set_id == hit.id))
                detected = {"seconds_after_change": round((hit.created_at.replace(tzinfo=None) - since)
                                                          .total_seconds()),
                            "change_set_id": hit.id, "review_tasks": tasks}
    alter_view(V1)
    rss = [x["rss_mb"] for x in samples if x["rss_mb"]]
    summary = {
        "minutes": args.minutes, "worker_alive_at_end": worker_alive_at_end, "events": events,
        "runs": samples[-1]["runs"], "jobs": samples[-1]["jobs"],
        "max_seconds_since_last_run": max(max(v for v in x["seconds_since_last_run"].values() if v is not None)
                                          for x in samples),
        "rss_mb": {"first": rss[0] if rss else None, "last": rss[-1] if rss else None,
                   "max": max(rss) if rss else None},
        "view_change_detected": detected,
        "log_errors": [line.strip() for line in log_path.read_text(encoding="utf-8").splitlines()
                       if " ERROR " in line or "Traceback" in line][:20],
    }
    (demo.STATE / "worker-soak.json").write_text(json.dumps({"summary": summary, "samples": samples},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
    print("SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
