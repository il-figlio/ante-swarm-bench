#!/usr/bin/env python3
"""Generate the synthetic fleet log corpus and ground-truth manifest.

Deterministic (fixed seed): re-running rewrites logs/ and tests/manifest.json
byte-identically.

    python3 tools/gen_logs.py

Corpus: one day of service logs (2026-09-15, 12:00-16:00 UTC) across 20
hosts under logs/host-NN/<service>.log. One incident is planted: a
db-primary connection-pool exhaustion on host-07 that cascades to 5xx on
three downstream hosts seconds later. Every host carries routine
WARN/ERROR noise all day so the incident is a triage job, not a grep for
"ERROR".
"""
import json
import random
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEED = 20260915
DAY = datetime(2026, 9, 15, tzinfo=timezone.utc)
T0 = DAY.replace(hour=12, minute=0, second=0)
T1 = DAY.replace(hour=16, minute=0, second=0)

HOSTS = [f"host-{i:02d}" for i in range(1, 21)]

# service -> log file <service>.log under the host dir
FLEET = {
    "host-01": ["api-gateway"],
    "host-02": ["api-gateway", "auth"],
    "host-03": ["api-gateway", "auth"],            # downstream of the incident
    "host-04": ["orders", "billing"],
    "host-05": ["cache", "worker"],
    "host-06": ["payments"],
    "host-07": ["db-primary", "db-metrics"],       # primary incident host
    "host-08": ["catalog", "search"],
    "host-09": ["notifier", "worker"],
    "host-10": ["api-gateway"],
    "host-11": ["orders", "payments"],             # downstream of the incident
    "host-12": ["billing", "worker"],
    "host-13": ["cache"],
    "host-14": ["auth", "api-gateway"],
    "host-15": ["worker"],
    "host-16": ["search", "recommendations"],      # downstream of the incident
    "host-17": ["notifier"],
    "host-18": ["catalog", "billing"],
    "host-19": ["worker", "queue"],
    "host-20": ["api-gateway", "notifier"],
}

# services whose requests depend on db-primary (so they can drown when it does)
DB_BACKED = {"api-gateway", "auth", "orders", "payments", "billing",
             "search", "recommendations", "catalog"}
# services that speak HTTP request logs; the rest log ops-style lines
HTTP_SVC = {"api-gateway", "auth", "orders", "payments", "billing",
            "search", "recommendations", "catalog", "notifier"}

PRIMARY_HOST = "host-07"
AFFECTED = {
    "host-07": "primary",
    "host-03": "downstream",
    "host-11": "downstream",
    "host-16": "downstream",
}
# how many seconds after the primary failure each downstream host starts erroring
DOWNSTREAM_DELAY_S = {"host-03": 9, "host-11": 21, "host-16": 14}
PRIMARY_SIG = "ERR_CONN_POOL_EXHAUSTED"
DOWNSTREAM_SIG = "ERR_UPSTREAM_TIMEOUT"

ENDPOINTS = {
    "api-gateway": [("GET", "/v1/orders"), ("POST", "/v1/auth/token"),
                    ("GET", "/v1/users/me"), ("GET", "/v1/catalog"),
                    ("POST", "/v1/checkout"), ("GET", "/v1/search"),
                    ("GET", "/healthz")],
    "auth": [("POST", "/v1/auth/token"), ("POST", "/v1/auth/refresh"),
             ("GET", "/v1/sessions"), ("DELETE", "/v1/sessions"),
             ("POST", "/v1/auth/verify")],
    "orders": [("POST", "/v1/orders"), ("GET", "/v1/orders"),
               ("GET", "/v1/orders/detail"), ("PATCH", "/v1/orders/status"),
               ("GET", "/v1/orders/history")],
    "payments": [("POST", "/v1/payments"), ("GET", "/v1/payments/status"),
                 ("POST", "/v1/refunds"), ("GET", "/v1/methods")],
    "billing": [("GET", "/v1/invoices"), ("POST", "/v1/invoices"),
                ("GET", "/v1/subscriptions"), ("POST", "/v1/usage")],
    "search": [("GET", "/v1/search"), ("GET", "/v1/suggest"),
               ("POST", "/v1/reindex")],
    "recommendations": [("GET", "/v1/recs"), ("GET", "/v1/recs/similar"),
                        ("POST", "/v1/recs/feedback")],
    "catalog": [("GET", "/v1/catalog"), ("GET", "/v1/products"),
                ("GET", "/v1/products/detail"), ("PUT", "/v1/products")],
    "notifier": [("POST", "/v1/notify"), ("GET", "/v1/templates"),
                 ("GET", "/v1/notify/status")],
}
EXTERNAL_UPSTREAMS = ["partner-payments", "geo-api", "smtp-relay", "stripe"]
ROUTINE_ERR_TOKENS = ["ERR_UPSTREAM_RST", "ERR_HANDLER", "ERR_DNS_LOOKUP",
                      "ERR_BAD_RESPONSE", "ERR_TLS"]

