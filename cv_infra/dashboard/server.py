"""``cv-infra dashboard`` — the operator's view of this verification host, as a web app.

A LENS, not a module: it adds nothing to the infrastructure. It reads two things and
writes none —

* the run history ``cv-infra verify`` keeps (``cv_infra.history``), opened READ-ONLY
  per request: requests, results, per-case timing, and the host samples verify took
  while its cases ran;
* the host itself, live, at the moment the page asks (``/api/live``) — nothing is
  stored; the page draws what it has seen since it was opened.

It can be started whenever an operator wants to look and stopped afterwards: the
history is there either way. A stdlib HTTP server, no framework, no CDN (the page is one
HTML file, one script, one stylesheet in ``static/``).

It binds to 127.0.0.1 by default: the dashboard shows repositories, commits and host
state, and an operator reaches it through an SSH tunnel
(``ssh -L 8765:localhost:8765 <host>``) rather than by exposing a port. ``--host``
changes that deliberately.

The JSON API (all read-only, all GET):

    /api/overview?window=S   KPIs over the last S seconds, per-repository and per-day
                             breakdowns, the runs in flight, the host right now
    /api/runs?window=S       the runs (requests) that started in the window, newest first
    /api/runs/<run_id>       one run: request, summary, every case run, its report
    /api/series?window=S[&run_id=ID][&buckets=N]
                             host + scheduling time series, bucketed to at most N points
    /api/live                the host right now
"""

from __future__ import annotations

import json
import mimetypes
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cv_infra import history

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_WINDOW_S = 86400.0
MAX_BUCKETS = 720
DAY_S = 86400.0


# --- the numbers the pages show (pure functions of store rows) ------------------------


def overview(rows: list[dict], *, now: float, window_s: float, host: Mapping[str, Any]) -> dict:
    """KPIs over the finished runs in the window, plus the breakdowns the pages chart."""
    done = [row for row in rows if row["status"] == "done"]
    durations = [row["ended_at"] - row["started_at"] for row in done if row["ended_at"]]
    cases = {
        key: sum(row[f"cases_{key}"] or 0 for row in done) for key in ("pass", "fail", "error")
    }
    judged = cases["pass"] + cases["fail"]
    return {
        "now": now,
        "window_s": window_s,
        "host": dict(host),
        "active": [row for row in rows if row["status"] == "running"],
        "kpi": {
            "runs": len(rows),
            "runs_done": len(done),
            "runs_passed": sum(1 for row in done if row["outcome"] == "pass"),
            "runs_stale": sum(1 for row in rows if row["status"] == "stale"),
            "cases": cases,
            "case_pass_rate": None if not judged else cases["pass"] / judged,
            "mean_duration_s": None if not durations else sum(durations) / len(durations),
            "busy_s": sum(durations),
            "peak_concurrency": max((row["peak_concurrency"] or 0 for row in done), default=0),
        },
        "by_repo": _by_repo(done),
        "daily": _daily(done, now=now, window_s=window_s),
    }


def _by_repo(done: list[dict]) -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in done:
        groups[row["repo"] or "?"].append(row)
    table = []
    for repo, runs in sorted(groups.items()):
        durations = [row["ended_at"] - row["started_at"] for row in runs if row["ended_at"]]
        table.append(
            {
                "repo": repo,
                "runs": len(runs),
                "passed": sum(1 for row in runs if row["outcome"] == "pass"),
                "cases_pass": sum(row["cases_pass"] or 0 for row in runs),
                "cases_fail": sum(row["cases_fail"] or 0 for row in runs),
                "cases_error": sum(row["cases_error"] or 0 for row in runs),
                "mean_duration_s": None if not durations else sum(durations) / len(durations),
                "last_started_at": max(row["started_at"] for row in runs),
            }
        )
    return table


