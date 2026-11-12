import pytest
from sqlalchemy import select

from scripts.prepare_vip_demo import DemoData
from vip.db import Database
from vip.linking import redeem_link_token
from vip.models import Order
from vip.users import register_user


def test_demo_ignores_external_database_configuration_and_refuses_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://production.example/do-not-use")
    data = DemoData(tmp_path)
    raw = data.seed(123, -100123)
    assert data.path.is_file()
    assert raw
    with pytest.raises(ValueError, match="overwrite"):
        data.seed(123, -100123)
    db = Database(data.url)
    try:
        with db.transaction() as session:
            register_user(session, 123)
            redeem_link_token(session, raw, 123)
        data.change("renew")
        data.change("revoke")
        with db.transaction() as session:
            assert all(order.status == "cancelled" for order in session.scalars(select(Order)))
    finally:
        db.engine.dispose()


def test_demo_requires_its_marker_before_any_change(tmp_path):
    with pytest.raises(ValueError, match="marker"):
        DemoData(tmp_path).change("revoke")


async def test_isolated_fixture_runs_binding_invite_approval_expiry_and_revocation(
    tmp_path, monkeypatch
):
    from time import time

    from aiogram.methods import BanChatMember

    from tests.test_group_bot import deliver
    from tests.test_groups import CHAT, Scenario

    data = DemoData(tmp_path)
    raw = data.seed(123, CHAT)
    db = Database(data.url)
    try:
        scenario = Scenario(db)
        scenario.now = int(time())
        assert "Compra vinculada" in (await deliver(scenario, text="/vincular " + raw))[0]
        # Binding can cross a wall-clock second; eligibility must observe its committed start.
        scenario.now = int(time())
        assert "https://t.me/" in (await deliver(scenario, text="/acessar"))[0]
        url = next(iter(scenario.telegram.invites))
        await deliver(scenario, join=scenario.request(123, url))
        assert scenario.telegram.members[123] == "member"
        # Expiry is enforced on the next persisted sync due time, not instantly
        # when the offline fixture is changed.
        scenario.now += 61
        monkeypatch.setattr("scripts.prepare_vip_demo.time", lambda: scenario.now)
        data.change("expire")
        scenario.restarted().sync_once()
        assert scenario.telegram.members[123] == "left"
        assert scenario.telegram.count(BanChatMember) == 1
        # A new isolated purchase uses the same production binding and entry handlers.
        renewed = data.change("renew")
        assert "Compra vinculada" in (await deliver(scenario, text="/vincular " + renewed))[0]
        await deliver(scenario, text="/acessar")
        url = next(reversed(scenario.telegram.invites))
        await deliver(scenario, join=scenario.request(123, url))
        assert scenario.telegram.members[123] == "member"
        data.change("revoke")
        scenario.now += 61
        scenario.restarted().sync_once()
        assert scenario.telegram.members[123] == "left"
        assert scenario.telegram.count(BanChatMember) == 2
    finally:
        db.engine.dispose()