# (status, weight) for routine traffic; ~1% 5xx baseline noise everywhere
STATUS_NOISE = [(200, 0.780), (201, 0.040), (204, 0.020), (301, 0.020),
                (304, 0.030), (400, 0.030), (401, 0.020), (403, 0.010),
                (404, 0.024), (429, 0.006), (500, 0.006), (502, 0.004)]

QUERIES = [
    "SELECT id,total FROM orders WHERE tenant=$1 ORDER BY created_at DESC LIMIT 50",
    "UPDATE inventory SET qty=qty-$1 WHERE sku=$2",
    "SELECT * FROM sessions WHERE token=$1",
    "INSERT INTO payments (id,amount,status) VALUES ($1,$2,$3)",
    "SELECT sku,title FROM products WHERE tsv @@ plainto_tsquery($1)",
    "SELECT tenant,SUM(amount) FROM invoices GROUP BY tenant",
    "DELETE FROM cart_items WHERE expires_at < now()",
    "SELECT user_id,score FROM recs_index WHERE sku=$1 ORDER BY score DESC LIMIT 20",
]
WORKER_JOBS = ["email_send", "invoice_roll", "reindex_delta", "report_gen",
               "cart_sweep", "webhook_deliver"]


def fmt_ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def wchoice(rng, table):
    r = rng.random()
    acc = 0.0
    for v, w in table:
        acc += w
        if r < acc:
            return v
    return table[-1][0]


# ---------------------------------------------------------------- normal lines

def http_line(rng, svc, t):
    m, p = rng.choice(ENDPOINTS[svc])
    rid = f"{rng.getrandbits(28):07x}"
    status = wchoice(rng, STATUS_NOISE)
    dur = int(rng.expovariate(1 / 30)) + 3
    extra = ""
    if svc in DB_BACKED and rng.random() < 0.35:
        extra = " upstream=db-primary"
    elif rng.random() < 0.08:
        extra = f" upstream={rng.choice(EXTERNAL_UPSTREAMS)}"
    if status >= 500:
        level = "ERROR"
        extra += " " + rng.choice(ROUTINE_ERR_TOKENS)
    elif status == 429 or (status >= 400 and rng.random() < 0.4):
        level = "WARN"
    else:
        level = "INFO"
    return (f"{fmt_ts(t)} {level:<5} {svc} req={rid} method={m} path={p} "
            f"status={status} dur_ms={dur}{extra}")