def _daily(done: list[dict], *, now: float, window_s: float) -> list[dict]:
    """One bucket per local day in the window: runs by outcome and busy seconds."""
    first = _day_start(now - window_s)
    days = []
    day = first
    while day <= now:
        days.append({"day": day, "pass": 0, "fail": 0, "error": 0, "busy_s": 0.0})
        day = _day_start(day + DAY_S + 3600)  # +1 h survives a DST day of 23 h
    for row in done:
        index = next(
            (
                i
                for i, bucket in reversed(list(enumerate(days)))
                if row["started_at"] >= bucket["day"]
            ),
            None,
        )
        if index is None:
            continue
        outcome = row["outcome"] if row["outcome"] in ("pass", "fail") else "error"
        days[index][outcome] += 1
        days[index]["busy_s"] += (row["ended_at"] or row["started_at"]) - row["started_at"]
    return days


def _day_start(t: float) -> float:
    local = time.localtime(t)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def series(samples: list[dict], *, since: float, until: float, buckets: int) -> list[dict]:
    """Bucket samples into at most ``buckets`` points.

    Per bucket: GPU utilisation and load are means, memory is the peak, ``running`` is
    the SUM over runs of each run's LAST in-flight count in the bucket — what was in
    flight together at (about) one moment; summing each run's bucket PEAK instead
    overstated it whenever two runs peaked at different times in one wide bucket (seen:
    "9" for two runs whose peaks were 4 and 5 minutes apart). ``level`` is the highest
    scheduler level any run held. Empty buckets are left out — a gap is drawn as a gap.
    ``samples`` must be in time order (``Store.samples`` returns them so)."""
    width = max((until - since) / max(buckets, 1), 1e-9)
    grouped: dict[int, list[dict]] = defaultdict(list)
    for sample in samples:
        grouped[min(int((sample["t"] - since) / width), buckets - 1)].append(sample)
    points = []
    for index in sorted(grouped):
        rows = grouped[index]
        per_run: dict[str, int] = {}
        for row in rows:
            if row["run_id"] is not None and row["running"] is not None:
                per_run[row["run_id"]] = row["running"]  # time order: the last one wins
        used_ram = [
            row["ram_total_mib"] - row["ram_available_mib"]
            for row in rows
            if row["ram_total_mib"] is not None and row["ram_available_mib"] is not None
        ]
        points.append(
            {
                "t": since + (index + 0.5) * width,
                "gpu_util_pct": _mean(row["gpu_util_pct"] for row in rows),
                "gpu_used_mib": _max(row["gpu_used_mib"] for row in rows),
                "gpu_total_mib": _max(row["gpu_total_mib"] for row in rows),
                "ram_used_mib": max(used_ram) if used_ram else None,
                "ram_total_mib": _max(row["ram_total_mib"] for row in rows),
                "load1": _mean(row["load1"] for row in rows),
                "running": sum(per_run.values()),
                "level": _max(row["level"] for row in rows),
            }
        )
    return points


def _mean(values: Any) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _max(values: Any) -> float | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


# --- HTTP -----------------------------------------------------------------------------


