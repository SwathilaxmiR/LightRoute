# Compliance and platform stance

## What the engine collects
- **Public business information only:** channel and feed metadata from official APIs (YouTube Data API v3, Podcast Index, iTunes lookup), the creator's own RSS feed, and pages on the creator's own website.
- **No logins, no scraping behind authentication, no bypass** of robots.txt, rate limits, paywalls or CAPTCHAs. Website fetches honour robots.txt for the engine's user agent (G6), are rate-limited per host, and identify the engine and a contact address in the User-Agent.
- LinkedIn, Instagram and X profiles are **never fetched**. They are recorded only when the creator links them from their own site or channel, and are verified by a person (`receipts manual-check`).
- **Contact details are never guessed** (G3). An email or handle is stored only with the URL where it was found and the verbatim text around it. Patterns such as `info@domain` are never constructed. MX records are looked up; no mail is sent to verify an address.

## Outreach rules
- Every message carries the **sender's name, role and postal address** (LinkedIn: name and role) and an **opt-out line**. The sender does not claim to work for IncuBrix.
- **Opt-outs are honoured immediately:** a STOP or negative reply suppresses the lead and cancels remaining steps. `receipts forget <creator_key>` deletes a creator's data and snapshots that only they use, and adds a suppression record.
- LinkedIn, Instagram and X are **assisted send only** (G7): a person sends from an account they own, within platform limits.
- The seed test sends only to `seed_test.allowed_recipients` (G2). No code path can email or message a real creator.

## Laws considered
- **CAN-SPAM (US):** accurate header and sender information, a non-deceptive subject, a physical postal address, a working opt-out honoured within 10 business days (we honour it immediately).
- **GDPR / UK GDPR (GB, IE):** legitimate interest for B2B contact using business contact details the creator published for that purpose; data minimisation (only business-relevant public data); right to erasure (`forget`); records of the source of each contact (`source_url`, `source_quote`).
- **CASL (CA):** contact only addresses conspicuously published for business enquiries without a "no unsolicited messages" statement; identification and an unsubscribe mechanism in every message.
- **Spam Act 2003 (AU), Unsolicited Electronic Messages Act 2007 (NZ), Spam Control Act (SG):** inferred consent from conspicuously published business addresses, sender identification, functional unsubscribe.
- **DPDP Act 2023 (India),** where the operator processes data from India: purpose limitation, data minimisation, erasure on request.

This is an engineering summary, not legal advice. Have IncuBrix's counsel confirm before sending at volume.

## Data handling
- `.env` (keys and passwords) is git-ignored; a test (G8) fails if any tracked file contains a key pattern or a `.env` value.
- Snapshots are kept under `out/` (git-ignored) as the audit trail for every claim.
- The tracker stores no IP addresses; it keeps a short hash of the user agent per click.
