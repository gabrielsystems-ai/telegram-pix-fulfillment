import asyncio
import logging
import math
from datetime import UTC, datetime
from html import escape
from threading import Event
from time import time

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatMemberUpdated,
    Message,
)
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from vip.bot_diagnostics import (
    PollingDiagnostics,
    UpdateDiagnostics,
    dispatcher_started,
    inspect_telegram,
    install_receive_log_filter,
    processing_error,
)
from vip.bot_payment_ui import BotPaymentUI
from vip.bot_purchases import BotPurchases
from vip.bot_timing import Timings, UpdateTimings, telegram_timings
from vip.bot_ui import (
    START_TEXT,
    MenuView,
    configure_public_commands,
    context_keyboard,
    main_keyboard,
)
from vip.catalog import OFFERS
from vip.config import Settings
from vip.db import Database
from vip.entitlements import product_accesses
from vip.errors import DomainError
from vip.fulfillment_worker import run_fulfillment_worker
from vip.group_worker import run_group_worker
from vip.groups import GroupError, VIPAccess
from vip.linking import redeem_link_token
from vip.models import Order, Plan, Subscription, User
from vip.recovery import RecoveryService, opt_in, opt_out, record_start
from vip.recovery_worker import run_recovery_worker
from vip.subscriptions import memberships, vip_entitlements
from vip.telegram_gateway import AiogramGroupGateway, AiogramRecoverySender
from vip.users import register_user
from vip.video_delivery import (
    TelegramVideoSender,
    configured_video,
    deliver_video,
    register_video,
    update_video_offer,
)


