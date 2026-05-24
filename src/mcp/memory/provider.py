"""
Memory Provider Protocol and MemoryEvent dataclass.

Defines the interface that all memory backends must implement.
This allows hot-swapping between VectorMemory and StaticMemory
without changing any calling code.
"""

from dataclasses import dataclass, field
from typing import Protocol, Optional
from datetime import datetime


VALENCE_VALUES = frozenset({"golden", "smell", "neutral"})

VALID_SIGNAL_TYPES = frozenset({
    "task_request", "design_question", "voice_note", "status_check",
    "system_observation", "meta_reflection", "philosophy", "casual"
})


@dataclass
class MemoryEvent:
    """A single event stored in memory.

    Events represent messages, tasks, decisions, notes, or links
    that Lobster should remember. Each event is stored with its
    embedding for vector search and indexed for keyword search.

    The ``valence`` field classifies an observation as a golden pattern
    (something that works well and should be reinforced), a smell (something
    problematic that should be addressed), or neutral (no strong signal).
    Valid values: 'golden' | 'smell' | 'neutral' (default).

    The ``subject`` field is a short noun-phrase label for what this event is about.

    The ``signal_type_hint`` field is the caller's pre-classification of the event's
    signal type. When provided, the slow-reclassifier will use this value and skip
    content inference. Valid values: 'task_request' | 'design_question' | 'voice_note' |
    'status_check' | 'system_observation' | 'meta_reflection' | 'philosophy' | 'casual'.
    """
    id: Optional[int]
    timestamp: datetime
    type: str        # 'message', 'task', 'decision', 'note', 'link'
    source: str      # 'telegram', 'github', 'internal'
    project: Optional[str]
    content: str
    metadata: dict = field(default_factory=dict)
    consolidated: bool = False
    valence: str = "neutral"   # 'golden' | 'smell' | 'neutral'
    subject: Optional[str] = None
    signal_type_hint: Optional[str] = None

    def to_dict(self) -> dict:
        """Serialize to a dictionary."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "type": self.type,
            "source": self.source,
            "project": self.project,
            "content": self.content,
            "metadata": self.metadata,
            "consolidated": self.consolidated,
            "valence": self.valence,
            "subject": self.subject,
            "signal_type_hint": self.signal_type_hint,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEvent":
        """Deserialize from a dictionary."""
        ts = data.get("timestamp")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        elif ts is None:
            ts = datetime.now()
        return cls(
            id=data.get("id"),
            timestamp=ts,
            type=data.get("type", "note"),
            source=data.get("source", "internal"),
            project=data.get("project"),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
            consolidated=data.get("consolidated", False),
            valence=data.get("valence", "neutral"),
            subject=data.get("subject"),
            signal_type_hint=data.get("signal_type_hint"),
        )


class MemoryProvider(Protocol):
    """Protocol for memory backends.

    Any memory backend (vector DB, static files, etc.) must implement
    these methods. This allows the vector system to be removed or
    swapped without incident.
    """

    def store(self, event: MemoryEvent) -> int:
        """Store an event and return its ID."""
        ...

    def search(
        self,
        query: str,
        limit: int = 10,
        project: str = None,
        valence: str = None,
    ) -> list[MemoryEvent]:
        """Search memory for events matching the query.

        Uses hybrid search (vector + keyword) when available,
        falls back to keyword-only search otherwise.

        Pass ``valence='golden'`` or ``valence='smell'`` to restrict results
        to observations with that classification.
        """
        ...

    def recent(self, hours: int = 24, project: str = None) -> list[MemoryEvent]:
        """Get recent events from the last N hours."""
        ...

    def unconsolidated(self) -> list[MemoryEvent]:
        """Get all events that haven't been consolidated yet."""
        ...

    def mark_consolidated(self, event_ids: list[int]) -> None:
        """Mark events as consolidated (processed by nightly job)."""
        ...

    def close(self) -> None:
        """Clean up resources (close DB connections, etc.)."""
        ...
