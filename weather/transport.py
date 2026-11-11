"""One retry policy, byte limits, provider cooldowns and a shared deadline."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import threading
import time

import requests

from weather.fields import DataFault, State


@dataclass
class Download:
    body: bytes
    headers: dict
    status: int


class Transport:
    def __init__(
        self,
        seconds=75,
        clock=time.monotonic,
        sleep=time.sleep,
        session_factory=requests.Session,
    ):
        self.clock, self.sleep, self.session_factory = clock, sleep, session_factory
        self.end = clock() + seconds
        self.local = threading.local()
        self.lock = threading.Lock()
        self.cooldowns, self.failures, self.sessions = {}, {}, []

    def remaining(self):
        left = self.end - self.clock()
        if left <= 0:
            raise DataFault("deadline", State.MISSING)
        return left

    def close(self):
        for session in self.sessions:
            session.close()

    def request(
        self,
        provider,
        method,
        url,
        *,
        headers=None,
        maximum=2 * 1024 * 1024,
        expected=200,
    ):
        if not hasattr(self.local, "session"):
            session = self.session_factory()
            # No HTTPAdapter retries: this loop is the entire retry policy.
            session.headers.update(
                {"User-Agent": "AWC-Autospot/2", "Accept-Encoding": "identity"}
            )
            self.local.session = session
            with self.lock:
                self.sessions.append(session)
        failure = DataFault("provider_unavailable", State.MISSING)
        for attempt in range(2):
            left = self.remaining()
            with self.lock:
                if self.cooldowns.get(provider, 0) > self.clock():
                    raise DataFault("provider_cooldown", State.MISSING)
            retry_after = 0
            try:
                with self.local.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=(min(10, left), min(10, left)),
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    self.remaining()
                    status = response.status_code
                    if status != expected:
                        if status in {429, 500, 502, 503, 504}:
                            failure = DataFault(f"http_{status}", State.MISSING)
                            retry_after = self._retry_after(
                                response.headers.get("Retry-After")
                            )
                        elif status == 404:
                            raise DataFault("http_404", State.MISSING)
                        elif status == 412:
                            raise DataFault("object_changed")
                        else:
                            raise DataFault(f"http_{status}")
                    else:
                        if response.headers.get(
                            "Content-Encoding", "identity"
                        ).lower() not in {"identity", ""}:
                            raise DataFault("encoded_response")
                        declared = response.headers.get("Content-Length")
                        if (
                            method != "HEAD"
                            and declared
                            and (not declared.isdigit() or int(declared) > maximum)
                        ):
                            raise DataFault("response_size")
                        body = bytearray()
                        if method != "HEAD":
                            for chunk in response.iter_content(32 * 1024):
                                self.remaining()
                                body.extend(chunk)
                                if len(body) > maximum:
                                    raise DataFault("response_size")
                            if declared and len(body) != int(declared):
                                raise DataFault("truncated_response")
                        with self.lock:
                            self.failures[provider] = 0
                        return Download(
                            bytes(body),
                            requests.structures.CaseInsensitiveDict(response.headers),
                            status,
                        )
            except requests.RequestException:
                failure = DataFault("network_timeout_or_connection", State.MISSING)
            with self.lock:
                failures = self.failures.get(provider, 0) + 1
                self.failures[provider] = failures
                if retry_after > 2 or failures >= 2:
                    self.cooldowns[provider] = self.clock() + max(
                        10, min(retry_after, 300)
                    )
                    raise failure
            if attempt == 0:
                pause = max(retry_after, random.uniform(0.2, 0.6))
                if pause >= self.remaining():
                    raise DataFault("deadline", State.MISSING)
                self.sleep(pause)
        raise failure

    @staticmethod
    def _retry_after(value):
        if not value:
            return 0
        try:
            return max(0, int(value))
        except ValueError:
            try:
                return max(
                    0,
                    (
                        parsedate_to_datetime(value) - datetime.now(timezone.utc)
                    ).total_seconds(),
                )
            except (ValueError, TypeError, OverflowError):
                return 0
