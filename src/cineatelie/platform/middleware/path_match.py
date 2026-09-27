"""Shared path-exemption matching for the tenant/closure/entitlement middleware layers,
whose exemption lists (backend spec §3, items 5-7) are written as a mix of exact paths
(`/tenant/reopen`) and prefixes (`/auth/*`) — one small helper instead of three copies of
the same trailing-`*` check.
"""

from __future__ import annotations


def path_is_exempt(path: str, patterns: frozenset[str]) -> bool:
    for pattern in patterns:
        if pattern.endswith("*"):
            if path.startswith(pattern[:-1]):
                return True
        elif path == pattern:
            return True
    return False
