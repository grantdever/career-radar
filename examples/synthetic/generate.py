#!/usr/bin/env python3
"""Generate the synthetic labelled set used by the eval demo and tests.

200 fictional postings for the synthetic backend-engineer persona in
examples/decision.backend-engineer.yaml, with labels drawn from a fixed
archetype -> apply-probability table (seeded, so the output is stable).
Employers and postings are invented; any resemblance is accidental.

    python examples/synthetic/generate.py        # rewrites pool.jsonl + labels.jsonl
"""

from __future__ import annotations

import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent
SEED = 20261006
N = 200

GOOD_ORGS = ["Gridlane (developer tools)", "Stackwell (open-source database)",
             "Ferrous Cloud (infrastructure platform)", "Ledgerless (B2B SaaS invoicing)",
             "Tracepoint (observability)"]
NEUTRAL_ORGS = ["Harbor Health (hospital network)", "Northfield Insurance", "Brightcart (retail)",
                "Civic Transit Authority", "Atlas Logistics"]
BAD_ORGS = ["CoinVault (crypto exchange)", "LuckyLine (sports betting)"]

# archetype: (titles, domain phrases, work phrases, years, p(apply), org pool)
ARCHETYPES = {
    "backend-core": (["Backend Engineer", "Senior Backend Engineer", "Platform Engineer",
                      "Distributed Systems Engineer", "API Engineer"],
                     "You will build backend services and APIs in Go and Python, design "
                     "distributed systems, and own databases and internal platform services.",
                     "Most of your week is spent designing and writing code and shipping services "
                     "end to end.", (4, 7), 0.85, "good"),
    "backend-neutral-org": (["Backend Engineer", "Senior Software Engineer, Backend"],
                            "You will build backend services and APIs that power internal "
                            "systems, with databases and distributed jobs.",
                            "You will design and write code for services and own them end to end.",
                            (3, 7), 0.6, "neutral"),
    "adjacent": (["Data Engineer", "Site Reliability Engineer", "ML Infrastructure Engineer",
                  "Full-stack Engineer"],
                 "You will build data pipelines, reliability tooling, and infrastructure; some "
                 "full-stack work across the platform.",
                 "A mix of building tools and operating production systems.", (3, 7), 0.35, "any"),
    "frontend": (["Frontend Engineer", "Senior Frontend Engineer (React)", "iOS Engineer",
                  "Product Designer"],
                 "You will build user interfaces in React and TypeScript and mobile screens, "
                 "working closely with design.",
                 "Mostly design and code for the user interface.", (3, 6), 0.05, "any"),
    "staff": (["Staff Backend Engineer", "Principal Engineer, Platform"],
              "You will set technical direction for backend services, APIs and distributed "
              "systems across the platform organization.",
              "A mix of design reviews, mentoring and writing code for critical services.",
              (9, 12), 0.45, "good"),
    "management": (["Engineering Manager, Backend", "Director of Engineering", "VP of Engineering"],
                   "You will lead backend and platform teams building services and APIs.",
                   "Managing people, hiring, planning and coordination; little hands-on code.",
                   (8, 15), 0.05, "any"),
    "non-eng": (["Account Executive", "Technical Recruiter", "Marketing Manager",
                 "Solutions Consultant"],
                "You will sell and market software products to customers and partners.",
                "Meetings, demos, quotas and campaigns; no software building.", (2, 6), 0.02,
                "any"),
    "support": (["Support Engineer", "Technical Support Specialist", "NOC Operator"],
                "You will triage customer tickets about backend services and APIs.",
                "Mostly support tickets, operations runbooks and escalations.", (1, 4), 0.08,
                "any"),
    "vetoed-backend": (["Backend Engineer", "Senior Backend Engineer"],
                       "You will build backend services and APIs in Go for our exchange and "
                       "betting platform; distributed systems at scale.",
                       "Design and write code for services and own them end to end.",
                       (4, 7), 0.05, "bad"),
    "clearance": (["Backend Engineer (Cleared)", "Software Engineer, Backend Services"],
                  "You will build backend services and APIs for government programs. An active "
                  "security clearance is required; on-site work in Huntsville.",
                  "Design and write code for services.", (4, 8), 0.03, "neutral"),
    "junior": (["Software Engineering Intern, Backend", "New Grad Backend Engineer"],
               "You will build backend services and APIs with mentorship.",
               "Writing code with guidance from senior engineers.", (0, 1), 0.02, "any"),
}
WEIGHTS = {"backend-core": 34, "backend-neutral-org": 16, "adjacent": 30, "frontend": 22,
           "staff": 12, "management": 14, "non-eng": 22, "support": 16, "vetoed-backend": 10,
           "clearance": 10, "junior": 14}
LOCATIONS = [("Remote (US)", "remote"), ("New York, NY", "hybrid"), ("Remote (US)", "remote"),
             ("Austin, TX", "onsite"), ("New York, NY", "onsite")]


def main() -> None:
    rng = random.Random(SEED)
    names = [k for k, w in WEIGHTS.items() for _ in range(w)]
    rng.shuffle(names)
    pool, labels = [], []
    for i, arch in enumerate(names[:N], 1):
        titles, domain, work, (ylo, yhi), p_apply, org_kind = ARCHETYPES[arch]
        org = rng.choice({"good": GOOD_ORGS, "neutral": NEUTRAL_ORGS, "bad": BAD_ORGS,
                          "any": GOOD_ORGS + NEUTRAL_ORGS}[org_kind])
        employer, _, blurb = org.partition(" (")
        location, remote = rng.choice(LOCATIONS)
        years = rng.randint(ylo, yhi)
        lo = rng.choice([90, 120, 140, 160, 180, 200]) * 1000
        apply_p = p_apply
        if location == "Austin, TX" and remote == "onsite":
            apply_p *= 0.2  # persona only works remote or in New York
        desc = (f"About {employer}: {blurb.rstrip(')') or 'a company'}. {domain} {work} "
                f"Requirements: {years}+ years of professional experience. "
                f"Location: {location} ({remote}).")
        rid = f"syn-{i:03d}"
        pool.append({"id": rid, "title": rng.choice(titles), "employer": employer,
                     "location": location, "remote_type": remote, "salary_min": lo,
                     "salary_max": lo + 40000, "description": desc, "stratum": "synthetic",
                     "archetype": arch})
        verdict = "interested" if rng.random() < apply_p else "not_interested"
        reason = None
        if verdict == "not_interested":
            reason = {"frontend": "role-shape", "non-eng": "role-shape", "support": "role-shape",
                      "management": "seniority", "staff": "seniority", "junior": "seniority",
                      "vetoed-backend": "org", "clearance": "location"}.get(arch, "domain")
            if location == "Austin, TX" and remote == "onsite":
                reason = "location"
        labels.append({"id": rid, "verdict": verdict, "reason": reason, "note": "synthetic"})
    (HERE / "pool.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in pool))
    (HERE / "labels.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in labels))
    pos = sum(lab["verdict"] == "interested" for lab in labels)
    print(f"wrote {len(pool)} rows ({pos} positive) to {HERE}")


if __name__ == "__main__":
    main()
