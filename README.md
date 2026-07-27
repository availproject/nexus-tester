# Nexus Tester

Portable FastBridge test harness and shareable QA report generator for live Avail Nexus bridge flows.

## What This Repo Contains

- `ui_fastbridge_connect_test.py`
  Runs a live browser flow against `https://fastbridge.availproject.org/`, executes a bridge with a supplied wallet, and generates a static HTML report bundle.
- `check_fastbridge_balances.py`
  Read-only helper for native balance checks across supported chains.
- `check_fastbridge_tokens.py`
  Read-only helper for ERC-20 balance checks across supported chains.
- `fastbridge-report-artifacts/`
  Example generated report bundles, including a shareable static HTML dashboard.

## Quick Start

Run from the repository root.

```bash
python3 ui_fastbridge_connect_test.py
```

To target a specific destination route:

```bash
FASTBRIDGE_DEST_SLUG=avalanche python3 ui_fastbridge_connect_test.py
```

To change the bridge amount:

```bash
FASTBRIDGE_DEST_SLUG=avalanche FASTBRIDGE_BRIDGE_AMOUNT=0.1 python3 ui_fastbridge_connect_test.py
```

To pass the private key via environment variable instead of prompt:

```bash
FASTBRIDGE_TEST_PRIVATE_KEY=your_key_here python3 ui_fastbridge_connect_test.py
```

## Helper Scripts

Native balances:

```bash
FASTBRIDGE_TEST_ADDRESS=0xYourAddress python3 check_fastbridge_balances.py
```

Token balances:

```bash
FASTBRIDGE_TEST_ADDRESS=0xYourAddress python3 check_fastbridge_tokens.py
```

## Main Environment Variables

- `FASTBRIDGE_DEST_SLUG`
  Example: `base`, `polygon`, `scroll`, `avalanche`
- `FASTBRIDGE_BRIDGE_AMOUNT`
  Example: `0.1`
- `FASTBRIDGE_TEST_PRIVATE_KEY`
  If omitted, the main test script prompts for it securely
- `FASTBRIDGE_TEST_ADDRESS`
  Used by the helper read-only scripts

## Report Output

Each run creates a new folder under:

- `fastbridge-report-artifacts/fastbridge-report-<RUN_ID>/`

Each bundle contains:

- `index.html`
- `expectations.html`
- `report.json`
- `report.md`
- `assets/`

The HTML report embeds screenshots directly, so the folder can be zipped and shared as-is.

## Scheduled CI Runs

The `.github/workflows/scheduled-fastbridge-tests.yml` workflow runs the harness automatically every Monday at `00:00 UTC` (and on manual `workflow_dispatch`). For each run:

- A matrix job executes the test against each destination chain — `arbitrum`, `ethereum`, `megaeth`, `base`, `monad` — with `FASTBRIDGE_BRIDGE_AMOUNT=0.1`.
- Each timestamped report bundle is uploaded as a workflow artifact (retained 30 days).
- After every run, the per-chain `fastbridge-report-artifacts/<slug>-latest/` snapshot is committed back to the branch so the most recent report for each destination is always browsable in the repo.

Required repository secret:

- `FASTBRIDGE_TEST_PRIVATE_KEY` — private key for the funded test wallet used by the harness.