class Dashboard:
    """The app: where the history is, a clock, and a live-host reader.

    The history is opened READ-ONLY per request (SQLite refuses writes through that
    connection) and closed again: the dashboard never holds the file, never writes it,
    and sees a DB that verify creates after the server started."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], float] = time.time,
        snapshot: Callable[[], dict] = history.host_snapshot,
    ) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self.snapshot = snapshot

    def handle(self, path: str) -> tuple[int, str, bytes]:
        """``(status, content type, body)`` for one GET."""
        url = urlparse(path)
        query = {key: values[-1] for key, values in parse_qs(url.query).items()}
        try:
            if url.path == "/api/live":
                return _json(HTTPStatus.OK, {"now": self.clock(), "host": self.snapshot()})
            if url.path.startswith("/api/"):
                with self._history() as store:
                    return self._api(store, url.path, query)
            return self._static(url.path)
        except ValueError as exc:  # a malformed query parameter
            return _json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

    @contextmanager
    def _history(self) -> Iterator[history.Store | None]:
        """The run log, read-only — or ``None`` while verify has never written one."""
        if not self.db_path.is_file():
            yield None
            return
        store = history.Store(self.db_path, readonly=True)
        try:
            yield store
        finally:
            store.close()

    def _api(
        self, store: history.Store | None, path: str, query: dict[str, str]
    ) -> tuple[int, str, bytes]:
        now = self.clock()
        window = float(query.get("window", DEFAULT_WINDOW_S))
        runs_in = (lambda since: []) if store is None else (
            lambda since: store.runs(since=since, now=now)
        )  # fmt: skip
        if path == "/api/overview":
            body = overview(runs_in(now - window), now=now, window_s=window, host=self.snapshot())
            body["history"] = {"path": str(self.db_path), "present": store is not None}
            return _json(HTTPStatus.OK, body)
        if path == "/api/runs":
            return _json(HTTPStatus.OK, {"now": now, "runs": runs_in(now - window)})
        run_id = path.rsplit("/", 1)[-1] if path.startswith("/api/runs/") else query.get("run_id")
        detail = None if store is None or not run_id else store.run(run_id, now=now)
        if path.startswith("/api/runs/"):
            if detail is None:
                return _json(HTTPStatus.NOT_FOUND, {"error": "no such run"})
            return _json(HTTPStatus.OK, detail)
        if path == "/api/series":
            if run_id and detail is None:
                return _json(HTTPStatus.NOT_FOUND, {"error": "no such run"})
            if detail is not None:
                since, until = detail["started_at"], detail["ended_at"] or now
            else:
                since, until = now - window, now
            buckets = max(min(int(query.get("buckets", MAX_BUCKETS)), MAX_BUCKETS), 1)
            samples = (
                [] if store is None else store.samples(since=since, until=until, run_id=run_id)
            )
            spans = [
                {"run_id": row["run_id"], "repo": row["repo"], "start": row["started_at"],
                 "end": row["ended_at"] or now, "outcome": row["outcome"]}
                for row in ([] if run_id else runs_in(since - DAY_S))
                if (row["ended_at"] or now) >= since
            ]  # fmt: skip
            body = {
                "since": since,
                "until": until,
                "points": series(samples, since=since, until=until, buckets=buckets),
                "runs": spans,
            }
            return _json(HTTPStatus.OK, body)
        return _json(HTTPStatus.NOT_FOUND, {"error": f"no endpoint {path}"})

    def _static(self, path: str) -> tuple[int, str, bytes]:
        name = "index.html" if path in ("/", "/index.html") else path.removeprefix("/static/")
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return HTTPStatus.NOT_FOUND, "text/plain; charset=utf-8", b"not found"
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if kind.startswith("text/") or kind.endswith("javascript"):
            kind += "; charset=utf-8"
        return HTTPStatus.OK, kind, target.read_bytes()


def _json(status: int, body: Any) -> tuple[int, str, bytes]:
    return status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False).encode()


def make_handler(app: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
            status, kind, body = app.handle(self.path)
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
            """Quiet: the page polls every few seconds."""

    return Handler


def serve(
    db_path: Path,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    stop: threading.Event | None = None,
    ready: Callable[[ThreadingHTTPServer], None] = lambda server: None,
) -> None:
    """Serve until ``stop`` is set (or forever, with Ctrl-C). Nothing runs in the
    background: every number on the page is read when the page asks for it."""
    stop = stop or threading.Event()
    server = ThreadingHTTPServer((host, port), make_handler(Dashboard(db_path)))
    watcher = threading.Thread(target=lambda: (stop.wait(), server.shutdown()), daemon=True)
    watcher.start()
    print(
        f"cv-infra dashboard: http://{host}:{server.server_address[1]}/  (history {db_path})",
        flush=True,
    )
    ready(server)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stop.set()
        server.server_close()
