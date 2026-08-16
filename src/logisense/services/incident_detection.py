"""Generic, dataset-agnostic incident detection.

Runs entirely against the canonical schema. It does not know or care which
source dataset populated the shipments table. The only rule is a real,
data-derived condition: a shipment whose actual delivery date is later than
its expected delivery date has incurred a delivery deviation. Severity is
NOT decided here -- that is Phase 1's job, based on context rather than a
single hardcoded threshold.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from logisense import models as m


@dataclass(frozen=True)
class DetectionResult:
    shipments_scanned: int
    incidents_created: int


class IncidentDetectionService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def detect(self, limit: int = 1000) -> DetectionResult:
        candidates = self.session.scalars(
            select(m.Shipment)
            .where(
                m.Shipment.expected_delivery_date.is_not(None),
                m.Shipment.actual_delivery_date.is_not(None),
            )
            .limit(limit)
        ).all()

        created = 0
        for shipment in candidates:
            delay_days = (shipment.actual_delivery_date - shipment.expected_delivery_date).days
            if shipment.delay_days != delay_days:
                shipment.delay_days = delay_days

            if delay_days <= 0:
                continue

            existing = self.session.scalar(
                select(m.Incident).where(m.Incident.shipment_id == shipment.id)
            )
            if existing is not None:
                continue

            self.session.add(
                m.Incident(
                    shipment_id=shipment.id,
                    incident_type="delivery_delay",
                    deviation_value=delay_days,
                    rationale=(
                        f"actual_delivery_date is {delay_days} day(s) after "
                        f"expected_delivery_date."
                    ),
                )
            )
            created += 1

        self.session.commit()
        return DetectionResult(shipments_scanned=len(candidates), incidents_created=created)
