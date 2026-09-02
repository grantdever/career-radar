# Career Radar

[![CI](https://github.com/grantdever/career-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/grantdever/career-radar/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Code style: ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An AI-powered job search and opportunity radar that queries employer Applicant Tracking Systems (ATS) directly, filters out noise deterministically, and scores surviving candidates against your personal rubric using a configurable LLM.

```
[ ATS Boards & Careers Pages ]
              │
              ▼
   [ ATS Fetchers (API/Scraper) ]   ── (Workday, Greenhouse, Lever, Ashby, etc.)
              │
              ▼
  [ Deterministic Pre-filter ]       ── (Drops excluded titles and locations before LLM)
              │
              ▼
    [ SQLite Database ]             ── (Local persistence, fuzzy dedupe & state machine)
              │
              ▼
     [ LLM Scorer ]                 ── (Scores 1-10 against your criteria.md via LiteLLM)
              │
     ┌────────┴────────┐
     ▼                 ▼
[ Shortlist Report ]  [ Terminal Review Inbox ]
(Daily Markdown)      (`career-radar review`)
```

---

## Quickstart

### Installation

For local development or installation from source (Python 3.11+):
```bash
# Clone the repository
git clone https://github.com/grantdever/career-radar.git
cd career-radar

# Create an isolated environment and install with development tools
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

*(Once published to PyPI, you can also install via `pip install career-radar`).*

### First Run

```bash
# 1. Initialize your local configuration directory (~/.config/career-radar)
career-radar init

# 2. Export your preferred LLM API key (OpenAI, Anthropic, Gemini, Groq, etc.)
export OPENAI_API_KEY="sk-..."

# 3. Run your first daily scan!
career-radar scan
```

---

## Terminal Showcase

### 1. Scanning Employer Boards (`career-radar scan`)
```text
$ career-radar scan
Scanning jobs...
2026-09-02 14:40:03 INFO career_radar.core.pipeline: greenhouse:example: pre-filter dropped 17 new postings (3 kept)
2026-09-02 14:40:03 INFO career_radar.core.pipeline: Example Corp: 20 on board, 3 new
2026-09-02 14:40:04 INFO career_radar.core.score: Scoring 3 postings in batches of 8 (0 calibration examples)
2026-09-02 14:40:06 INFO career_radar.core.report: Wrote /home/user/.local/share/career-radar/output/2026-09-02-shortlist.md (new=3, capped_out=0, awaiting=0, interested=0, borderline=0, duplicates_collapsed=0)
```

### 2. Interactive Terminal Review (`career-radar review`)
```text
Inbox: 1 new, 0 awaiting review, 0 on your apply list.
(q quits at any time; shown postings stay 'surfaced', the rest stay new.)

========================================================================
Senior Systems Engineer
  Example Corp · $165,000 - $210,000 · Remote (US) · Remote · posted 2026-09-01 · 9/10 [remote]
  Strong match for distributed systems experience and the personal rubric.
  https://example.com/jobs/5918231
========================================================================
  [y]es  [n]o  [s]kip  [b]lock  [q]uit > y
  ✓ moved to your apply list.

Done: 1 shown, 1 decided this session.
```

When you mark a role with `[n]`, you can provide a quick rejection reason (e.g. `too much sales engineering`, `requires 50% travel`). Career Radar can append these reasons directly to your `criteria.md` to refine future scores.

---

## Example Daily Shortlist

Every scan outputs a curated Markdown digest to `~/.local/share/career-radar/output/YYYY-MM-DD-shortlist.md`:

```markdown
# Shortlist — 2026-09-02

Precision: no reviews yet

## New (7+, not yet shown to you)

- **Staff Backend Engineer** — Example Corp (9/10) `[remote]`
  - $180,000 - $220,000 · Remote · posted 2026-09-01
  - Strong product alignment, modern backend stack, and autonomous role.
  - https://example.com/job/101

## Awaiting your review

None.
```

---

## Architecture & Engineering Highlights

* **Cost-Aware Two-Stage Filtering**: Career Radar applies deterministic title and location rules before sending surviving postings to an LLM API.
* **Multi-ATS Normalization Engine**: Normalizes diverse listing schemas into a uniform `Posting` data model across APIs and HTML job boards:
  - Workday, Greenhouse, Lever, Ashby, Rippling, Paylocity, JazzHR, Breezy HR, iCIMS, USAJOBS, Adzuna, WordPress job boards, ADP Joblist, and a Universal LLM web extractor.
  - *See our [ATS Configuration Guide](docs/ats-guide.md) for endpoint tokens.*
* **Exponential Backoff & Failure Isolation**: Shared HTTP fetchers retry 429/5xx responses with exponential backoff. Scanner failures are isolated so one broken endpoint does not abort the remaining scans.
* **Longitudinal SQLite State Machine**: Jobs are stored in local SQLite (`scanner.db`) with a persisted review lifecycle (`new` -> `surfaced` -> `interested` | `not_interested`). First-seen and last-seen timestamps preserve posting history across scans.
* **Fuzzy Title & Multi-Locale Deduplication**: Companies frequently publish the same role across dozens of cities. Career Radar strips locale decoration and uses `SequenceMatcher` heuristics to collapse duplicates down to a single canonical recommendation.
* **Parallel LLM Batch Scoring & Schema Fallback**: Evaluates postings in concurrent batches of 8 using [LiteLLM](https://github.com/BerriAI/litellm). If a model backend lacks JSON-mode support, Career Radar automatically falls back to raw text extraction with custom JSON array recovery.

---

## Privacy & Data Handling

Trust and privacy are core engineering priorities:

* **Local by Default**:
  - Your SQLite database, application logs, review verdicts, and search criteria reside entirely on your local machine (`~/.local/share/career-radar` and `~/.config/career-radar`).
  - There are no telemetry trackers, third-party analytics, or external database synchronizations.
* **What Leaves Your Machine**:
  - HTTP GET requests to public employer job boards.
  - For postings that pass the pre-filter, the job description excerpt and your `criteria.md` rubric are sent over HTTPS to your chosen LLM provider (OpenAI, Anthropic, Gemini, Groq, etc.).
* **Local LLM Option**:
  - To keep your rubric and extracted job text out of cloud LLM APIs, configure a local model with [Ollama](https://ollama.com/) (for example, `llm_model: "ollama/llama3"`). Career Radar still makes network requests to the public ATS boards you configure.

---

## Configuration & Customization

Career Radar separates the execution engine from your personal configuration. When you run `career-radar init`, it creates `~/.config/career-radar/`:

1. `employers.yaml`: Employers to monitor and their ATS endpoints.
2. `filters.yaml`: Pre-filtering rules (allowed locations, negative titles, and LLM model selection).
3. `criteria.md`: Plain markdown rubric that teaches the LLM how to evaluate your fit.

### Using Custom LLMs

To switch models, set `llm_model` in `~/.config/career-radar/filters.yaml`:

```yaml
# Examples:
# Claude 3.5 Sonnet
llm_model: "claude-3-5-sonnet-20241022"

# Local Ollama Llama 3
# llm_model: "ollama/llama3"

# Google Gemini
# llm_model: "gemini/gemini-1.5-flash"
```

---

## Repository Structure

```text
career-radar/
├── src/career_radar/
│   ├── cli.py              # Click command-line interface
│   ├── config.py           # YAML and environment loaders
│   ├── core/
│   │   ├── dedupe.py       # SQLite persistence, review lifecycle & fuzzy deduping
│   │   ├── normalize.py    # Multi-ATS schema normalization & data models
│   │   ├── pipeline.py     # Scanner orchestration & failure isolation
│   │   ├── prefilter.py    # Deterministic FilterRules pre-filtering engine
│   │   ├── report.py       # Markdown shortlist generation & title de-duplication
│   │   └── score.py        # LiteLLM batch scoring & JSON array extraction
│   ├── fetchers/           # ATS connectors (Greenhouse, Workday, Lever, USAJOBS, etc.)
│   └── ui/
│       └── review.py       # Interactive terminal inbox UI
├── tests/                  # 30 unit & integration tests
├── docs/
│   └── ats-guide.md        # ATS endpoint discovery documentation
├── .github/workflows/ci.yml # GitHub Actions multi-version test workflow
├── .env.example            # Example API key configuration
└── pyproject.toml          # Package metadata and tool configurations
```

---

## Development & Testing

```bash
# Run the automated test suite
pytest -v

# Run with an isolated empty configuration
CAREER_RADAR_CONFIG_DIR=$(mktemp -d) pytest -v

# Run static analysis and linting
ruff check src/ tests/
```

---

## License

This project is licensed under the [MIT License](LICENSE).