def ops_line(rng, svc, t):
    ts = fmt_ts(t)
    if svc == "db-primary":
        q = rng.choice(QUERIES)
        dur = int(rng.expovariate(1 / 6)) + 1
        return f'{ts} INFO  db-primary qid={rng.getrandbits(28):07x} dur_ms={dur} rows={rng.randint(0, 400)} query="{q}"'
    if svc == "db-metrics":
        return (f"{ts} INFO  db-metrics connections={rng.randint(60, 190)} "
                f"qps={rng.randint(800, 5200)} repl_lag_ms={rng.randint(0, 40)} "
                f"pool_waiters={rng.randint(0, 3)}")
    if svc == "cache":
        hit = rng.random() < 0.93
        return (f"{ts} INFO  cache op={rng.choice(['GET', 'GET', 'SET', 'DEL'])} "
                f"key={rng.choice(['sess', 'cfg', 'rate', 'recs'])}:{rng.getrandbits(20):05x} "
                f"hit={1 if hit else 0} dur_us={rng.randint(20, 900)}")
    if svc == "worker":
        return (f"{ts} INFO  worker job={rng.choice(WORKER_JOBS)} "
                f"status=ok dur_ms={rng.randint(30, 2600)} attempt=1")
    if svc == "queue":
        return (f"{ts} INFO  queue topic={rng.choice(['orders', 'emails', 'webhooks', 'reindex'])} "
                f"depth={rng.randint(0, 380)} consumed={rng.randint(1, 60)}")
    raise ValueError(svc)


def warn_line(rng, svc, t):
    ts = fmt_ts(t)
    msg = rng.choice([
        f"slow_query dur_ms={rng.randint(800, 4200)}",
        f"cache_miss path={rng.choice(['/v1/recs', '/v1/catalog', '/v1/sessions'])}",
        f"rate_limit tenant=t{rng.randint(100, 999)} remaining={rng.randint(0, 15)}",
        f"retry attempt=2 upstream={rng.choice(EXTERNAL_UPSTREAMS)}",
        f"gc_pause_ms={rng.randint(80, 400)}",
        f"cert_expires_in_days={rng.randint(4, 20)}",
        f"queue_depth={rng.randint(380, 520)} threshold=400",
        f"mem_util={rng.randint(82, 95)}%",
        f"disk_util={rng.randint(78, 91)}%",
        f"slow_response dur_ms={rng.randint(1500, 5000)} upstream={rng.choice(EXTERNAL_UPSTREAMS)}",
    ])
    return f"{ts} WARN  {svc} {msg}"


def error_line(rng, svc, t):
    ts = fmt_ts(t)
    msg = rng.choice([
        f"job={rng.choice(WORKER_JOBS)} attempt=3 ERR_RETRY_EXCEEDED",
        f"auth_failed user=u{rng.randint(10000, 99999)} ERR_INVALID_TOKEN",
        f"upstream={rng.choice(EXTERNAL_UPSTREAMS)} status=502 ERR_UPSTREAM_RST",
        f"upstream={rng.choice(EXTERNAL_UPSTREAMS)} handshake ERR_TLS",
        f"diskio latency_ms={rng.randint(500, 1400)} ERR_DISK_LATENCY",
        f"handler exception=KeyError ERR_HANDLER path={rng.choice(['/v1/orders', '/v1/search', '/v1/notify'])}",
        f"upstream=queue status=503 ERR_QUEUE_FULL",
        f"dns_resolve host={rng.choice(EXTERNAL_UPSTREAMS)}.internal ERR_DNS_LOOKUP",
        f"payment_declined txn=tx{rng.randint(100000, 999999)} reason=insufficient_funds",
        f"upstream={rng.choice(EXTERNAL_UPSTREAMS)} status=504 ERR_BAD_RESPONSE",
    ])
    return f"{ts} ERROR {svc} {msg}"


# ------------------------------------------------------------- incident lines

