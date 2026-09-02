#!/usr/bin/env python3
"""Interactive review inbox: walk through new + backlog postings, record verdicts.

Run from the repo root:
    .venv/bin/python -m src.review

Each posting is shown one at a time — the moment it is displayed it counts as
"surfaced". You answer:

    y  -> interested (moves to your apply list)
    n  -> not interested (ask why, then pick a reason code; the free-text
          reason is suggested into criteria.md, after you approve, and the
          posting is never surfaced again)
    s  -> skip (keeps it in the backlog)
    b  -> block this org (not interested here, plus every other open
          posting from the same employer, plus the employer is added to
          config/blocklist.yaml so future scans skip it entirely)
    q  -> quit (everything already shown is saved as surfaced; the rest stays new)

Nothing is dropped from consideration without an explicit y/n/b here, so a
job you haven't reacted to stays in the backlog no matter how long you're
away. The one exception is cosmetic: a role posted once per locale is shown
once, and your verdict applies to the copy you were shown.
"""

import json
import sqlite3
from datetime import date
from pathlib import Path

import yaml

from career_radar.config import CONFIG_DIR
from career_radar.core import dedupe, report


def parse_verdict(raw: str) -> str | None:
    """Map a raw keystroke to a canonical verdict, or None if unrecognized.

    yes | no | skip | block | quit. Blank and 's'/'skip' both mean skip —
    safe by default (never judges or drops a posting on an ambiguous
    keypress).
    """
    key = raw.strip().lower()
    if key in ("y", "yes"):
        return "yes"
    if key in ("n", "no"):
        return "no"
    if key in ("s", "skip", ""):
        return "skip"
    if key in ("b", "block"):
        return "block"
    if key in ("q", "quit"):
        return "quit"
    return None


_REASON_CODES = {
    "s": "seniority",
    "seniority": "seniority",
    "o": "org-fit",
    "org-fit": "org-fit",
    "d": "domain",
    "domain": "domain",
    "r": "role-shape",
    "role-shape": "role-shape",
    "c": "comp",
    "comp": "comp",
    "x": "other",
    "other": "other",
}


def parse_reason_code(raw: str) -> str | None:
    """Map a raw keystroke/word to a canonical reason code, or None.

    seniority | org-fit | domain | role-shape | comp | other. Blank means
    'other' — safe by default, same convention as parse_verdict's skip.
    """
    key = raw.strip().lower()
    if key == "":
        return "other"
    return _REASON_CODES.get(key)


def build_criteria_line(title: str, employer: str, reason: str, today: str) -> str:
    """The dated criteria.md line a rejection should append."""
    return f'- {today} (reaction): "{title}" at {employer} — not interested: {reason.strip()}'


def resolve_criteria_choice(suggestion: str, choice: str) -> str | None:
    """Turn the approve/edit/skip answer into a line to append (or None).

    Enter/blank -> append the suggestion; 'n'/'no' -> append nothing; anything
    else is treated as the user's edited replacement.
    """
    c = choice.strip()
    if c.lower() in ("n", "no"):
        return None
    if c == "":
        return suggestion
    return c


def append_to_criteria(path: Path, line: str) -> None:
    """Append a dated reaction line to criteria.md's update log."""
    text = path.read_text(encoding="utf-8").rstrip()
    path.write_text(text + "\n" + line + "\n", encoding="utf-8")


def load_blocklist(path: Path) -> set[str]:
    """Load the set of blocked employer names; empty if the file is missing."""
    if not path.exists():
        return set()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return set(data.get("blocked") or [])


def add_to_blocklist(path: Path, name: str) -> None:
    """Append an employer to the blocklist file, idempotently.

    Preserves the existing `blocked:` list order and appends new names at
    the end; a no-op if the name is already present.
    """
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        names = list(data.get("blocked") or [])
    else:
        names = []
    if name in names:
        return
    names.append(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f"  - {n}" for n in names)
    path.write_text(f"blocked:\n{body}\n", encoding="utf-8")


def block_employer(conn: sqlite3.Connection, employer: str, today: str) -> int:
    """Bulk-mark an employer's other open postings as not_interested.

    Only touches rows currently 'new' or 'surfaced' (already-reviewed rows
    and other employers are untouched). Does not commit. Returns the number
    of rows updated.
    """
    cur = conn.execute(
        """UPDATE postings
           SET status = ?, reviewed_at = ?, reaction_note = ?
           WHERE employer = ? AND status IN (?, ?)""",
        (dedupe.NOT_INTERESTED, today, "org blocklisted",
         employer, dedupe.NEW, dedupe.SURFACED),
    )
    return cur.rowcount


