import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from tests.test_catalog import product_order, setup_catalog
from tests.test_domain import NOW
from tests.test_omega_pix import buyer, charge_response, configured, transaction_response
from tests.test_product_access import bind
from vip.entitlements import product_accesses
from vip.errors import DomainError
from vip.linking import issue_link_token
from vip.models import AccessGrant, Payment, RecoveryTask
from vip.payments.omega import OmegaGateway
from vip.payments.operations import OmegaPayments


@pytest.mark.parametrize(
    "code,units,count",
    [
        ("sample_video", "10.00", 1),
        ("vip_permanent", "20.00", 1),
        ("recovery_bundle", "25.00", 2),
    ],
)
@pytest.mark.parametrize(
    "negative,event",
    [
        ("REFUNDED", "TRANSACTION_REFUNDED"),
        ("CHARGED_BACK", "TRANSACTION_CHARGED_BACK"),
    ],
)
def test_real_omega_pipeline_grants_and_revokes_product_snapshots(
    db, code, units, count, negative, event
):
    setup_catalog(db)
    with db.transaction() as session:
        session.add(
            RecoveryTask(
                telegram_id=123,
                first_started_at=NOW,
                due_at=NOW + 1200,
                next_check_at=NOW + 1200,
                eligible_at=NOW + 1200,
                state="eligible",
            )
        )
    oid = product_order(db, code)
    now = NOW + 1202
    status, amount, posts = "PENDING", units, []

    def handle(request):
        if request.method == "POST":
            body = json.loads(request.content, parse_float=Decimal)
            posts.append(body)
            assert body["amount"] == Decimal(units)
            return httpx.Response(200, json=charge_response(transactionId="tx-" + oid))
        body = transaction_response(
            id="tx-" + oid,
            clientIdentifier=oid,
            amount=0,
            chargeAmount=0,
            status=status,
            payedAt=datetime.fromtimestamp(now, UTC).isoformat() if status == "COMPLETED" else None,
        )
        # Official numeric JSON fields; exact decimal literals, no monetary float arithmetic.
        content = json.dumps(body).replace('"amount": 0', '"amount": ' + amount)
        content = content.replace('"chargeAmount": 0', '"chargeAmount": ' + amount)
        return httpx.Response(200, content=content, headers={"Content-Type": "application/json"})

    service = OmegaPayments(
        db, OmegaGateway(configured(), transport=httpx.MockTransport(handle)), clock=lambda: now
    )
    service.create_charge(oid, buyer())
    service.create_charge(oid, buyer())
    assert len(posts) == 1
    webhook = {
        "event": "TRANSACTION_PAID",
        "token": "synthetic-webhook-token",
        "transaction": {"id": "tx-" + oid},
    }
    status, amount = "COMPLETED", "0.01"
    with pytest.raises(DomainError):
        service.process_webhook(json.dumps(webhook).encode())
    with db.transaction() as session:
        assert session.scalar(select(Payment)) is None
        assert all(g.granted_at is None for g in session.scalars(select(AccessGrant)))
    amount = units
    service.process_webhook(json.dumps(webhook).encode())
    service.process_webhook(json.dumps(webhook).encode())
    with db.transaction() as session:
        raw = issue_link_token(session, oid, ttl_seconds=900, now=now)
    bind(db, raw)
    with db.transaction() as session:
        assert len(product_accesses(session, 123)) == count
        assert session.get(RecoveryTask, 123).state == "canceled"
        assert len(list(session.scalars(select(Payment)))) == 1
    status, webhook["event"] = negative, event
    service.process_webhook(json.dumps(webhook).encode())
    service.process_webhook(json.dumps(webhook).encode())
    with db.transaction() as session:
        assert product_accesses(session, 123) == []
        assert all(g.revoked_at is not None for g in session.scalars(select(AccessGrant)))