def primary_incident_events(rng, t_start, t_end):
    """db-primary.log lines for the pool-exhaustion cascade.

    Returns [(t, line, flag)] where flag is 'sig' (contains the signature
    token), 'err' (incident error without the token) or 'ctx' (precursor /
    recovery context that is legitimate evidence but does not bound the
    incident window)."""
    ev = []
    # precursor: pool creeping up for ~95 s before exhaustion
    t = t_start - timedelta(seconds=95)
    util = 86
    while t < t_start:
        util = min(99, util + rng.randint(1, 4))
        ev.append((t, f"{fmt_ts(t)} WARN  db-primary pool_util={util}% "
                      f"active={int(util * 2)}/200 waiters={rng.randint(1, 8)}", "ctx"))
        t += timedelta(seconds=rng.uniform(5.5, 9.5))
    # the cascade
    t = t_start
    while t < t_end:
        r = rng.random()
        if r < 0.55:
            ev.append((t, f"{fmt_ts(t)} ERROR db-primary {PRIMARY_SIG} "
                          f"pool_size=200 waiters={rng.randint(20, 190)} "
                          f'oldest_wait_ms={rng.randint(9000, 30000)} query="{rng.choice(QUERIES)}"', "sig"))
        elif r < 0.78:
            ev.append((t, f"{fmt_ts(t)} ERROR db-primary query_abort "
                          f"dur_ms=30000 {PRIMARY_SIG} "
                          f"qid={rng.getrandbits(28):07x}", "sig"))
        else:
            ev.append((t, f"{fmt_ts(t)} WARN  db-primary pool_util=100% "
                          f"waiters={rng.randint(50, 200)}", "ctx"))
        t += timedelta(seconds=rng.uniform(0.7, 2.6))
    # recovery
    for w in (38, 11, 2):
        t += timedelta(seconds=rng.uniform(2.0, 5.0))
        ev.append((t, f"{fmt_ts(t)} WARN  db-primary pool_draining waiters={w}", "ctx"))
    t += timedelta(seconds=rng.uniform(2.0, 4.0))
    ev.append((t, f"{fmt_ts(t)} INFO  db-primary pool_recovered active=142/200 waiters=0", "ctx"))
    return ev


def metrics_incident_events(rng, t_start, t_end):
    ev = []
    t = t_start + timedelta(seconds=rng.uniform(2, 6))
    while t < t_end:
        if rng.random() < 0.4:
            ev.append((t, f"{fmt_ts(t)} ERROR db-metrics probe_failed "
                          f"upstream=db-primary dur_ms=5000 ERR_PROBE_TIMEOUT", "err"))
        else:
            ev.append((t, f"{fmt_ts(t)} WARN  db-metrics "
                          f"pool_waiters={rng.randint(40, 200)} qps={rng.randint(5, 300)}", "ctx"))
        t += timedelta(seconds=rng.uniform(3.5, 7.5))
    return ev


def downstream_incident_events(rng, svc, t_start, t_end):
    ev = []
    t = t_start
    while t < t_end:
        r = rng.random()
        rid = f"{rng.getrandbits(28):07x}"
        if r < 0.52:
            m, p = rng.choice(ENDPOINTS[svc])
            st = rng.choice([503, 503, 503, 502, 500])
            ev.append((t, f"{fmt_ts(t)} ERROR {svc} req={rid} method={m} path={p} "
                          f"status={st} dur_ms={rng.randint(9000, 30010)} "
                          f"upstream=db-primary {DOWNSTREAM_SIG}", "sig"))
        elif r < 0.72:
            ev.append((t, f"{fmt_ts(t)} WARN  {svc} circuit=db-primary "
                          f"state=OPEN failures={rng.randint(10, 120)}", "ctx"))
        elif r < 0.9:
            m, p = rng.choice(ENDPOINTS[svc])
            ev.append((t, f"{fmt_ts(t)} ERROR {svc} req={rid} method={m} path={p} "
                          f"status=503 dur_ms={rng.randint(2000, 8000)} "
                          f"upstream=db-primary retry=exhausted", "err"))
        else:
            ev.append((t, f"{fmt_ts(t)} WARN  {svc} shed_load "
                          f"upstream=db-primary queue={rng.randint(50, 300)}", "ctx"))
        t += timedelta(seconds=rng.uniform(0.9, 2.8))
    t += timedelta(seconds=rng.uniform(1.5, 4.0))
    ev.append((t, f"{fmt_ts(t)} INFO  {svc} circuit=db-primary state=CLOSED "
                  f"recovery=ok", "ctx"))
    return ev


