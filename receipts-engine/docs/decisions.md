# Decisions

Each entry: Decision / Alternatives considered / Why / Evidence that would change my mind. Pre-filled by the build; edit freely.

## 1. Deterministic filters before the LLM
- **Decision:** every check is a rule over saved snapshots first. The LLM (`judge_*`) is consulted only for ambiguous cases, and its claim counts only if its quote is found verbatim in the snapshot (`accept_llm_claim`).
- **Alternatives:** LLM-first classification of each profile; no LLM at all.
- **Why:** rules are reproducible, cheap and auditable; the audit can re-run them. Unverified LLM claims are stored with `reason_code=unverified_llm_claim` but never pass.
- **Would change my mind:** an audit showing rules miss many qualified creators that a verified-quote LLM pass would catch at acceptable cost.

## 2. Corroborated country rule
- **Decision:** accept a country only when a declared value (YouTube `snippet.country` or schema.org `addressCountry`) agrees with at least one independent signal (ccTLD, place name, postal code, second schema value).
- **Alternatives:** trust the declared value alone; infer from text alone.
- **Why:** declared values are self-reported and sometimes stale; a second agreeing signal makes a country claim checkable.
- **Would change my mind:** too many podcasters rejected as `country_unverified` (podcast feeds have no declared country) while spot checks show they are clearly in approved countries.

## 3. Eight-minute long-form threshold
- **Decision:** long-form means at least 480 s (`thresholds.longform_min_seconds`); Shorts are up to 60 s; the `no_shortform` signal treats anything up to 180 s as a possible Short.
- **Alternatives:** 10 or 20 minutes; duration percentiles per creator.
- **Why:** 8 minutes is where YouTube's mid-roll ads begin and a common split between "short" and "full" content. Shorts can now run 3 minutes, so the need signal is conservative to avoid false "no clips" claims.
- **Would change my mind:** audit failures on `no_shortform` from 2-3 minute clips that are not Shorts.

## 4. Quality over 5,000 volume, with a scale ladder
- **Decision:** target 1,000-1,200 audited leads first; scale toward 5,000 only while two consecutive audits stay ≥ 95%.
- **Alternatives:** chase 5,000 immediately.
- **Why:** the brief rewards verified accuracy; a large list with 85% accuracy costs more in wasted outreach than it gains.
- **Would change my mind:** audits that stay at 98%+ as volume grows.

## 5. Canonical-key dedupe beyond the workbook's ID check
- **Decision:** `creator_key` comes from the strongest anchor (YouTube id > Podcast Index id > website domain); any shared identity (channel, feed, domain, handle, verified social) merges into the earlier key. Fuzzy name matches only go to a review file.
- **Alternatives:** dedupe by URL or name only.
- **Why:** the same creator often appears as a channel, a podcast and a site. Name-only matching would wrongly merge different people.
- **Would change my mind:** the review file showing many true duplicates that share no identity.

## 6. Assisted send for LinkedIn, Instagram and X
- **Decision:** those channels are never automated (G7); they are queued for a person, need a manual check to become a best channel, and are marked `assisted_send`.
- **Alternatives:** browser automation.
- **Why:** platform terms prohibit automation, and account bans are costly.
- **Would change my mind:** an official API that permits this kind of messaging.

## 7. Sender wording that does not claim IncuBrix employment
- **Decision:** the default role says "independent growth researcher (assessment sample, not an IncuBrix employee)"; a validator blocks "I work at IncuBrix" and similar phrases.
- **Why:** honesty, and the sender is a candidate, not staff.
- **Would change my mind:** IncuBrix approving outreach in its name with its own sending domain.

## 8. Responsiveness as availability evidence, not a reply rate
- **Decision:** comment-reply rate and guest or sponsor forms add up to 3 points to a channel score, labelled as availability signals.
- **Why:** no reply data exists before outreach; claiming reply rates would be invented.
- **Would change my mind:** seed-test or real reply data allowing calibration.

## Build-time decisions (made while implementing the spec)
- **Identity resolution runs right after collection (before s3)**, not after s4, so duplicates merge into the earlier key without being evaluated twice. Merge semantics are unchanged.
- **Podcast transcript gap counts as `captions_gap`.** The spec's `has_captions` column stores `podcast:transcript` presence for feed episodes. Without this, podcast-only creators can almost never show a strong need signal.
- **Hook item fallback.** The hook uses the latest long-form item; creators who only publish shorter non-Short pieces (often coaches) fall back to their latest non-Short item. With no titled recent item, the reason is `no_hook_item`.
- **Message word limits exclude the mandatory footer** (sender block and opt-out line), which other validators require separately. LinkedIn's 300 characters counts the whole note because it is a platform limit.
- **Messages drafted while `settings.yaml` still says TODO** are stored `validated=0` instead of rejecting the lead; `export` re-renders all messages with the filled config and requires every one to validate (G4 blocks export until then).
- **Export skips, but does not reject, leads whose messages fail validation**; only stale evidence, incomplete rows and duplicates are recorded as rejections.
- **Fixture replays are never live.** Recorded real responses replay as `data_origin=fixture`, hand-built ones as `synthetic`; the keyless demo writes `leads_DEMO_not_live.csv`, never `leads.csv` (G1).
- **Lead ids are never reused:** a rejected creator's lead row is kept with status `Rejected`; the id counter only increases.
- **Quota balance is a hard rule:** export picks the largest score-ordered set that meets every country and segment rule and keeps the rest as `Reserve`.
- **AU postal pattern adjusted** from `\d{4}.*STATE` (which matched any year followed later by a state word) to `STATE 1234`.
- **`registrable_domain` falls back to the full host** for unknown suffixes; otherwise every `.test` or intranet host collapsed into one identity.

## One decision I would change
1. **Country corroboration for podcasts.** Requiring a declared country rejects podcasters whose sites carry no schema.org address. With more time I would accept an explicit "based in <city>" statement on the creator's own About page as the declared value, corroborated by a second, independent signal.
2. **Language detection model size.** High-accuracy lingua with all languages costs about 850 MB of memory. Restricting it to the languages seen in discovery would cut that sharply without hurting the English check.
