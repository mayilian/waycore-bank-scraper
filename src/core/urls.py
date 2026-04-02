"""URL normalization for stable connection identity.

Banks often have equivalent URLs that differ only cosmetically:
  https://demo-bank-2.vercel.app   vs  https://demo-bank-2.vercel.app/
  https://WWW.Bank.com/login       vs  https://www.bank.com/login

normalize_url() canonicalizes these so connection matching is stable.
"""

from urllib.parse import urlparse, urlunparse


def normalize_url(url: str) -> str:
    """Normalize a bank URL for stable identity matching.

    - Lowercases scheme and host
    - Strips trailing slash from path
    - Strips www. prefix
    - Removes default ports (80 for http, 443 for https)
    - Drops fragment
    - Preserves path and query (banks may use path-based login routing)
    """
    parsed = urlparse(url.strip())

    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()

    # Strip www. prefix
    if host.startswith("www."):
        host = host[4:]

    # Strip default ports
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    netloc = f"{host}:{port}" if port else host

    # Normalize path — strip trailing slash, keep everything else
    path = parsed.path.rstrip("/") or ""

    # Drop fragment, keep query
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))