# ----------------------------------------------------------------- decoys
# Sub-incident noise on a few clean hosts: enough that grepping levels or
# "around 14:00" is not the whole answer, never a sustained cascade with
# the incident signature. Manifest keeps these hosts affected=false.

def decoy_events(rng, host, svc):
    ev = []
    if host == "host-02" and svc == "api-gateway":
        t = DAY.replace(hour=13, minute=57, second=rng.randint(0, 40))
        for _ in range(14):
            t += timedelta(seconds=rng.uniform(6, 18))
            ev.append((t, f"{fmt_ts(t)} WARN  api-gateway rate_limit "
                          f"tenant=t{rng.randint(100, 999)} remaining=0 "
                          f"path=/v1/search", "ctx"))
    if host == "host-05" and svc == "cache":
        t = DAY.replace(hour=13, minute=31, second=rng.randint(0, 30))
        for _ in range(4):
            t += timedelta(seconds=rng.uniform(20, 60))
            ev.append((t, f"{fmt_ts(t)} ERROR cache diskio "
                          f"latency_ms={rng.randint(900, 2200)} ERR_DISK_LATENCY", "ctx"))
    if host == "host-09" and svc == "notifier":
        t = DAY.replace(hour=12, minute=47, second=rng.randint(0, 30))
        for _ in range(3):
            t += timedelta(seconds=rng.uniform(15, 50))
            ev.append((t, f"{fmt_ts(t)} ERROR notifier job=webhook_deliver "
                          f"attempt=3 ERR_RETRY_EXCEEDED", "ctx"))
    if host == "host-14" and svc == "auth":
        t = DAY.replace(hour=14, minute=3, second=rng.randint(0, 50))
        for _ in range(2):
            t += timedelta(seconds=rng.uniform(40, 90))
            ev.append((t, f"{fmt_ts(t)} ERROR auth upstream=ldap "
                          f"handshake ERR_TLS", "ctx"))
    if host == "host-18" and svc == "catalog":
        t = DAY.replace(hour=14, minute=21, second=rng.randint(0, 30))
        for _ in range(16):
            t += timedelta(seconds=rng.uniform(8, 22))
            if rng.random() < 0.25:
                ev.append((t, f"{fmt_ts(t)} WARN  catalog req={rng.getrandbits(28):07x} "
                              f"method=GET path=/v1/catalog status=429 "
                              f"dur_ms={rng.randint(4, 60)}", "ctx"))
            else:
                ev.append((t, f"{fmt_ts(t)} WARN  catalog rate_limit "
                              f"tenant=t{rng.randint(100, 999)} burst=1", "ctx"))
    if host == "host-19" and svc == "queue":
        t = DAY.replace(hour=13, minute=45, second=rng.randint(0, 30))
        depth = 380
        while t < DAY.replace(hour=14, minute=10):
            t += timedelta(seconds=rng.uniform(40, 90))
            depth = min(640, depth + rng.randint(5, 30))
            ev.append((t, f"{fmt_ts(t)} WARN  queue topic=orders "
                          f"depth={depth} threshold=400", "ctx"))
    if host == "host-20" and svc == "api-gateway":
        # a couple of scattered 5xx to a *different* upstream, straddling
        # the incident window: the signature and volume tell them apart
        for hh, mm in ((13, 58), (14, 1), (14, 4)):
            t = DAY.replace(hour=hh, minute=mm, second=rng.randint(0, 55))
            ev.append((t, f"{fmt_ts(t)} ERROR api-gateway req={rng.getrandbits(28):07x} "
                          f"method=POST path=/v1/checkout status=502 "
                          f"dur_ms={rng.randint(3000, 12000)} "
                          f"upstream=partner-payments ERR_UPSTREAM_RST", "ctx"))
    return ev


# ---------------------------------------------------------------- generation

def host_line_budget(rng, n_files):
    base = rng.randint(4300, 6400)
    return [max(1200, int(base * w)) for w in
            _split_weights(rng, n_files)]


