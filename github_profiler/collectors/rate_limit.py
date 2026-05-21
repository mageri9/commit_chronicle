"""Rate Limit Manager - Adaptive rate limiting for GitHub API"""

import logging
import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock
from typing import Any

from github import Github, RateLimitExceededException

logger = logging.getLogger(__name__)


class RateLimitManager:
    """Manages GitHub API rate limits with adaptive backoff"""

    def __init__(self, client: Github, min_remaining: int = 100) -> None:
        self.client = client
        self.min_remaining = min_remaining
        self.lock = Lock()
        self.last_check: float = 0.0

    def check_and_wait(self) -> None:
        """Check rate limit and wait if needed"""
        with self.lock:
            # Don't check more than once per second
            if time.time() - self.last_check > 1.0:
                return

            rate_limit = self.client.get_rate_limit()
            remaining = rate_limit.core.remaining

            if remaining < self.min_remaining:
                reset_time = rate_limit.core.reset
                wait_seconds = (reset_time - datetime.now()).total_seconds() + 1
                logger.warning(
                    f"Rate limit low ({remaining} remaining). Waiting {wait_seconds:.0f}s"
                )
                time.sleep(wait_seconds)

            self.last_check = time.time()

    def execute_with_retry(
        self, func: Callable[..., Any], max_retries: int = 3, initial_delay: float = 1.0
    ) -> Any:
        """Execute a function with exponential backoff on rate limit"""
        delay = initial_delay

        for attempt in range(max_retries):
            try:
                self.check_and_wait()
                return func()
            except RateLimitExceededException:
                if attempt == max_retries - 1:
                    raise

                wait: float = delay * (2**attempt)
                logger.warning(
                    f"Rate limit exceeded. Retrying in {wait:.0f}s (attempt {attempt + 1})"
                )
                time.sleep(wait)

        return None
