"""What the receiving server concluded about a message's authenticity.

Two questions are asked of these headers, and they are not the same question.

**"Did this really come from inside the tenant?"** - `is_internal`. This is what
guards the task-reply path, where a message that appears to be from Alex is
allowed to drive Jira. The `From` address alone is a claim anyone can make;
DMARC is not the answer either, because intra-organisation mail never leaves
the tenant to be evaluated and comes back `dmarc=none`. The real signal is
`X-MS-Exchange-Organization-AuthAs`, which Exchange Online stamps on receipt
and strips from anything arriving from outside. Genuine self-sent mail is
`Internal`; a spoof of the same address is `Anonymous`, whatever the From says.

**"Did this sender prove they are who they claim?"** - `failure`. This guards
the junk review, where surfacing a phish with a "move to inbox" button beside
it would have the assistant vouching for it. Here the ordinary SPF/DKIM/DMARC
verdict is exactly right, and a failure is disqualifying regardless of what the
model concluded about the content.

HEADERS CAN BE ABSENT. Some intra-org and system mail arrives with none at all,
so "no headers" is a third answer, not a synonym for pass or fail. Each caller
decides which way that falls, and both currently fail closed: an unverifiable
message does not drive Jira and is not offered as a rescue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Stamped by Exchange Online on receipt. Anything inbound has its own copy
# stripped first, so these cannot be spoofed by a sender.
AUTH_AS = "x-ms-exchange-organization-authas"
DIRECTIONALITY = "x-ms-exchange-organization-messagedirectionality"
RESULTS = "authentication-results"

_VERDICT = re.compile(r"\b(spf|dkim|dmarc|compauth)\s*=\s*([a-z]+)", re.I)


def headers(message: dict) -> dict[str, str]:
    """Internet headers as a lowercase-keyed dict. Empty when none were returned."""
    return {
        (h.get("name") or "").strip().lower(): (h.get("value") or "").strip()
        for h in (message.get("internetMessageHeaders") or [])
    }


@dataclass(frozen=True)
class AuthResult:
    spf: str = ""
    dkim: str = ""
    dmarc: str = ""
    compauth: str = ""
    present: bool = False

    @property
    def summary(self) -> str:
        """A short line for a confirmation page. Plain, not reassuring."""
        if not self.present:
            return "no authentication results"
        bits = [f"{k}={v}" for k, v in (("SPF", self.spf), ("DKIM", self.dkim),
                                        ("DMARC", self.dmarc)) if v]
        return " · ".join(bits) if bits else "no authentication results"


def parse(message: dict) -> AuthResult:
    raw = headers(message).get(RESULTS, "")
    if not raw:
        return AuthResult()
    found = {k.lower(): v.lower() for k, v in _VERDICT.findall(raw)}
    return AuthResult(
        spf=found.get("spf", ""),
        dkim=found.get("dkim", ""),
        dmarc=found.get("dmarc", ""),
        compauth=found.get("compauth", ""),
        present=True,
    )


def is_internal(message: dict) -> bool:
    """Whether Exchange itself says this originated inside the tenant.

    Deliberately strict: absent headers are not internal. The one thing this
    gates is a message being allowed to write to Jira, and "I could not tell"
    must not read as yes.
    """
    found = headers(message)
    if found.get(AUTH_AS, "").strip().lower() == "internal":
        return True
    return found.get(DIRECTIONALITY, "").strip().lower() == "originating"


# Verdicts that mean the sender failed to prove who they are. `none` is not
# here: it means the domain published no policy, which is ordinary for small
# senders and is not evidence of forgery.
_BAD = {"fail", "softfail", "permerror", "temperror"}


def failure(message: dict) -> str:
    """Why this message failed authentication, or "" if it did not.

    A returned string is meant to be shown to Alex, so it names the check
    rather than describing the consequence.
    """
    result = parse(message)
    if not result.present:
        return "the sending server left no authentication results"
    if result.compauth == "fail":
        return "Microsoft's composite authentication marked it as spoofed"
    if result.dmarc in _BAD:
        return f"DMARC {result.dmarc}"
    if result.spf in _BAD:
        return f"SPF {result.spf}"
    if result.dkim in _BAD and result.dmarc != "pass":
        return f"DKIM {result.dkim}"
    return ""


def domain(address: str) -> str:
    """The sending domain, which is the part worth reading on a phish."""
    _, _, host = (address or "").partition("@")
    return host.strip().lower()
