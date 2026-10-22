# Launch kit (template: copy to launch_kit.md and complete with real run numbers)

Numbered headings matter: `receipts export --workbook` copies the first paragraph under each `## n)` heading into 03_ENGINE_LAUNCH B24:B31.

## 1) Who we target first
TODO: segment, country and need signal, with the count from `receipts stats` (e.g. "N UK podcasters with no transcripts and a sponsor page").

## 2) Channels and why
TODO: channel mix from `receipts stats` and the responsiveness evidence. State that these are availability signals, not reply rates.

## 3) Week 1 actions
TODO: owner, tool and daily volume per channel. Keep volumes conservative for new accounts and verify current platform limits.

## 4) Weeks 2 to 4
TODO: step-up rules (bounce rate under 2%, near-zero spam complaints) and the weekly `make refresh` ritual.

## 5) Tools and monthly cost
TODO: a table, including the measured engine run cost from docs/run_report.md.

## 6) Targets after 30 days
TODO: sends, replies, positive replies and meetings as low/expected/high ranges with the arithmetic shown, labelled as planning assumptions to calibrate in week 1.

## 7) Risks and mitigations
TODO: deliverability, platform limits, wrong contact, opt-out handling, stale data.

## 8) What IncuBrix must provide
TODO: sending domain and mailboxes, accounts, CRM or sheet, approved offer line, calendar link, a named reply owner, budget.

## Appendix: run commands
`make setup`, `make run`, `receipts export --workbook`, `make refresh`, `receipts audit --n 20 --seed 7 --channels 20`.
