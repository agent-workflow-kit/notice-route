"""Links (#12): ``pr_url``, ``ticket_url`` and a rule's rendered link.

Pure logic: no clock, no files, no processes. docs/interface.md (Links) is normative.

Safety properties:

- A URL is assembled only from a link-table entry (``links.hosts``, ``links.tickets``), the fixed
  words of the forge forms and components that matched a closed ASCII grammar. Nothing is
  escaped because nothing that needs escaping can pass the grammar.
- Only the payload's ``pr``, ``site`` and ``key`` are read, and only when they are exactly
  ``str``. A payload field named ``link`` or ``url``, a label, ``repo`` and a notice's own
  ``link`` never reach a link. A channel notice has no derived links.
- A rule's literal link is checked again here (it was checked at load) so a hand-built rule
  cannot smuggle anything through.
- Every input is length-bounded before any pattern runs, and the patterns have no nested
  quantifiers, so the work is linear. Building never raises and never refuses: anything outside
  the grammar or the tables gives the empty string (no link; the notice is still sent).

Choices where the interface is silent (the narrower reading): ``pr`` is at most 1024 characters;
a built URL is at most 1024 characters (else empty); a port must also be 1 to 65535.

TODO(#22): the send pipeline calls :func:`build_links` and passes ``pr_url`` and ``ticket_url``
to :func:`~notice_route.render.render`, and stores ``link`` with the notification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .payload import Notification
from .validate import Links, Rule, is_literal_url

__all__ = ["BuiltLinks", "build_links", "build_pr_url", "build_ticket_url"]

URL_MAX = 1024

_LABEL = re.compile(r"[a-z0-9-]+")
_PORT = re.compile(r"[0-9]{1,5}")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]+")
_NUMBER = re.compile(r"[1-9][0-9]{0,9}")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]{1,9}")
_FORGES = {"github": "/pull/", "gitlab": "/-/merge_requests/"}


@dataclass(frozen=True, slots=True)
class BuiltLinks:
    """The derived fields and the rule's link; each is ``""`` when there is none."""

    pr_url: str = ""
    ticket_url: str = ""
    link: str = ""


def _host_ok(hostport: str) -> bool:
    host, sep, port = hostport.partition(":")
    if sep and (not _PORT.fullmatch(port) or not 1 <= int(port) <= 65535):
        return False
    if not host:
        return False
    for label in host.split("."):
        if not _LABEL.fullmatch(label) or label[0] == "-" or label[-1] == "-":
            return False
    return True


def build_pr_url(pr: object, links: Links) -> str:
    """The pull or merge request URL for a payload's ``pr`` (``<host>[:<port>]/<path>#<n>``),
    when its host is a key of ``links.hosts``; else ``""``."""
    if type(pr) is not str or not links.hosts or len(pr) > URL_MAX:
        return ""
    head, sep, number = pr.partition("#")
    if not sep or not _NUMBER.fullmatch(number):
        return ""
    hostport, sep, path = head.partition("/")
    forge = links.hosts.get(hostport)
    if not sep or forge not in _FORGES or not _host_ok(hostport):
        return ""
    for segment in path.split("/"):
        if not _SEGMENT.fullmatch(segment) or segment in (".", ".."):
            return ""
    url = f"https://{hostport}/{path}{_FORGES[forge]}{number}"
    return url if len(url) <= URL_MAX else ""


def build_ticket_url(site: object, key: object, links: Links) -> str:
    """``links.tickets[site]`` followed by ``key``, when ``site`` is a table key and ``key``
    matches the ticket grammar; else ``""``."""
    if type(site) is not str or type(key) is not str or len(key) > URL_MAX:
        return ""
    base = links.tickets.get(site)
    if base is None or not _KEY.fullmatch(key):
        return ""
    url = base + key
    return url if len(url) <= URL_MAX else ""


def _rule_link(link: str | None, pr_url: str, ticket_url: str) -> str:
    if link == "{{pr_url}}":
        return pr_url
    if link == "{{ticket_url}}":
        return ticket_url
    return link if link is not None and is_literal_url(link) else ""


def build_links(rule: Rule, n: Notification, links: Links) -> BuiltLinks:
    """The derived links of ``n`` and the link ``rule`` gives it. A channel notice has no
    derived links, and its own ``link`` is never used."""
    pr_url = ticket_url = ""
    if n.shape == "hook":
        pr_url = build_pr_url(n.payload.get("pr"), links)
        ticket_url = build_ticket_url(n.payload.get("site"), n.payload.get("key"), links)
    return BuiltLinks(pr_url, ticket_url, _rule_link(rule.link, pr_url, ticket_url))
