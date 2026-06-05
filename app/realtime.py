"""
In-process pub/sub broadcaster for Server-Sent Events.

One Broadcaster instance (module-level singleton) holds a dict of
  event_id → set[asyncio.Queue]
Each connected SSE client gets its own Queue.  When a mutation happens
(check-in, checkout, badge issued, …) the endpoint calls
`await broadcaster.publish(event_id, payload)` and every queue for
that event gets the serialised SSE message.

This works for a single-process deployment (Docker + uvicorn).  If you
ever scale to multiple workers you would swap the Queue approach for a
Redis pub/sub backend — the interface stays the same.
"""

import asyncio
import json
from collections import defaultdict
from typing import Dict, Set


class Broadcaster:
    def __init__(self) -> None:
        # keyed by event_id (int)
        self._queues: Dict[int, Set[asyncio.Queue]] = defaultdict(set)

    async def subscribe(self, event_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._queues[event_id].add(q)
        return q

    def unsubscribe(self, event_id: int, q: asyncio.Queue) -> None:
        self._queues[event_id].discard(q)

    async def publish(self, event_id: int, payload: dict) -> None:
        msg = f"data: {json.dumps(payload)}\n\n"
        stale: list = []
        for q in list(self._queues.get(event_id, [])):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                stale.append(q)
        for q in stale:
            self._queues[event_id].discard(q)


# Singleton used by routers
broadcaster = Broadcaster()
