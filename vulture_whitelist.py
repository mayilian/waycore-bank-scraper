# Vulture whitelist — suppress false positives for interface parameters
# and framework-invoked methods that appear unused statically.

# Base class parameters used by subclasses or callers
dashboard_url  # noqa: used by callers (steps.py passes post_login_url)