def _split_weights(rng, n):
    if n == 1:
        return [1.0]
    if n == 2:
        a = rng.uniform(0.55, 0.75)
        return [a, 1 - a]
    ws = sorted(rng.uniform(0.2, 0.8) for _ in range(n - 1))
    out, prev = [], 0.0
    for w in ws + [1.0]:
        out.append(w - prev)
        prev = w
    return out


def normal_stream(rng, svc, n_lines):
    """(t, line, '') events at a low base rate with routine noise folded in."""
    ev = []
    rate = n_lines / (T1 - T0).total_seconds()
    t = T0 + timedelta(seconds=rng.uniform(0, 3))
    while t < T1 and len(ev) < n_lines:
        t += timedelta(seconds=rng.expovariate(rate))
        if t >= T1:
            break
        if svc in HTTP_SVC:
            ev.append((t, http_line(rng, svc, t), ""))
        else:
            ev.append((t, ops_line(rng, svc, t), ""))
        # routine WARN / ERROR folded into the stream
        r = rng.random()
        if r < 0.020:
            ev.append((t + timedelta(milliseconds=rng.randint(1, 400)),
                       warn_line(rng, svc, t), ""))
        elif r < 0.027:
            ev.append((t + timedelta(milliseconds=rng.randint(1, 400)),
                       error_line(rng, svc, t), ""))
    return ev


