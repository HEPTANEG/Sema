"""Recoverable Telegram HTTP transports with polling liveness tracking."""

import asyncio
import time


class ResilientHTTPXRequest:
    """Factory wrapper so telegram imports stay optional until Remote starts."""

    @staticmethod
    def create(on_success=None, on_failure=None, retries=3, **kwargs):
        from telegram.error import NetworkError, TimedOut
        from telegram.request import HTTPXRequest

        class _ResilientRequest(HTTPXRequest):
            async def do_request(self, *args, **request_kwargs):
                last_error = None
                for attempt in range(retries):
                    try:
                        response = await super().do_request(*args, **request_kwargs)
                        if on_success:
                            on_success()
                        return response
                    except (TimedOut, NetworkError) as error:
                        last_error = error
                        if on_failure:
                            on_failure(error)
                        if attempt + 1 >= retries:
                            raise
                        await self.shutdown()
                        await self.initialize()
                        await asyncio.sleep(2 ** attempt)
                raise last_error

        return _ResilientRequest(**kwargs)


class PollingLiveness:
    def __init__(self):
        self.started_at = time.monotonic()
        self.last_success_at = None

    def mark_success(self):
        self.last_success_at = time.monotonic()

    def seconds_since_success(self):
        reference = self.last_success_at if self.last_success_at is not None else self.started_at
        return time.monotonic() - reference
