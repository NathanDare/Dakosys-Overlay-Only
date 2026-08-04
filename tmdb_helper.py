# tmdb_helper.py
"""Helper utilities for interacting with TMDB.

Provides a thin wrapper around the TMDB TV API with built‑in retry handling
and a `should_refresh` helper that decides whether a show needs a fresh request
based on its status and the last‑checked timestamp.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("tmdb_helper")

# ---------------------------------------------------------------------------
# HTTP session with retry logic respecting the Retry‑After header
# ---------------------------------------------------------------------------
def _session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session

_session_obj = _session()

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_show(tmdb_id: str, api_key: str) -> Optional[Dict[str, Any]]:
    """Fetch TV show details from TMDB.

    Returns the JSON payload on success, ``None`` on a 404 (missing TMDB entry)
    or on any non‑recoverable error.  Rate‑limit (429) responses are handled by
    respecting the ``Retry‑After`` header and retrying automatically.
    """
    url = f"https://api.themoviedb.org/3/tv/{tmdb_id}"
    params = {"api_key": api_key}
    try:
        resp = _session_obj.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            logger.warning("TMDB returned 404 for TMDB ID %s – keeping existing entry.", tmdb_id)
            return None
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 1))
            logger.warning("TMDB rate limit hit – sleeping %s seconds", retry_after)
            time.sleep(retry_after)
            return get_show(tmdb_id, api_key)
        logger.error("TMDB request failed %s: %s", resp.status_code, resp.text)
    except Exception as e:
        logger.exception("Exception during TMDB request for ID %s: %s", tmdb_id, e)
    return None

# ---------------------------------------------------------------------------
# Refresh helper
# ---------------------------------------------------------------------------
ACTIVE_STATUSES = {"Returning Series", "In Production", "Planned", "Pilot"}

def should_refresh(entry: Dict[str, Any], status: str, refresh_ended_days: int = 7) -> bool:
    """Determine whether a TMDB request should be made for a show.

    * Active statuses (RETURNING, IN PRODUCTION, PLANNED, PILOT) are always
      refreshed.
    * Ended or cancelled shows are refreshed only if ``last_checked`` is older
      than ``refresh_ended_days``.
    """
    if status in ACTIVE_STATUSES:
        return True
    last = entry.get("last_checked")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last.rstrip("Z"))
    except Exception:
        # If parsing fails, be safe and refresh.
        return True
    return datetime.utcnow() - last_dt > timedelta(days=refresh_ended_days)