def generate():
    rng = random.Random(SEED)
    out_logs = ROOT / "logs"
    tmp_logs = ROOT / "logs.new"
    # build into logs.new/ then swap, so a failed run never leaves a
    # half-written corpus behind
    if tmp_logs.exists():
        shutil.rmtree(tmp_logs)
    tmp_logs.mkdir()

    # incident timing (primary first, downstreams a few seconds later)
    p_start = DAY.replace(hour=14, minute=2) + timedelta(seconds=rng.uniform(4, 18))
    p_end = p_start + timedelta(seconds=rng.uniform(200, 260))
    windows = {PRIMARY_HOST: (p_start, p_end)}
    for h, delay in DOWNSTREAM_DELAY_S.items():
        s = p_start + timedelta(seconds=delay)
        e = p_end + timedelta(seconds=rng.uniform(4, 22))
        windows[h] = (s, e)

    manifest = {
        "corpus": {
            "log_date": "2026-09-15",
            "span_utc": [fmt_ts(T0), fmt_ts(T1)],
            "hosts": 20,
            "seed": SEED,
            "generator": "tools/gen_logs.py",
        },
        "incident": {
            "primary_host": PRIMARY_HOST,
            "downstream_hosts": sorted(DOWNSTREAM_DELAY_S),
            "signatures": [PRIMARY_SIG, DOWNSTREAM_SIG],
            "summary": ("db-primary connection pool exhaustion on host-07 "
                        "cascaded to 5xx on downstream hosts host-03, host-11, "
                        "host-16 within ~9-21 s"),
        },
        "hosts": {},
    }

    for host in HOSTS:
        services = FLEET[host]
        hrng = random.Random(f"{SEED}:{host}")
        budgets = host_line_budget(hrng, len(services))
        hdir = tmp_logs / host
        hdir.mkdir(parents=True)
        hinfo = {"affected": host in AFFECTED,
                 "services": services,
                 "files": [f"logs/{host}/{s}.log" for s in services]}
        flagged = {s: [] for s in services}   # line numbers of incident-flagged lines
        win_start_ts = None
        win_end_ts = None
        first_sig = None                      # (t, file, lineno)

        win = windows.get(host)
        for svc, budget in zip(services, budgets):
            events = normal_stream(hrng, svc, budget)
            events += decoy_events(hrng, host, svc)
            if host == PRIMARY_HOST:
                if svc == "db-primary":
                    events += primary_incident_events(hrng, *win)
                else:
                    events += metrics_incident_events(hrng, *win)
            elif host in AFFECTED and svc in DB_BACKED:
                events += downstream_incident_events(hrng, svc, *win)

            # during the window some normal requests on db-backed services
            # also drown; mark them incident evidence too
            if host in AFFECTED and svc in DB_BACKED and svc != "db-primary":
                for i, (t, line, flag) in enumerate(events):
                    if flag or not (win[0] <= t <= win[1]):
                        continue
                    if "status=" in line and hrng.random() < 0.30:
                        line = line.replace("status=200", "status=503") \
                                   .replace("status=201", "status=503") \
                                   .replace("status=204", "status=503") \
                                   .replace("status=304", "status=503") \
                                   .replace("status=301", "status=503")
                        if "status=503" in line:
                            line = line.replace(" INFO  ", " ERROR ")
                            if "upstream=db-primary" not in line:
                                line += " upstream=db-primary"
                            line += f" {DOWNSTREAM_SIG}"
                            events[i] = (t, line, "sig")

            events.sort(key=lambda e: e[0])
            rel = f"logs/{host}/{svc}.log"
            lines = []
            for t, line, flag in events:
                lines.append(line)
                if flag:
                    flagged[svc].append(len(lines))
                    if flag in ("sig", "err"):
                        if win_start_ts is None or t < win_start_ts:
                            win_start_ts = t
                        if win_end_ts is None or t > win_end_ts:
                            win_end_ts = t
                    if flag == "sig" and (first_sig is None or t < first_sig[0]):
                        first_sig = (t, rel, len(lines))
            (hdir / f"{svc}.log").write_text("\n".join(lines) + "\n")

        if host in AFFECTED:
            hinfo["role"] = AFFECTED[host]
            hinfo["window_utc"] = {"start": fmt_ts(win_start_ts),
                                   "end": fmt_ts(win_end_ts)}
            hinfo["signatures"] = ([PRIMARY_SIG] if AFFECTED[host] == "primary"
                                   else [DOWNSTREAM_SIG])
            hinfo["first_occurrence"] = {
                "file": first_sig[1], "line": first_sig[2],
                "timestamp": fmt_ts(first_sig[0]),
            }
            hinfo["evidence"] = [
                {"file": f"logs/{host}/{s}.log",
                 "first_line": min(nums), "last_line": max(nums)}
                for s, nums in flagged.items() if nums
            ]
        else:
            hinfo["role"] = "clean"
            notes = {
                "host-02": "WARN rate_limit flurry 13:57-14:01, no 5xx",
                "host-05": "four isolated ERR_DISK_LATENCY ~13:31-13:33",
                "host-09": "three ERR_RETRY_EXCEEDED ~12:47",
                "host-14": "two ERR_TLS blips inside the incident window",
                "host-18": "WARN/429 rate-limit burst ~14:21-14:26",
                "host-19": "queue depth WARNs 13:45-14:10, no errors",
                "host-20": "three scattered partner-payments 502s straddling the window; different upstream and signature",
            }
            if host in notes:
                hinfo["note"] = notes[host]
        manifest["hosts"][host] = hinfo

    # top-level incident window = primary host's
    manifest["incident"]["window_utc"] = manifest["hosts"][PRIMARY_HOST]["window_utc"]

    # corpus built OK — swap it in atomically, then write the manifest
    if out_logs.exists():
        shutil.rmtree(out_logs)
    tmp_logs.rename(out_logs)
    (ROOT / "tests" / "manifest.json").write_text(
        json.dumps(manifest, indent=1) + "\n")

    total = sum(f.stat().st_size for f in out_logs.rglob("*.log"))
    nfiles = len(list(out_logs.rglob("*.log")))
    print(f"wrote {nfiles} log files, {total / 1e6:.1f} MB total")
    for h in HOSTS:
        sz = sum(f.stat().st_size for f in (out_logs / h).glob("*.log"))
        print(f"  {h}: {sz / 1024:6.0f} KB"
              + ("  <-- AFFECTED" if h in AFFECTED else ""))
    print(f"incident window: {manifest['incident']['window_utc']['start']}"
          f" .. {manifest['incident']['window_utc']['end']}")


if __name__ == "__main__":
    sys.exit(generate())