def format_posting(row: sqlite3.Row) -> str:
    """The full view shown for one posting."""
    flags = json.loads(row["flags"]) if row["flags"] else []
    flag_text = " ".join(f"[{f}]" for f in flags)
    return (
        f"\n{'=' * 72}\n"
        f"{row['title']}\n"
        f"  {row['employer']} · {report.fmt_salary(row)} · {row['location']} · "
        f"{report.fmt_remote(row)} · posted {row['posted_date']} · {row['score']}/10 "
        f"{flag_text}\n"
        f"  {row['rationale']}\n"
        f"  {row['url']}\n"
        f"{'=' * 72}"
    )


def _prompt() -> str:
    """Ask for a verdict, looping until a recognized key is entered."""
    while True:
        raw = input("  [y]es  [n]o  [s]kip  [b]lock  [q]uit > ")
        verdict = parse_verdict(raw)
        if verdict is not None:
            return verdict
        print("  (unrecognized — type y, n, s, b, or q)")


def _prompt_reason_code() -> str:
    """Ask for a rejection reason code, looping until a recognized one is entered."""
    while True:
        raw = input(
            "  Reason: [s]eniority  [o]rg-fit  [d]omain  [r]ole-shape  [c]omp  [x]other > ")
        code = parse_reason_code(raw)
        if code is not None:
            return code
        print("  (unrecognized — type s, o, d, r, c, or x)")


def main(db_path: Path | str | None = None, config_dir: Path | str | None = None) -> None:
    db = Path(db_path) if db_path else dedupe.DB_PATH
    cfg_dir = Path(config_dir) if config_dir else CONFIG_DIR

    criteria_path = cfg_dir / "criteria.md"
    blocklist_path = cfg_dir / "blocklist.yaml"
    today = date.today().isoformat()
    conn = dedupe.connect(db)

    # Collapse locale duplicates the same way the shortlist does, so a role
    # posted once per country is not asked about once per country.
    new, new_dupes = report.collapse_duplicates(dedupe.pending_new(conn, today))
    backlog, backlog_dupes = report.collapse_duplicates(
        dedupe.pending_backlog(conn, today)
    )
    duplicates = new_dupes + backlog_dupes
    if not new and not backlog:
        print("Inbox is empty — nothing new and nothing awaiting review.")
        print(f"You have {len(dedupe.interested_list(conn))} posting(s) on your apply list.")
        conn.close()
        return

    print(f"\nInbox: {len(new)} new, {len(backlog)} awaiting review, "
          f"{len(dedupe.interested_list(conn))} on your apply list.")
    if duplicates:
        print(f"({duplicates} duplicate listing(s) collapsed — same role, several locales.)")
    print("(q quits at any time; shown postings stay 'surfaced', the rest stay new.)")

    shown = decided = 0
    try:
        for row in new + backlog:
            if row["status"] == dedupe.NEW:
                dedupe.mark_surfaced(conn, row["source"], row["req_id"], today)
            shown += 1
            print(format_posting(row))

            verdict = _prompt()
            if verdict == "quit":
                print("\nQuitting — progress saved.")
                break
            if verdict == "yes":
                dedupe.record_verdict(
                    conn, row["source"], row["req_id"], dedupe.INTERESTED, "", today)
                decided += 1
                print("  ✓ moved to your apply list.")
            elif verdict == "no":
                reason = input("  Why not? (one line) > ").strip()
                if reason:
                    suggestion = build_criteria_line(
                        row["title"], row["employer"], reason, today)
                    print("\n  Suggested criteria.md line:")
                    print(f"      {suggestion}")
                    choice = input(
                        "  [Enter] append as-is, type a replacement, or [n] skip > ")
                    line = resolve_criteria_choice(suggestion, choice)
                    if line:
                        append_to_criteria(criteria_path, line)
                        print("  ✓ appended to criteria.md.")
                    else:
                        print("  (criteria.md not changed.)")
                code = _prompt_reason_code()
                note = f"{code}: {reason}"
                dedupe.record_verdict(
                    conn, row["source"], row["req_id"], dedupe.NOT_INTERESTED, note, today)
                decided += 1
                print("  ✗ not interested — will not be surfaced again.")
            elif verdict == "block":
                dedupe.record_verdict(
                    conn, row["source"], row["req_id"], dedupe.NOT_INTERESTED,
                    "org blocklisted", today)
                add_to_blocklist(blocklist_path, row["employer"])
                blocked_count = block_employer(conn, row["employer"], today)
                decided += 1
                print(f"  ⛔ blocklisted {row['employer']} — {blocked_count} other "
                      f"posting(s) marked not interested; future scans will skip it.")
            # skip: already surfaced, stays in the backlog
            conn.commit()
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted — progress saved.")
    finally:
        conn.commit()
        conn.close()

    print(f"\nDone: {shown} shown, {decided} decided this session.")


if __name__ == "__main__":
    main()
