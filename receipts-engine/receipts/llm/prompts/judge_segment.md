You classify a public creator profile into exactly one label from: podcaster, youtuber, expert_led, agency_or_brand, publication_or_directory, fan_account, unknown.
Definitions: podcaster = main output is a podcast or video podcast. youtuber = main output is YouTube/video-first content. expert_led = a coach, consultant or educator who publishes content and sells expertise. agency_or_brand = a company, agency, management firm or product brand. 
Use ONLY the supplied text. Return JSON: {"label": str, "quote": str, "reason": str}. "quote" must be copied verbatim (at least 12 characters) from the supplied text and justify the label. If no verbatim support exists, return label "unknown" and an empty quote.
TEXT: {payload}
