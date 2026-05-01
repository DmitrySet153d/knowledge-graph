"""Knowledge-graph evolution cycle.

Wraps `kg index` with metrics capture and a rolling JSON-Lines log so the
persistent memory pipeline has a closed feedback loop:

  parse vault -> rebuild kg.db -> capture stats -> append to evolution log
                                                -> diff vs last run
                                                -> emit lint warnings

Run:
    python scripts/evolution_cycle.py [--force] [--no-rebuild] [--quiet]

--force        full re-index (drops sync state before running kg index)
--no-rebuild   skip the kg index call, only read state and append a log row
--quiet        suppress kg index stdout, only print the summary

Environment:
    KG_VAULT_PATH   path to Obsidian vault (required)
    KG_DATA_DIR     path to kg data dir (default ~/.local/share/knowledge-graph)

Outputs:
    output/kg_evolution_log.jsonl   append-only, one row per run
    output/kg_evolution_latest.json one row, the most recent run (for dashboards)

Exit codes:
    0 = clean (no HIGH lints)
    1 = HIGH lint findings (singleton communities, stub spike, output leak)
    2 = kg index failed
    3 = environment / db missing
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_DEFAULT = Path.home() / "Development" / "Obsidian data update"
DATA_DEFAULT = Path.home() / ".local" / "share" / "knowledge-graph"

LOG_PATH = REPO_ROOT / "output" / "kg_evolution_log.jsonl"
LATEST_PATH = REPO_ROOT / "output" / "kg_evolution_latest.json"

# Lint thresholds — extracted from compute_lints() per adversarial review
# Architect/4 (medium). Tuned for a ~1700-node vault. Re-tune for vaults of
# substantially different scale (e.g., 10K+ nodes).
STUB_SPIKE_RATIO = 1.25         # cur > prev * this  → MEDIUM stub_spike
NODE_DRIFT_RATIO = 0.10         # |cur - prev| > prev * this → MEDIUM node drift
PROMOTION_INBOUND_MIN = 5       # stub with >= this inbound = LOW promotion candidate
DB_PROBE_TIMEOUT = 15           # sqlite3.connect timeout (s) — handles concurrent writers


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_paths() -> tuple[Path, Path]:
    vault = Path(os.environ.get("KG_VAULT_PATH") or VAULT_DEFAULT)
    data = Path(os.environ.get("KG_DATA_DIR") or DATA_DEFAULT)
    return vault, data


def _resolve_npx() -> str:
    """Find the npx binary. On Windows, this is `npx.cmd`. shutil.which
    resolves both .exe and .cmd via the PATHEXT mechanism, so we get the
    actual path without needing shell=True (which has documented unsafety
    when combined with a list-style args).
    """
    return shutil.which("npx") or "npx"


def run_index(force: bool, quiet: bool, vault: Path, data: Path) -> tuple[int, dict, float]:
    """Invoke `npx tsx src/cli/index.ts index`. Returns (exit, stats_dict, seconds)."""
    # Top-level options (--vault-path / --data-dir) come BEFORE the subcommand
    # name per commander.js conventions.
    args = [
        _resolve_npx(), "tsx", "src/cli/index.ts",
        "--vault-path", str(vault).replace("\\", "/"),
        "--data-dir", str(data).replace("\\", "/"),
        "index",
    ]
    if force:
        args.append("--force")
    env = os.environ.copy()
    env["KG_VAULT_PATH"] = str(vault)
    env["KG_DATA_DIR"] = str(data)
    start = time.time()
    # shell=False is the safe Python convention. On Windows we resolve
    # `npx.cmd` explicitly via shutil.which so CreateProcess can find it
    # without invoking cmd.exe. The full path is in args[0] now.
    proc = subprocess.run(
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=False,
        env=env,
    )
    elapsed = time.time() - start
    if not quiet and proc.stdout:
        print(proc.stdout, end="")
    if proc.returncode != 0:
        sys.stderr.write(f"kg index failed (exit {proc.returncode}):\n{proc.stderr}\n")
        return proc.returncode, {}, elapsed
    # The CLI emits one JSON object on stdout. Find it.
    stats: dict = {}
    raw = proc.stdout.strip()
    try:
        if raw.startswith("{"):
            stats = json.loads(raw)
        else:
            # Find the last JSON object in stdout (ignore preamble noise)
            depth = 0
            start_idx = -1
            for i, ch in enumerate(raw):
                if ch == "{":
                    if depth == 0:
                        start_idx = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start_idx >= 0:
                        try:
                            stats = json.loads(raw[start_idx:i + 1])
                        except json.JSONDecodeError:
                            pass
    except json.JSONDecodeError as e:
        sys.stderr.write(f"warn: could not parse kg index stats: {e}\n")
    return 0, stats, elapsed


def probe_db(db_path: Path, vault: Path | None = None, data: Path | None = None) -> dict:
    """Read health metrics. Prefer the `kg probe` CLI (decoupled from kg.db
    schema), fall back to direct SQL if the CLI is unavailable or fails.

    Returns a dict that always includes _db_missing or _db_error on failure
    rather than raising — caller can still log the run and lint on the
    error itself.
    """
    if not db_path.exists():
        return {"_db_missing": True}

    # Path 1: ask the CLI (decoupled from schema; survives kg.db schema changes).
    cli_result = _probe_via_cli(vault, data)
    if cli_result is not None:
        return cli_result

    # Path 2: direct SQL fallback (in case the CLI is offline or rejected).
    try:
        db = sqlite3.connect(str(db_path), timeout=DB_PROBE_TIMEOUT)
    except sqlite3.OperationalError as e:
        return {"_db_error": f"connect: {e}"}
    try:
        return _probe_db_inner(db)
    except sqlite3.OperationalError as e:
        # `database is locked` or schema-mismatch errors don't crash the cycle.
        return {"_db_error": f"query: {e}"}
    finally:
        try:
            db.close()
        except Exception:
            pass


def _probe_via_cli(vault: Path | None, data: Path | None) -> dict | None:
    """Try `npx tsx src/cli/index.ts probe`. Returns None on any failure so
    the caller falls through to direct SQL — never raises.
    """
    args = [_resolve_npx(), "tsx", "src/cli/index.ts"]
    if vault is not None:
        args += ["--vault-path", str(vault).replace("\\", "/")]
    if data is not None:
        args += ["--data-dir", str(data).replace("\\", "/")]
    args.append("probe")
    try:
        env = os.environ.copy()
        if vault is not None:
            env["KG_VAULT_PATH"] = str(vault)
        if data is not None:
            env["KG_DATA_DIR"] = str(data)
        proc = subprocess.run(
            args,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            env=env,
            timeout=DB_PROBE_TIMEOUT * 4,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    try:
        # The CLI emits exactly one JSON object on stdout; if there's preamble
        # noise (rare), find the first balanced { ... }.
        if raw.startswith("{"):
            return json.loads(raw)
        depth = 0
        start_idx = -1
        for i, ch in enumerate(raw):
            if ch == "{":
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    return json.loads(raw[start_idx : i + 1])
    except json.JSONDecodeError:
        return None
    return None


def _probe_db_inner(db: sqlite3.Connection) -> dict:
    db.row_factory = sqlite3.Row
    c = db.cursor()
    out: dict = {}
    out["nodes_total"] = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    out["nodes_real"] = c.execute(
        "SELECT COUNT(*) FROM nodes WHERE id NOT LIKE '_stub/%'"
    ).fetchone()[0]
    out["nodes_stub"] = c.execute(
        "SELECT COUNT(*) FROM nodes WHERE id LIKE '_stub/%'"
    ).fetchone()[0]
    out["edges_total"] = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    out["communities_total"] = c.execute("SELECT COUNT(*) FROM communities").fetchone()[0]

    # Singleton communities are a smell — usually leaked artifacts.
    # A malformed JSON in node_ids (e.g., schema drift) must NOT crash the
    # whole probe — skip the row and continue.
    singletons = []
    malformed = 0
    for r in c.execute(
        "SELECT id, label, node_ids FROM communities ORDER BY id"
    ).fetchall():
        try:
            members = json.loads(r["node_ids"])
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        if len(members) <= 1:
            singletons.append({"id": r["id"], "label": r["label"], "members": members})
    out["communities_singleton"] = len(singletons)
    out["singleton_details"] = singletons
    if malformed:
        out["communities_malformed"] = malformed

    # Top stubs by inbound link count — these are high-value missing nodes
    out["top_stubs"] = [
        {"id": r["id"], "inbound": r["inbound"]}
        for r in c.execute(
            """
            SELECT n.id AS id, COUNT(*) AS inbound FROM nodes n
            JOIN edges e ON e.target_id = n.id
            WHERE n.id LIKE '_stub/%'
            GROUP BY n.id ORDER BY inbound DESC LIMIT 10
            """
        ).fetchall()
    ]

    # Pollution canaries (transient artifact dirs that should never have nodes)
    leaked: dict[str, int] = {}
    for prefix in ("output/", "scripts/", "vault_backup_", "_FileOrganizer2000/"):
        n = c.execute(
            "SELECT COUNT(*) FROM nodes WHERE id LIKE ?", (prefix + "%",)
        ).fetchone()[0]
        if n:
            leaked[prefix.rstrip("/")] = n
    out["leaked_artifact_nodes"] = leaked

    # Section breakdown (for trend tracking)
    sections: dict[str, int] = {}
    for r in c.execute(
        """
        SELECT
          CASE WHEN id LIKE '%/%' THEN substr(id, 1, instr(id, '/')-1) ELSE id END AS section,
          COUNT(*) AS n
        FROM nodes WHERE id NOT LIKE '_stub/%'
        GROUP BY section
        """
    ).fetchall():
        sections[r["section"]] = r["n"]
    out["sections"] = sections

    # Most recent indexed_at = "rebuild end timestamp"
    row = c.execute(
        "SELECT MAX(indexed_at) AS m FROM sync"
    ).fetchone()
    out["last_indexed_at_ms"] = row["m"]

    # Top 5 bridges (drift signal — bridge identity changes over time signal pipeline shifts)
    # Lazy import — graphology runs inside the kg CLI, not here. Skip in DB probe.

    return out


def read_last_log_row() -> dict | None:
    """Return the LAST successfully-parsed JSON line from the evolution log.

    Per-line try/except so a partial / corrupted final line (e.g., from a
    crashed prior run) doesn't silently disable drift detection. Without
    this, JSONDecodeError for the WHOLE file caused the function to return
    None, which masked all stub_spike / node_count_drift lints for the
    next cycle (architect/3 finding).
    """
    if not LOG_PATH.exists():
        return None
    last: dict | None = None
    bad_lines = 0
    try:
        with LOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    last = json.loads(line)
                except json.JSONDecodeError:
                    bad_lines += 1
                    continue
    except OSError:
        return None
    if bad_lines:
        sys.stderr.write(
            f"warn: skipped {bad_lines} malformed JSONL line(s) in "
            f"{LOG_PATH.name}\n"
        )
    return last


def compute_lints(probe: dict, prev: dict | None) -> list[dict]:
    """Detect drift and pollution. Each lint = {severity, code, message, evidence}."""
    lints: list[dict] = []

    if probe.get("communities_singleton", 0) > 0:
        lints.append({
            "severity": "HIGH",
            "code": "singleton_community",
            "message": (
                f"{probe['communities_singleton']} singleton community(ies) — "
                "almost always leaked artifacts. Inspect singleton_details."
            ),
            "evidence": probe.get("singleton_details", []),
        })

    leaked = probe.get("leaked_artifact_nodes", {})
    if leaked:
        lints.append({
            "severity": "HIGH",
            "code": "artifact_leak",
            "message": (
                "Transient artifact directories were indexed. "
                "Update parser EXCLUDED_DIRS or remove the .md files."
            ),
            "evidence": leaked,
        })

    # Stub growth > 25% since last run = drift signal.
    # Use float comparison, NOT int(prev * ratio) — for prev=3, int(3 * 1.25) = 3
    # so any cur >= 3 triggers (including 3 == prev, no growth at all).
    if prev:
        prev_stubs = prev.get("probe", {}).get("nodes_stub", 0)
        cur_stubs = probe.get("nodes_stub", 0)
        if prev_stubs > 0 and cur_stubs > prev_stubs * STUB_SPIKE_RATIO:
            pct = ((STUB_SPIKE_RATIO - 1) * 100)
            lints.append({
                "severity": "MEDIUM",
                "code": "stub_spike",
                "message": (
                    f"Stub count grew {prev_stubs} -> {cur_stubs} "
                    f"(+{cur_stubs - prev_stubs}, > {pct:.0f}%). "
                    "Wiki-link integrity may be degrading."
                ),
                "evidence": {"prev": prev_stubs, "current": cur_stubs},
            })

    # Real-node count change > 10% (suspicious — vault rebuild may have failed).
    # Float comparison, same rationale as stub_spike.
    if prev:
        prev_real = prev.get("probe", {}).get("nodes_real", 0)
        cur_real = probe.get("nodes_real", 0)
        if prev_real > 0 and abs(cur_real - prev_real) > prev_real * NODE_DRIFT_RATIO:
            pct = NODE_DRIFT_RATIO * 100
            lints.append({
                "severity": "MEDIUM",
                "code": "node_count_drift",
                "message": (
                    f"Real-node count changed {prev_real} -> {cur_real} "
                    f"({cur_real - prev_real:+d}, > {pct:.0f}%). "
                    "Investigate vault rebuild log or filesystem changes."
                ),
                "evidence": {"prev": prev_real, "current": cur_real},
            })

    # Top stubs that have crossed >= 5 inbound = candidates for promotion to real nodes
    promotion_candidates = [
        s for s in probe.get("top_stubs", []) if s["inbound"] >= PROMOTION_INBOUND_MIN
    ]
    if promotion_candidates:
        lints.append({
            "severity": "LOW",
            "code": "stub_promotion_candidates",
            "message": (
                f"{len(promotion_candidates)} stubs have >= {PROMOTION_INBOUND_MIN} "
                "inbound links. Consider creating these nodes (kg_create_node) or "
                "symlinking from project memory."
            ),
            "evidence": promotion_candidates,
        })

    # If probe failed (DB locked / corrupted), flag it explicitly so the cycle
    # row records it and the user knows the metrics are stale.
    if probe.get("_db_error") or probe.get("_db_missing"):
        err = probe.get("_db_error") or "kg.db not found"
        lints.append({
            "severity": "HIGH",
            "code": "probe_failure",
            "message": f"kg.db probe failed: {err}",
            "evidence": {"error": err},
        })

    return lints


def summarize(row: dict) -> str:
    p = row.get("probe", {})
    s = row.get("index_stats", {})
    lints = row.get("lints", [])
    lines = [
        f"Knowledge-graph evolution cycle @ {row['timestamp']}",
        f"  Nodes: {p.get('nodes_real', '?')} real + {p.get('nodes_stub', '?')} stubs = {p.get('nodes_total', '?')}",
        f"  Edges: {p.get('edges_total', '?')}",
        f"  Communities: {p.get('communities_total', '?')} (singletons: {p.get('communities_singleton', 0)})",
        f"  Index step: indexed={s.get('nodesIndexed', 0)} skipped={s.get('nodesSkipped', 0)} "
        f"edges_added={s.get('edgesIndexed', 0)} stubs_added={s.get('stubNodesCreated', 0)} "
        f"in {row.get('index_seconds', 0):.1f}s",
    ]
    if lints:
        lines.append(f"  Lint findings: {len(lints)}")
        for L in lints:
            lines.append(f"    [{L['severity']}] {L['code']}: {L['message']}")
    else:
        lines.append("  Lint findings: 0 (clean)")
    return "\n".join(lines)


def main() -> int:
    args = sys.argv[1:]
    force = "--force" in args
    no_rebuild = "--no-rebuild" in args
    quiet = "--quiet" in args

    vault, data = resolve_paths()
    if not vault.exists():
        sys.stderr.write(f"ERROR: vault path does not exist: {vault}\n")
        return 3
    db_path = data / "kg.db"

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    index_stats: dict = {}
    index_seconds = 0.0
    if not no_rebuild:
        rc, index_stats, index_seconds = run_index(force, quiet, vault, data)
        if rc != 0:
            return 2

    probe = probe_db(db_path, vault=vault, data=data)
    prev = read_last_log_row()
    lints = compute_lints(probe, prev)

    row = {
        "timestamp": utcnow_iso(),
        "vault_path": str(vault),
        "data_dir": str(data),
        "force": force,
        "no_rebuild": no_rebuild,
        "index_stats": index_stats,
        "index_seconds": round(index_seconds, 2),
        "probe": probe,
        "lints": lints,
    }

    # Atomic append to JSONL + write the latest snapshot
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    LATEST_PATH.write_text(
        json.dumps(row, indent=2),
        encoding="utf-8",
    )

    print(summarize(row))
    print(f"Log: {LOG_PATH}")
    print(f"Latest: {LATEST_PATH}")

    high_lints = [L for L in lints if L["severity"] == "HIGH"]
    return 1 if high_lints else 0


if __name__ == "__main__":
    sys.exit(main())
