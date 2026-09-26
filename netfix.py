"""
Prefer IPv4 when connecting to Google and Gmail.

The problem this solves
-----------------------
On some networks IPv6 is switched on but does not actually work - a router
or internet provider misconfiguration. Windows tries IPv6 addresses first,
so every connection has to wait for each IPv6 attempt to time out before it
falls back to IPv4. Google's services have many IPv6 addresses, so that wait
adds up to a minute or more per connection, and the Google Sheets library
often gives up before the fallback arrives. It shows up as runs hanging at
"Connecting to Google Sheets", or as SSL "EOF" errors.

What this does
--------------
Reorders address lookups so IPv4 addresses are tried first. IPv6 is still
tried afterwards, so a network that only has IPv6 keeps working. It changes
nothing outside this program - not the computer's network settings.

It covers the Google Sheets connection and email. Page fetching uses its own
networking, which already races IPv4 against IPv6, so it never had the
problem.

Set PREFER_IPV4=false in .env to turn it off.
"""

from __future__ import annotations

import socket

_original_getaddrinfo = socket.getaddrinfo
_installed = False


def prefer_ipv4() -> None:
    """Install the IPv4-first ordering. Safe to call more than once."""
    global _installed
    if _installed:
        return

    def ipv4_first(*args, **kwargs):
        results = _original_getaddrinfo(*args, **kwargs)
        # sorted() is stable, so the order within each family is unchanged.
        return sorted(results, key=lambda result: 0 if result[0] == socket.AF_INET else 1)

    socket.getaddrinfo = ipv4_first
    _installed = True
