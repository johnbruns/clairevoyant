"""Who the assistant says it is.

One place, because the name now appears in five different emails and a
persona that drifts - "Claire" in one, "your assistant" in another - reads as
two different systems rather than one.

IMPORTANT: this persona is for mail sent TO Alex only. It must never appear in
a drafted reply, because those go out under Alex's name to other people. A
meeting reply that opened "this is Claire Voyant" would be Claire writing to
his board, which is not what a drafted reply is. `triage.py` builds those and
deliberately does not import this module.
"""

from __future__ import annotations

import html
import os

NAME = os.environ.get("ASSISTANT_PERSONA_NAME", "Claire Voyant")
TITLE = os.environ.get(
    "ASSISTANT_PERSONA_TITLE", "your personal Senior Administrative Strategist"
)


def greeting() -> str:
    """The line that opens every email the assistant sends Alex."""
    return f"Hi, this is {NAME}, {TITLE}!"


def greeting_html(css_class: str = "persona") -> str:
    return f"<p class='{css_class}'>{html.escape(greeting(), quote=False)}</p>"


def signoff_html(css_class: str = "signoff") -> str:
    return f"<div class='{css_class}'>&mdash; {html.escape(NAME, quote=False)}</div>"


STYLE = """
.persona{font-size:24px;line-height:1.3;font-weight:700;color:#0f172a;
margin:0 0 14px;font-style:normal}
.signoff{font-size:13px;color:#94a3b8;margin:22px 0 0;font-style:italic}
"""
