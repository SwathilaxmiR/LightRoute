"""MX lookup for email channels (no mail is ever sent or probed). Replays/records like http.py."""
from __future__ import annotations

import json
import os
from pathlib import Path


def _fixture_path(fixtures_dir: str | Path, domain: str) -> Path:
    return Path(fixtures_dir) / "dns" / f"{domain.lower()}.json"


async def mx_hosts(domain: str, *, fixture_mode: bool, fixtures_dir: str | Path, timeout: float = 5.0) -> list[str] | None:
    """MX hostnames; [] if the domain has none; None if the lookup itself failed."""
    domain = domain.lower().strip(".")
    if fixture_mode:
        p = _fixture_path(fixtures_dir, domain)
        if not p.exists():
            return []
        return json.loads(p.read_text())["mx"]
    import dns.asyncresolver
    import dns.exception
    import dns.resolver

    try:
        ans = await dns.asyncresolver.resolve(domain, "MX", lifetime=timeout)
        hosts = sorted(str(r.exchange).rstrip(".") for r in ans if str(r.exchange) not in (".", ""))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        hosts = []
    except dns.exception.DNSException:
        return None
    if os.environ.get("RECORD_FIXTURES") == "1":
        p = _fixture_path(fixtures_dir, domain)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"origin": "recorded", "domain": domain, "mx": hosts}))
    return hosts
