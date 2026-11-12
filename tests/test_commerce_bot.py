from tests.test_catalog import product_order, setup_catalog
from tests.test_group_bot import deliver
from tests.test_groups import Scenario
from tests.test_product_access import bind, settle
from vip.models import BuyerAssurance, RecoveryTask


async def test_private_start_records_first_interaction_and_opt_out_survives_repeated_start(db):
    s = Scenario(db)
    await deliver(s, text="/start")
    with db.transaction() as session:
        task = session.get(RecoveryTask, 123)
        assert task.first_started_at == s.now and task.due_at == s.now + 1200
    s.now += 600
    await deliver(s, text="/start")
    await deliver(s, text="/parar")
    with db.transaction() as session:
        task = session.get(RecoveryTask, 123)
        assert task.first_started_at == s.now - 600 and task.opted_out_at == s.now
        assert task.state == "canceled"
    # Group commands cannot register promotional schedules.
    await deliver(s, text="/start", user_id=456, chat_id=-100123)
    with db.transaction() as session:
        assert session.get(RecoveryTask, 456) is None


async def test_bot_does_not_request_or_record_age_verification(db):
    s = Scenario(db)
    welcome = (await deliver(s, text="/start"))[0]
    assert "maioridade" not in welcome.lower() and "adultos" not in welcome.lower()
    response = await deliver(s, text="/maioridade confirmar")
    assert "não exige verificação de maioridade" in response[0]
    with db.transaction() as session:
        assert session.get(BuyerAssurance, 123) is None


async def test_permanent_vip_display_has_no_artificial_expiration_and_video_is_not_sent(db):
    setup_catalog(db)
    bind(db, settle(db, product_order(db, "vip_permanent")))
    s = Scenario(db)
    reply = (await deliver(s, text="/meuvip"))[0]
    assert "permanente" in reply.lower() and "Sem vencimento" in reply
    assert "Dias restantes: 0" not in reply
    reply = (await deliver(s, text="/meusprodutos"))[0]
    assert "vip_permanent_access" not in reply
    assert "VIP Permanente" in reply