def build_router(
    settings: Settings, db: Database, *, access: VIPAccess | None = None, payments=None
) -> Router:
    router = Router()
    private = Router()
    private.message.filter(F.chat.type == "private", F.from_user.is_bot == False)  # noqa: E712
    router.include_router(private)
    view = MenuView(settings.start_banner_path, database=db, video_path=settings.start_video_path)
    timings = Timings(settings.bot_timing_enabled)
    purchases = BotPurchases(db, settings, payments=payments)

    async def show(bot, user_id, text, keyboard, source=None, *, fresh=False, media="brand"):
        try:
            await view.show(bot, user_id, text, keyboard, source=source, fresh=fresh, media=media)
        except TelegramAPIError:
            logging.getLogger(__name__).warning("Public menu delivery unavailable")

    def vip_available(user_id):
        with db.transaction() as session:
            if settings.vip_chat_id is not None:
                return bool(
                    vip_entitlements(
                        session,
                        user_id,
                        settings.vip_chat_id,
                        now=int(access.clock()) if access else int(time()),
                    )
                )
            return any(
                a.permission == "vip_permanent_access" for a in product_accesses(session, user_id)
            )

    async def public_screen(bot, user_id, page, source=None):
        async def present(*args, **kwargs):
            await show(*args, media="text" if page in {"vip", "access"} else "brand", **kwargs)

        try:
            if page == "home":
                await present(bot, user_id, START_TEXT, main_keyboard(), source)
            elif page == "link":
                await present(
                    bot,
                    user_id,
                    "<b>Recuperar acesso</b>\n\nEnvie /vincular SEU_TOKEN com o código individual "
                    "recebido após a compra.\n\nNão compartilhe esse código.",
                    context_keyboard(),
                    source,
                )
            elif page == "support":
                contact = settings.support_contact.strip()
                if not contact or contact == "Suporte ainda não configurado.":
                    contact = (
                        "O suporte está temporariamente indisponível. Tente novamente mais tarde."
                    )
                await present(
                    bot, user_id, "<b>Suporte</b>\n\n" + escape(contact), context_keyboard(), source
                )
            elif page in {"previous", "sample"}:

                def owned_video():
                    with db.transaction() as session:
                        return any(
                            a.permission == "sample_video_access"
                            for a in product_accesses(session, user_id)
                        )

                owned = await asyncio.to_thread(owned_video)
                buy_ready = not owned and await asyncio.to_thread(purchases.can_buy, "sample_video")
                ready = owned and await asyncio.to_thread(configured_video, db, bot.id)
                name, amount, _ = OFFERS["sample_video"]
                text = (
                    f"<b>{escape(name)}</b>\nR$ {amount // 100},{amount % 100:02d}\n\n"
                    "Assista ao vídeo da Exemplo. Acesso individual.\n\n"
                )
                text += (
                    "Compra confirmada. Seu vídeo está disponível abaixo."
                    if ready
                    else "Compra confirmada. O vídeo está temporariamente indisponível. "
                    "Fale com o suporte."
                    if owned
                    else "Compre agora e receba automaticamente após a confirmação do pagamento."
                    if buy_ready
                    else "As compras estão temporariamente indisponíveis. Fale com o suporte."
                )
                await present(
                    bot,
                    user_id,
                    text,
                    context_keyboard(
                        support=True,
                        video=ready,
                        buy="sample_video" if buy_ready else None,
                    ),
                    source,
                )
            elif page == "video":
                sender = TelegramVideoSender(settings.telegram_bot_token)
                status = await asyncio.to_thread(deliver_video, db, bot.id, user_id, sender)
                texts = {
                    "denied": "Seu acesso ao vídeo ainda não está ativo.",
                    "unavailable": "O Vídeo Exemplo está temporariamente indisponível. "
                    "Fale com o suporte.",
                    "sent": "Vídeo Exemplo enviado nesta conversa.",
                    "already_sent": "Seu Vídeo Exemplo já foi entregue nesta conversa.",
                    "uncertain": "Sua entrega está em processamento ou não pôde ser confirmada. "
                    "Confira esta conversa e fale com o suporte antes de tentar novamente.",
                }
                await present(
                    bot,
                    user_id,
                    texts[status],
                    context_keyboard(support=True),
                    source,
                )
            elif page == "vip":
                owned = await asyncio.to_thread(vip_available, user_id)
                buy_ready = not owned and await asyncio.to_thread(
                    purchases.can_buy, "vip_permanent"
                )
                name, amount, _ = OFFERS["vip_permanent"]
                text = (
                    f"<b>{escape(name)}</b>\n\nAcesso permanente ao grupo VIP por "
                    f"R$ {amount // 100},{amount % 100:02d}. Pagamento único."
                )
                text += (
                    "\n\nSeu acesso VIP está ativo."
                    if owned
                    else ""
                    if buy_ready
                    else "\n\nAs compras estão temporariamente indisponíveis. Fale com o suporte."
                )
                await present(
                    bot,
                    user_id,
                    text,
                    context_keyboard(
                        access=owned,
                        support=not owned,
                        buy="vip_permanent" if buy_ready else None,
                    ),
                    source,
                )
            elif page == "access":
                owned = await asyncio.to_thread(vip_available, user_id)
                if not owned:
                    await present(
                        bot,
                        user_id,
                        "Seu acesso VIP ainda não está ativo.\n\n"
                        "Conheça o VIP Permanente para saber mais.",
                        context_keyboard(discover_vip=True, support=True),
                        source,
                    )
                    return
                if access is None:
                    await present(
                        bot,
                        user_id,
                        "Não foi possível solicitar o convite agora. Fale com o suporte.",
                        context_keyboard(support=True),
                        source,
                    )
                    return
                try:
                    ticket = await asyncio.to_thread(access.issue_invite, user_id)
                except GroupError as error:
                    unavailable = error.code == "vip_not_eligible"
                    await present(
                        bot,
                        user_id,
                        "Seu acesso VIP ainda não está ativo."
                        if unavailable
                        else "Não foi possível emitir o convite agora. "
                        "Tente novamente ou fale com o suporte.",
                        context_keyboard(discover_vip=unavailable, support=True),
                        source,
                    )
                    return
                if ticket.status == "already_member":
                    text = "Você já participa do grupo VIP."
                elif ticket.url:
                    expiry = datetime.fromtimestamp(ticket.expires_at, UTC).strftime("%H:%M:%S UTC")
                    text = (
                        f"Solicite entrada pelo seu convite pessoal, válido até {expiry}:\n"
                        f"{escape(ticket.url)}\n"
                        "Use este convite com a mesma conta."
                    )
                else:
                    text = "O convite venceu antes da entrega. Solicite um novo convite."
                await present(bot, user_id, text, context_keyboard(access=True), source)
        except SQLAlchemyError:
            await present(
                bot,
                user_id,
                "Não foi possível consultar seu acesso agora. Tente novamente mais tarde.",
                context_keyboard(support=True),
                source,
            )

    def contact_start(telegram_id: int):
        with db.transaction() as session:
            record_start(
                session,
                telegram_id,
                delay_seconds=settings.recovery_delay_seconds,
                now=int(access.clock()) if access else int(time()),
            )

    def register_contact(telegram_id: int):
        with db.transaction() as session:
            register_user(session, telegram_id)

    def link(raw: str, telegram_id: int):
        with db.transaction() as session:
            register_user(session, telegram_id)
            redeem_link_token(session, raw, telegram_id)

    async def handle_link(message: Message, raw: str):
        try:
            # The user ID comes only from the Telegram update, never command arguments.
            await asyncio.to_thread(link, raw, message.from_user.id)
        except DomainError:
            await message.answer("Token inválido, expirado, já utilizado ou compra indisponível.")
            return
        await message.answer(
            "Compra vinculada. Use /sample para acessar o vídeo ou /vip para consultar seu VIP."
        )

    @private.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        logging.getLogger(__name__).warning("start_handler_entered")
        if command.args and command.args.startswith("link_"):
            await asyncio.to_thread(contact_start, message.from_user.id)
            await handle_link(message, command.args[5:])
            return
        with timings.measure("start.registration"):
            await asyncio.to_thread(register_contact, message.from_user.id)
        logging.getLogger(__name__).warning("start_user_registration_finished")
        with timings.measure("start.menu"):
            await show(message.bot, message.from_user.id, START_TEXT, main_keyboard(), fresh=True)
        logging.getLogger(__name__).warning("start_menu_attempt_finished")
        # Persist in this handler after delivery. No fire-and-forget recovery/financial work.
        try:
            with timings.measure("start.after_reply"):
                await asyncio.to_thread(contact_start, message.from_user.id)
        except SQLAlchemyError:
            logging.getLogger(__name__).warning("Start contact persistence unavailable")

    @private.message(Command("sample", "previous", "vip"))
    async def product_page(message: Message, command: CommandObject):
        await public_screen(message.bot, message.from_user.id, command.command)

    @router.callback_query(
        F.data.in_(
            {
                "menu:home",
                "menu:sample",
                "menu:previous",
                "menu:video",
                "menu:vip",
                "menu:access",
                "menu:support",
                "menu:link",
            }
        )
    )
    async def menu_callback(query: CallbackQuery):
        if (
            query.from_user.is_bot
            or not isinstance(query.message, Message)
            or query.message.chat.type != "private"
            or query.message.chat.id != query.from_user.id
        ):
            await query.answer()
            return
        await query.answer()
        await public_screen(
            query.bot, query.from_user.id, query.data.split(":", 1)[1], query.message
        )

    def vip_text(telegram_id: int) -> str:
        with db.transaction() as session:
            now = int(access.clock()) if access else int(time())
            if settings.vip_chat_id is not None:
                rights = vip_entitlements(session, telegram_id, settings.vip_chat_id, now=now)
                entries = [(right.plan_name, right.expires_at) for right in rights]
            else:
                entries = []
                for subscription in memberships(session, telegram_id, now=now):
                    order = session.get(Order, subscription.order_id)
                    plan = session.get(Plan, order.plan_id)
                    entries.append((plan.name, subscription.expires_at))
            if not entries:
                if settings.vip_chat_id is not None:
                    previous = session.execute(
                        select(Subscription, Order.status, Plan.name)
                        .join(Order, Subscription.order_id == Order.id)
                        .join(Plan, Plan.id == Order.plan_id)
                        .where(
                            Subscription.telegram_id == telegram_id,
                            Subscription.telegram_group_id == settings.vip_chat_id,
                        )
                        .order_by(Subscription.expires_at.desc())
                        .limit(1)
                    ).first()
                    if previous:
                        subscription, order_status, plan_name = previous
                        state = "revogado" if order_status != "paid" else "vencido"
                        date = datetime.fromtimestamp(subscription.expires_at, UTC).strftime(
                            "%d/%m/%Y %H:%M UTC"
                        )
                        return (
                            f"Status: {state}\nPlano: {plan_name}\nData de vencimento: {date}\n"
                            "Dias restantes: 0\nNão há direito de entrada vigente."
                        )
                return "Nenhuma assinatura VIP vigente vinculada a este Telegram."
            permanent = next((name for name, expiry in entries if expiry is None), None)
            if permanent:
                return (
                    f"Status: permanente\nPlano: {permanent}\nSem vencimento automático.\n"
                    "Válido enquanto o grupo estiver em operação, sujeito às condições de acesso.\n"
                    + (
                        "Use Acessar VIP para solicitar entrada."
                        if access
                        else "Automação de grupo desativada."
                    )
                )
            name, expiry = max(entries, key=lambda item: item[1])
            days = math.ceil((expiry - now) / 86400)
            status = "próximo do vencimento" if days <= 3 else "ativo"
            date = datetime.fromtimestamp(expiry, UTC).strftime("%d/%m/%Y %H:%M UTC")
            lines = [
                f"Status: {status}",
                f"Plano: {name}",
                f"Data de vencimento: {date}",
                f"Dias restantes: {days}",
            ]
            if access:
                lines.append("Use Acessar VIP para solicitar entrada no grupo.")
            else:
                lines.append("Automação de grupo desativada.")
            return "\n".join(lines)

    @private.message(Command("meuvip", "vencimento"))
    @private.message(F.text.in_({"Meu VIP", "Consultar vencimento"}))
    async def my_vip(message: Message):
        await message.answer(await asyncio.to_thread(vip_text, message.from_user.id))

    @private.message(Command("vincular"))
    async def bind(message: Message, command: CommandObject):
        if not command.args:
            await message.answer(
                "Use /vincular TOKEN com o token individual recebido após a compra."
            )
            return
        await handle_link(message, command.args.strip())

    @private.message(F.text == "Vincular acesso comprado")
    async def bind_help(message: Message):
        await message.answer("Use /vincular TOKEN no privado. O token é individual e de uso único.")

    @private.message(Command("suporte"))
    @private.message(F.text == "Suporte")
    async def support(message: Message):
        await public_screen(message.bot, message.from_user.id, "support")

    @private.message(Command("maioridade"))
    async def obsolete_age_command(message: Message):
        await message.answer(
            "O catálogo Example Store não exige verificação de maioridade. "
            "Consulte /meusprodutos ou /meuvip para acessar suas compras."
        )

    @private.message(Command("parar", "promocoes_aceitar"))
    async def communication(message: Message, command: CommandObject):
        def record():
            with db.transaction() as session:
                operation = opt_out if command.command == "parar" else opt_in
                operation(
                    session,
                    message.from_user.id,
                    now=int(access.clock()) if access else int(time()),
                )

        await asyncio.to_thread(record)
        await message.answer(
            "Preferência registrada. /parar interrompe definitivamente "
            "esta comunicação promocional única."
        )

    @private.message(Command("meusprodutos"))
    async def my_products(message: Message):
        def text():
            with db.transaction() as session:
                permissions = {
                    a.permission for a in product_accesses(session, message.from_user.id)
                }
                lines = []
                if "vip_permanent_access" in permissions:
                    lines.append("VIP Permanente — consulte /meuvip ou /acessar.")
                if "sample_video_access" in permissions:
                    lines.append("Vídeo Exemplo — acesso adquirido; solicite em /sample.")
                return (
                    "\n".join(lines)
                    or "Nenhum produto digital autorizado vinculado a este Telegram."
                )

        await message.answer(await asyncio.to_thread(text))

    @private.message(Command("cadastrarvideo"))
    async def register_sample(message: Message):
        if message.from_user.id not in settings.admin_ids:
            await message.answer("Acesso restrito.")
            return
        video = message.reply_to_message.video if message.reply_to_message else None
        if video is None:
            await message.answer("Responda ao vídeo definitivo com /cadastrarvideo.")
            return
        try:
            await asyncio.to_thread(register_video, db, message.bot.id, video)
        except DomainError:
            await message.answer("O vídeo não corresponde aos metadados do arquivo definitivo.")
            return
        await message.answer("Vídeo Exemplo cadastrado. Identificador armazenado com segurança.")

    @private.message(Command("atualizarvideo"))
    async def update_sample_offer(message: Message):
        if message.from_user.id not in settings.admin_ids:
            await message.answer("Acesso restrito.")
            return

        def update():
            with db.transaction() as session:
                update_video_offer(session)

        try:
            await asyncio.to_thread(update)
        except DomainError as error:
            if error.code == "video_product_code_conflict":
                await message.answer(
                    "Há um registro desse vídeo com outro identificador. "
                    "Nenhum produto foi duplicado; revise o catálogo antes de continuar."
                )
            else:
                await message.answer("Não foi possível sincronizar o produto agora.")
            return
        await message.answer(
            "Vídeo Exemplo atualizado para R$ 10,00. Compras anteriores preservadas."
        )

    def admin_text() -> str:
        with db.transaction() as session:
            users = session.scalar(select(func.count()).select_from(User))
            orders = session.scalar(select(func.count()).select_from(Order))
            omega = "configurada" if settings.omega_pix_enabled else "desativada"
            group = "habilitado" if access else "desativado"
            return f"Usuários: {users}\nPedidos: {orders}\nOmega: {omega}\nGrupo VIP: {group}"

    @private.message(Command("admin"))
    async def admin(message: Message):
        if message.from_user.id not in settings.admin_ids:
            await message.answer("Acesso restrito.")
            return
        await message.answer(await asyncio.to_thread(admin_text))

    @private.message(Command("acessar"))
    @private.message(F.text == "Acessar VIP")
    async def access_vip(message: Message):
        await public_screen(message.bot, message.from_user.id, "access")

    @private.message(Command("meuid"))
    async def my_id(message: Message):
        await message.answer(f"Seu Telegram ID: {message.from_user.id}")

    @router.message(Command("idgrupo"), F.chat.type.in_({"group", "supergroup"}))
    async def group_id(message: Message):
        if message.from_user and message.from_user.id in settings.admin_ids:
            await message.answer(f"ID deste grupo: {message.chat.id}")

    @private.message(Command("grupostatus"))
    async def group_status(message: Message):
        if message.from_user.id not in settings.admin_ids:
            await message.answer("Acesso restrito.")
            return
        if access is None:
            await message.answer("Automação de grupo desativada.")
            return
        result = await asyncio.to_thread(access.operational_status)
        try:
            await asyncio.to_thread(access.check_permissions)
            permission = "verificadas"
        except GroupError as error:
            permission = error.code
        await message.answer(
            f"Permissões: {permission}\nMembros acompanhados: {result['tracked']}\n"
            f"Pendências: {result['pending']}\nFalhas: {result['failures']}"
        )

    @router.chat_join_request()
    async def join_request(request: ChatJoinRequest):
        if access and request.chat.id == settings.vip_chat_id:
            try:
                await asyncio.to_thread(access.join_request, request)
            except (GroupError, SQLAlchemyError):
                logging.getLogger(__name__).warning("VIP join processing unavailable")

    @router.chat_member()
    async def member_updated(update: ChatMemberUpdated):
        if (
            access
            and update.chat.id == settings.vip_chat_id
            and not update.new_chat_member.user.is_bot
        ):
            try:
                await asyncio.to_thread(
                    access.observe_member, update.new_chat_member.user.id, update.chat.id
                )
            except (GroupError, SQLAlchemyError):
                logging.getLogger(__name__).warning("VIP membership observation unavailable")

    BotPaymentUI(purchases, show, public_screen).register(router, private)
    return router


