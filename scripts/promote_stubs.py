"""Promote high-inbound stub nodes to real vault pages.

A "stub" is a wiki-link target with no underlying .md file. The kg.db
records each as `_stub/<title>.md` and tracks inbound links. Stubs with
many inbound links are valuable — they're concepts other notes already
reference but haven't yet defined.

This script generates skeleton .md files for stubs with >=N inbound links
so subsequent `kg index` runs resolve those wiki-links to real nodes.

Two skeleton types:
  Topics/<title>.md       (default — concept stubs)
  Projects/<title>.md     (when title starts with "project-" — project capsules)

Usage:
    python scripts/promote_stubs.py --dry-run [--threshold 5]
    python scripts/promote_stubs.py --apply   [--threshold 5]

After --apply, run `python scripts/evolution_cycle.py --quiet` to re-index
and verify the stubs disappeared from the lint. The script auto-bumps
mtimes of source files that wiki-link to the promoted stubs so a plain
incremental run re-resolves those links — no `--force` required.

Safety:
  * Default is --dry-run; --apply is required to write files.
  * Never overwrites an existing file. Reports skips clearly.
  * All generated files have a `status: stub-promoted` frontmatter flag
    so they're easy to grep + enrich later.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
VAULT_DEFAULT = Path.home() / "Development" / "Obsidian data update"
DATA_DEFAULT = Path.home() / ".local" / "share" / "knowledge-graph"

# Known Destinus projects (for project-* stub promotion).
# Source: ~/.claude/CLAUDE.md "Key Project Directories" section.
KNOWN_PROJECTS = {
    "wms": {
        "title": "WMS — D365 Warehouse Management",
        "summary": "D365 WMS implementation (Inventory to Deliver). Sprint-driven roll-out across NL10/ES20/DE20 entities.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/WMS",
    },
    "fd": {
        "title": "FD — Financial Dimensions",
        "summary": "D365 F&O Financial Dimensions remediation. CFO Bronte sponsor. Drives downstream RBAC + ExFlow.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/FIN",
    },
    "exflow": {
        "title": "ExFlow — AP Automation",
        "summary": "Truvio ExFlow implementation for AP invoice processing. Replaces Axtension after Oct 2028.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/EIP",
    },
    "mdm": {
        "title": "MDM — Master Data Management",
        "summary": "MDM framework (13 rules, 6 processes). Data quality remediation, AI Agent for item creation.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/MDM",
    },
    "rbac": {
        "title": "RBAC — D365 Role-Based Access Control",
        "summary": "Sysadmin remediation (44->4) for IPO readiness. Phase 1-5 lifecycle, control owner Pim.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/RBAC",
    },
    "ilnd": {
        "title": "ILND — Intelligent Legal NDA Dispatcher",
        "summary": "Legal NDA automation. Email -> AI classification -> DocuSign -> Jira pipeline.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/ILND",
    },
    "pbvr": {
        "title": "PBVR — Production BOM Visibility Report",
        "summary": "D365 3-Bucket Traveler report. Deployed Prod via SA.Global.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/PBVR",
    },
    "comstrat": {
        "title": "COMSTRAT — Commercial Strategy",
        "summary": "Commercial team workspace. CCS/PCWE Jira projects.",
        "jira": "https://destinus-bpo-jira.atlassian.net/jira/software/projects/CCS",
    },
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_paths() -> tuple[Path, Path]:
    vault = Path(os.environ.get("KG_VAULT_PATH") or VAULT_DEFAULT)
    data = Path(os.environ.get("KG_DATA_DIR") or DATA_DEFAULT)
    return vault, data


import re as _re

# Defense in depth: same newline-safe constraint as src/lib/wiki-links.ts.
# Even though _truncate_clean() runs first to drop trailing `[[X` fragments,
# we don't rely on that — keep the regex single-line so we can't reintroduce
# the original parser bug. \r is excluded alongside \n so legacy Mac CR-only
# line endings cannot widen the match across logical lines.
_WIKILINK = _re.compile(r"\[\[([^\]\r\n]+)\]\]")
_WIKILINK_DANGLING = _re.compile(r"\[\[[^\]\r\n]*$")


def _scrub_wiki_links(text: str) -> str:
    """Replace [[X]] / [[X|alias]] with X (or alias) AND strip any dangling
    [[X (truncated at the end) so quoted contexts don't introduce new
    wiki-link edges when re-parsed.

    Without this, a context line like "Topics: [[D365 F&O]], [[Finance]]"
    would create new wiki-link edges to D365 F&O and Finance from the file
    that quotes it, propagating stubs back into the graph. Truncated
    fragments like "[[Strate" at end-of-string also count to the parser
    once they're saved + re-read.
    """
    def _sub(m: "_re.Match[str]") -> str:
        inner = m.group(1)
        if "|" in inner:
            inner = inner.split("|", 1)[1]
        return inner

    text = _WIKILINK.sub(_sub, text)
    # Strip dangling truncated `[[X` fragments at end of string
    text = _WIKILINK_DANGLING.sub("", text).rstrip()
    return text


def _truncate_clean(text: str, limit: int = 200) -> str:
    """Truncate to ~limit chars but on a wiki-link-safe boundary."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # If the cut leaves an unclosed `[[`, retreat to before it
    last_open = cut.rfind("[[")
    last_close = cut.rfind("]]")
    if last_open > last_close:
        cut = cut[:last_open].rstrip()
    return cut


def fetch_top_stubs(db_path: Path, threshold: int) -> list[dict]:
    """Return [{stub_id, title, inbound, references: [{source_id, context}]}, ...]."""
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    c = db.cursor()
    rows = c.execute(
        """
        SELECT n.id AS id, COUNT(*) AS inbound FROM nodes n
        JOIN edges e ON e.target_id = n.id
        WHERE n.id LIKE '_stub/%'
        GROUP BY n.id HAVING inbound >= ?
        ORDER BY inbound DESC
        """,
        (threshold,),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        stub_id = r["id"]
        title = stub_id.removeprefix("_stub/").removesuffix(".md")
        refs = c.execute(
            "SELECT source_id, context FROM edges WHERE target_id = ? LIMIT 20",
            (stub_id,),
        ).fetchall()
        out.append({
            "stub_id": stub_id,
            "title": title,
            "inbound": r["inbound"],
            "references": [
                {
                    "source_id": e["source_id"],
                    "context": _scrub_wiki_links(
                        _truncate_clean((e["context"] or "").replace("\n", " ").strip())
                    ),
                }
                for e in refs
            ],
        })
    db.close()
    return out


def categorize(title: str) -> tuple[str, str | None]:
    """Return (kind, project_key). kind is 'project' or 'concept'."""
    if title.lower().startswith("project-"):
        key = title.lower().removeprefix("project-")
        return "project", key
    return "concept", None


def safe_filename(title: str) -> str:
    """Convert a title to a filesystem-safe filename component.

    The vault uses Obsidian conventions which permit `/` for nested paths and
    most punctuation in filenames. We only sanitize chars that break NTFS.
    """
    # NTFS-illegal: < > : " | ? * \  (forward slash IS preserved for nesting)
    illegal = '<>:"|?*\\'
    cleaned = "".join("_" if ch in illegal else ch for ch in title)
    return cleaned.strip()


def render_project(title: str, key: str, refs: list[dict]) -> str:
    meta = KNOWN_PROJECTS.get(key, {})
    full_title = meta.get("title", f"Project: {key}")
    summary = meta.get("summary", "(stub-promoted; fill in)")
    jira_url = meta.get("jira")
    today = datetime.now(timezone.utc).date().isoformat()

    lines = [
        "---",
        f'title: "{full_title}"',
        "type: project",
        f'project_key: "{key}"',
        "status: stub-promoted",
        f'created: "{today}"',
        f'updated: "{today}"',
        "confidence: medium",
        "---",
        "",
        f"# {full_title}",
        "",
        "## Summary",
        "",
        f"> {summary}",
        "",
    ]
    if jira_url:
        lines += ["## Source of Truth", "", f"- Jira board: {jira_url}", ""]
    lines += [
        "## Referenced From",
        "",
        f"This page was auto-promoted from a stub with {len(refs)} inbound links. "
        "It exists so wiki-links across the vault resolve. Enrich the Summary "
        "and add cross-references as the project evolves.",
        "",
    ]
    for r in refs[:10]:
        ctx = r["context"] or "(no context)"
        lines.append(f"- `{r['source_id']}` — {ctx}")
    lines.append("")
    lines.append("## Open Questions / Gaps")
    lines.append("")
    lines.append("- Promote `confidence: medium` -> `high` once the Summary is reviewed.")
    lines.append("")
    return "\n".join(lines)


def render_concept(title: str, refs: list[dict]) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    lines = [
        "---",
        f'title: "{title}"',
        "type: topic",
        "status: stub-promoted",
        f'created: "{today}"',
        f'updated: "{today}"',
        "confidence: low",
        "---",
        "",
        f"# {title}",
        "",
        "## Summary",
        "",
        "> (stub-promoted; fill in)",
        "",
        "## Referenced From",
        "",
        f"This page was auto-promoted from a stub with {len(refs)} inbound links. "
        "It exists so wiki-links across the vault resolve. The references below "
        "show the contexts where this topic is mentioned — use them as raw material "
        "for the Summary.",
        "",
    ]
    for r in refs[:15]:
        ctx = r["context"] or "(no context)"
        lines.append(f"- `{r['source_id']}` — {ctx}")
    lines.append("")
    lines.append("## Open Questions / Gaps")
    lines.append("")
    lines.append("- Replace placeholder Summary with content derived from the references above.")
    lines.append("- Promote `confidence: low` -> `medium` after review; -> `high` if verified against source.")
    lines.append("")
    return "\n".join(lines)


def plan_writes(vault: Path, stubs: list[dict]) -> list[dict]:
    """Return the list of files we'd create (or skip)."""
    plan: list[dict] = []
    for stub in stubs:
        kind, key = categorize(stub["title"])
        if kind == "project":
            target_dir = vault / "Projects"
            filename = safe_filename(stub["title"]) + ".md"
            content = render_project(stub["title"], key or stub["title"], stub["references"])
        else:
            target_dir = vault / "Topics"
            filename = safe_filename(stub["title"]) + ".md"
            content = render_concept(stub["title"], stub["references"])
        target = target_dir / filename
        plan.append({
            "stub_id": stub["stub_id"],
            "kind": kind,
            "target": target,
            "exists": target.exists(),
            "content": content,
            "inbound": stub["inbound"],
        })
    return plan


def force_reindex_link_sources(
    vault: Path, db_path: Path, promoted_stub_ids: list[str]
) -> int:
    """Bump mtimes of vault files that wiki-link to a promoted stub.

    Closes the closed-loop convergence gap (architect/1 finding):

    Without this, after promote_stubs writes Topics/X.md, the next
    incremental `kg index` parses the new file but does NOT re-parse
    source files that contain `[[X]]` (their mtimes are unchanged).
    Result: X stays orphan with 0 inbound until the user manually runs
    --force.

    Fix: query edges where target_id matches the promoted stubs, get
    distinct source_ids, and `os.utime()` each one. Next incremental run
    sees them as changed and re-resolves the wiki-links to the now-real
    targets.

    Returns the number of files touched.
    """
    if not db_path.exists() or not promoted_stub_ids:
        return 0
    db = sqlite3.connect(str(db_path))
    c = db.cursor()
    placeholders = ",".join("?" * len(promoted_stub_ids))
    rows = c.execute(
        f"SELECT DISTINCT source_id FROM edges WHERE target_id IN ({placeholders})",
        promoted_stub_ids,
    ).fetchall()
    db.close()
    now = time.time()
    touched = 0
    for (source_id,) in rows:
        # source_id is vault-relative
        full = vault / source_id
        if full.exists():
            try:
                os.utime(full, (now, now))
                touched += 1
            except OSError:
                pass
    return touched


def apply_plan(plan: list[dict], overwrite: bool) -> dict:
    created, updated, skipped = 0, 0, 0
    skipped_targets: list[str] = []
    for p in plan:
        if p["exists"] and not overwrite:
            skipped += 1
            skipped_targets.append(str(p["target"]))
            continue
        p["target"].parent.mkdir(parents=True, exist_ok=True)
        p["target"].write_text(p["content"], encoding="utf-8")
        if p["exists"]:
            updated += 1
        else:
            created += 1
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "skipped_targets": skipped_targets,
    }


def print_plan(plan: list[dict], overwrite: bool) -> None:
    print(f"Plan: {len(plan)} stub(s) above threshold")
    for p in plan:
        if p["exists"] and overwrite:
            marker = "OVERWRITE"
        elif p["exists"]:
            marker = "SKIP (exists)"
        else:
            marker = "CREATE"
        print(f"  [{marker:14}] {p['kind']:8}  inbound={p['inbound']:>3}  -> {p['target']}")


def main() -> int:
    args = sys.argv[1:]
    apply = "--apply" in args
    overwrite = "--overwrite" in args
    dry = "--dry-run" in args or not apply
    threshold = 5
    for i, a in enumerate(args):
        if a == "--threshold" and i + 1 < len(args):
            try:
                threshold = int(args[i + 1])
            except ValueError:
                pass

    vault, data = resolve_paths()
    db_path = data / "kg.db"
    if not db_path.exists():
        print(f"ERROR: kg.db not found: {db_path}", file=sys.stderr)
        return 3

    stubs = fetch_top_stubs(db_path, threshold)
    if not stubs:
        print(f"No stubs at or above threshold {threshold}. Nothing to do.")
        return 0

    plan = plan_writes(vault, stubs)
    print_plan(plan, overwrite)
    print()
    if dry:
        print("Dry run. Re-run with --apply (and --overwrite to update existing) to write files.")
        return 0

    result = apply_plan(plan, overwrite)
    print(
        f"Wrote {result['created']} new + updated {result['updated']}; "
        f"skipped {result['skipped']} (already exist; pass --overwrite to update)."
    )
    if result["skipped_targets"] and not overwrite:
        for t in result["skipped_targets"]:
            print(f"  skipped: {t}")

    # Closed-loop convergence (architect/1 finding from adversarial review):
    # Bump mtimes of every vault file that wiki-links to a promoted stub so
    # the next incremental `kg index` re-resolves those links to the now-real
    # targets. Without this, a plain incremental run would leave the new
    # pages orphan with 0 inbound until a manual --force.
    promoted_ids = [
        p["stub_id"] for p in plan
        if (not p["exists"] or overwrite)
    ]
    if result["created"] > 0 or result["updated"] > 0:
        touched = force_reindex_link_sources(vault, db_path, promoted_ids)
        print()
        print(
            f"Touched {touched} source file(s) so the next incremental run "
            "re-resolves their wiki-links."
        )
    print()
    print("Next: run a regular evolution cycle (no --force needed):")
    print("  python scripts/evolution_cycle.py --quiet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
