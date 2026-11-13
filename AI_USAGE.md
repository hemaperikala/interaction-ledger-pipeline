# AI Usage Disclosure

## Tools used
Claude (Anthropic) in the claude.ai chat.

## What I used them for
- Claude read the repo (schemas, glossary, generator) and wrote the first full draft of the solution: the parsers, SQLite schema, ingestion, report code, the test suite, and the first draft of `SUBMISSION.md`.
- Claude ran the tests and the large-scale benchmark in its own sandbox, not on my machine.

## What I wrote or decided myself
- Chose Assignment A (data) and cloned the repo, set up the virtual environment and generated the synthetic data with the provided script (seed 42, small scale).
- Ran the full flow on my own machine: ingest drop_1, ingest drop_2, report, and the test suite (82 tests passing).
- Re-ran ingest on both drops and confirmed every file was skipped as already ingested.
- Checked the generated report files against the output contract in the assignment README.
- Made sure the database, the PII key file, the generated data and the virtual environment are git-ignored and not committed.
- Filled in my details and timings in `SUBMISSION.md` and published the project to my own GitHub repository.

## Where the AI was wrong or unhelpful
- One of the AI's own tests asserted the wrong number of non-empty event lines (6 instead of 7, a miscount in the test, not the pipeline). The test failed on the first run and was corrected after counting the lines by hand.
- The AI's first suggested plan called for a separate `orphan_events` table; the final design derives orphans at report time instead, because that is what makes late-arriving leads work without a back-fill step.
- The large-scale timings in `SUBMISSION.md` come from the AI's Linux sandbox, not from my laptop, so they may differ on other hardware.

## How I verified AI-generated code
- 82 automated tests, including a differential test against an independent in-memory reference implementation, on two generated seeds.
- Idempotency checks: drop_1 → drop_2, 1 → 2 → 1, and all-at-once give byte-identical reports.
- Scanned the database file for plaintext phone numbers and names.
- Ran the pipeline and the tests myself on Windows (PowerShell) and confirmed the output and the re-ingest behaviour.