async def run_bot():
    logger = logging.getLogger(__name__)
    logger.warning("bot_runtime_initializing")
    settings = Settings()
    if not settings.telegram_bot_token or not settings.telegram_bot_token.get_secret_value():
        raise SystemExit("Configure TELEGRAM_BOT_TOKEN before running the bot.")
    if settings.vip_group_enabled and settings.vip_chat_id is None:
        raise SystemExit("Configure VIP_CHAT_ID before enabling VIP group automation.")
    db = Database(settings.database_url)
    # No update/body logging: /start and /vincular may carry bearer tokens.
    logging.basicConfig(level=logging.WARNING)
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(UpdateDiagnostics())
    dispatcher.errors.register(processing_error)
    dispatcher.startup.register(dispatcher_started)
    install_receive_log_filter()
    access = (
        VIPAccess(settings, db, AiogramGroupGateway(settings.telegram_bot_token))
        if settings.vip_group_enabled
        else None
    )
    dispatcher.include_router(build_router(settings, db, access=access))
    logger.warning("bot_router_registered")
    stop, wake = Event(), asyncio.Event()
    worker = None
    recovery_worker = None
    fulfillment_worker = None
    try:
        async with Bot(token=settings.telegram_bot_token.get_secret_value()) as bot:
            bot.session.middleware(PollingDiagnostics())
            if settings.bot_timing_enabled:
                bot.session.middleware(telegram_timings)
                dispatcher.update.outer_middleware(UpdateTimings())
            await inspect_telegram(bot)
            logger.warning("public_command_registration_started")
            try:
                await configure_public_commands(bot)
            except TelegramAPIError:
                logging.getLogger(__name__).warning("Public command menu registration unavailable")
            logger.warning("public_command_registration_finished")
            fulfillment_worker = asyncio.create_task(
                run_fulfillment_worker(db, settings, bot.id, access, stop, wake)
            )
            if access:
                worker = asyncio.create_task(run_group_worker(access, stop, wake))
            if settings.recovery_enabled:
                recovery = RecoveryService(
                    settings,
                    db,
                    AiogramRecoverySender(AiogramGroupGateway(settings.telegram_bot_token)),
                )
                recovery_worker = asyncio.create_task(run_recovery_worker(recovery, stop, wake))
            try:
                updates = (
                    ["message", "callback_query", "chat_join_request", "chat_member"]
                    if access
                    else ["message", "callback_query"]
                )
                logger.warning("polling_start_requested")
                await dispatcher.start_polling(bot, allowed_updates=updates)
                logger.warning("polling_returned")
            finally:
                logger.warning("polling_finished")
                stop.set()
                wake.set()
                if worker:
                    await worker
                if recovery_worker:
                    await recovery_worker
                if fulfillment_worker:
                    await fulfillment_worker
    finally:
        db.engine.dispose()


def main():
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
