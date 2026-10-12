"""Kafka topology and client construction.

Topics
    ais.positions  key = MMSI, N partitions. One vessel always lands on one partition, so a single
                   consumer sees each vessel's reports in the order its collector produced them.
    ais.static     key = MMSI, same partition count, compacted: only the latest identity matters.
    ais.dlq        dead letters with the original payload and the reason.

Delivery is at-least-once end to end (idempotent producer, acks=all, offsets committed only after
the database transaction). The sink's (mmsi, ts) primary key turns that into effectively-once rows.
"""

from __future__ import annotations

import json
from typing import Any

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from .config import Settings


async def ensure_topics(s: Settings) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=s.kafka)
    await admin.start()
    try:
        retention = {"retention.ms": str(s.retention_h * 3_600_000)}
        topics = [
            NewTopic(s.topic_positions, s.partitions, 1, topic_configs=retention),
            NewTopic(s.topic_static, s.partitions, 1, topic_configs={"cleanup.policy": "compact"}),
            NewTopic(s.topic_dlq, 1, 1, topic_configs={"retention.ms": str(14 * 86_400_000)}),
        ]
        for topic in topics:  # one at a time: a batch fails as a whole if any topic already exists
            try:
                await admin.create_topics([topic])
            except TopicAlreadyExistsError:
                pass
    finally:
        await admin.close()


def producer(s: Settings) -> AIOKafkaProducer:
    return AIOKafkaProducer(
        bootstrap_servers=s.kafka,
        acks="all",
        enable_idempotence=True,
        linger_ms=10,
        key_serializer=lambda k: str(k).encode(),
        value_serializer=lambda v: json.dumps(v, separators=(",", ":")).encode(),
    )


def dead_letter(stage: str, reason: str, source: str, payload: str, received_at: float, cid: str) -> dict[str, Any]:
    return {
        "stage": stage,
        "reason": reason,
        "source": source,
        "payload": payload,
        "received_at": received_at,
        "cid": cid,
    }
