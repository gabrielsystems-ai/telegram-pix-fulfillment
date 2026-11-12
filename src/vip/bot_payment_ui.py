"""Telegram presentation for the existing Pix services; no financial decisions here."""

import asyncio
import hashlib
import hmac
import logging
from html import escape
from io import BytesIO

import qrcode
from aiogram import F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from qrcode.exceptions import DataOverflowError
from qrcode.image.pure import PyPNGImage
from sqlalchemy.exc import SQLAlchemyError

from vip.errors import DomainError, GatewayError, IntegrationUnavailable

LOG = logging.getLogger(__name__)


def diagnostic_ref(bot, kind, value):
    """Keyed pseudonym: correlates attempts without logging IDs or bearer tokens."""
    return hmac.new(
        bot.token.encode(), f"payment-diagnostic-v1:{kind}:{value}".encode(), hashlib.sha256
    ).hexdigest()[:16]


def qr_png(code):
    buffer = BytesIO()
    qrcode.make(code, image_factory=PyPNGImage).save(buffer)
    return buffer.getvalue()


def payment_keyboard(data, *, native_copy=True):
    oid, state = data["order_id"], data["state"]
    rows = []
    code = data.get("copy_paste") if state == "awaiting_payment" else None
    if code:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Copiar código Pix",
                    **(
                        {"copy_text": CopyTextButton(text=code)}
                        if native_copy and len(code) <= 256
                        else {"callback_data": "pay:copy:" + oid}
                    ),
                )
            ]
        )
    if state == "awaiting_payment":
        rows.append(
            [
                InlineKeyboardButton(
                    text="Voltar", callback_data="pay:product:" + data["product_code"]
                )
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if data.get("payment_provider") == "pushinpay" and (
        state == "validity_unverified" or data.get("replacement_allowed")
    ):
        rows.append(
            [InlineKeyboardButton(text="Gerar novo Pix", callback_data="pay:replace:" + oid)]
        )
    elif state == "pix_pending" and data.get("charge_state"):
        rows.append(
            [InlineKeyboardButton(text="Verificar tentativa", callback_data="pay:recover:" + oid)]
        )
    if state in {"buyer_required", "legacy_draft"}:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Cancelar esta tentativa", callback_data="pay:cancel:" + oid
                )
            ]
        )
    if state in {"paid", "available", "retrying", "review_required"}:
        if data["product_code"] == "sample_video":
            rows.append([InlineKeyboardButton(text="▶️ Receber vídeo", callback_data="pay:video")])
        if data["product_code"] == "vip_permanent":
            rows.append([InlineKeyboardButton(text="🎟️ Acessar VIP", callback_data="pay:vip")])
    if state != "awaiting_payment":
        rows.append([InlineKeyboardButton(text="💬 Suporte", callback_data="pay:support")])
    rows.append(
        [
            InlineKeyboardButton(
                text="Voltar", callback_data="pay:product:" + data["product_code"]
            ),
            InlineKeyboardButton(text="Início", callback_data="pay:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


class BotPaymentUI:
    def __init__(self, service, show_menu, public_screen):
        self.service, self.show_menu, self.public_screen = service, show_menu, public_screen

    async def render(self, bot, user_id, data, notice=None, *, source=None):
        LOG.warning(
            "chat_purchase_state account_ref=%s order_ref=%s provider=%s state=%s",
            diagnostic_ref(bot, "buyer", user_id),
            diagnostic_ref(bot, "order", data["order_id"]),
            data.get("payment_provider", "omega"),
            data["state"],
        )
        price = data["amount_minor"]
        name = "Exemplo" if data["product_code"] == "sample_video" else data["product_name"]
        heading = f"<b>{escape(name)}</b> — R$ {price // 100},{price % 100:02d}"
        keyboard = payment_keyboard(data)
        if data["state"] == "buyer_required":
            text = heading + "\n\n📩 Informe seu e-mail para gerar o pagamento Pix."
            if notice:
                text += "\n\n" + escape(notice)
            await self.show_menu(bot, user_id, text, keyboard)
            return
        text = heading + "\n\n" + escape(data["message"])
        code = data.get("copy_paste")
        if data["state"] == "awaiting_payment":
            text = heading + "\n\nAguardando pagamento.\n\n"
            text += (
                "Copie o código Pix ou escaneie o QR Code."
                if code
                else "Estamos verificando a geração do Pix."
            )
        if notice:
            text += "\n\n" + escape(notice)
        if data.get("payment_provider") == "pushinpay" and data["state"] == "awaiting_payment":
            text += "\n\n<i>Pushin Pay processa o pagamento. Produto, entrega, suporte, "
            text += "conteúdo e qualidade são responsabilidade da Example Store.</i>"
        caption = text
        if data.get("pix_message_id") and (
            source is None or source.message_id == data["pix_message_id"]
        ):
            try:
                kwargs = dict(
                    chat_id=user_id,
                    message_id=data["pix_message_id"],
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
                if data.get("pix_file_id"):
                    await bot.edit_message_caption(caption=caption, **kwargs)
                else:
                    await bot.edit_message_text(text=caption, **kwargs)
                return
            except TelegramBadRequest as error:
                if "message is not modified" in error.message.lower():
                    return
                LOG.warning("pix_menu_edit_unavailable exception=%s", type(error).__name__)
        if not code:
            await self.show_menu(bot, user_id, text, keyboard, media="text")
            return
        upload = None
        try:
            if not data.get("pix_file_id"):
                upload = BufferedInputFile(
                    await asyncio.to_thread(qr_png, code), filename="pix.png"
                )
            try:
                message = await bot.send_photo(
                    user_id,
                    photo=data.get("pix_file_id") or upload,
                    caption=caption,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except TelegramBadRequest as error:
                if any(b.copy_text for row in keyboard.inline_keyboard for b in row) and any(
                    term in error.message.lower()
                    for term in ("copy_text", "copy text", "button_type_invalid")
                ):
                    keyboard = payment_keyboard(data, native_copy=False)
                    message = await bot.send_photo(
                        user_id,
                        photo=data.get("pix_file_id") or upload,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
                elif not data.get("pix_file_id"):
                    raise
                else:
                    upload = BufferedInputFile(
                        await asyncio.to_thread(qr_png, code), filename="pix.png"
                    )
                    message = await bot.send_photo(
                        user_id,
                        photo=upload,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=keyboard,
                    )
            file_id = message.photo[-1].file_id if message.photo else None
        except (DataOverflowError, ValueError):
            message = await bot.send_message(
                user_id, text, parse_mode="HTML", reply_markup=keyboard
            )
            file_id = None
        await asyncio.to_thread(
            self.service.remember_message,
            user_id,
            data["order_id"],
            message.message_id,
            file_id,
            code,
        )
        LOG.warning(
            "chat_pix_presented account_ref=%s order_ref=%s provider=%s",
            diagnostic_ref(bot, "buyer", user_id),
            diagnostic_ref(bot, "order", data["order_id"]),
            data.get("payment_provider", "omega"),
        )

    async def copy_code(self, bot, user_id, data):
        code = data.get("copy_paste") if data["state"] == "awaiting_payment" else None
        if not code:
            await self.render(bot, user_id, data)
            return
        text = "Toque no código para copiar:\n<code>" + escape(code) + "</code>"
        if len(text.encode("utf-16-le")) // 2 <= 4096:
            await bot.send_message(user_id, text, parse_mode="HTML")
        else:
            await bot.send_document(
                user_id,
                BufferedInputFile(code.encode(), filename="pix-copia-e-cola.txt"),
                caption="Pix Copia e Cola desta compra.",
            )

    async def error(
        self, bot, user_id, error, *, phase="unknown", back_product=None, order_id=None
    ):
        # No body, payer data, QR payload, tokens or provider response is logged.
        code = error.code if isinstance(error, (DomainError, GatewayError)) else "unavailable"
        LOG.warning(
            "chat_purchase_unavailable phase=%s exception=%s code=%s account_ref=%s",
            phase,
            type(error).__name__,
            code,
            diagnostic_ref(bot, "buyer", user_id),
        )
        if code == "product_already_owned":
            await self.public_screen(bot, user_id, "home")
            return
        notice = {
            "invalid_buyer_details": "Envie um e-mail válido, como seu@email.com.",
            "purchase_in_progress": "Você já tem uma compra em andamento. Conclua ou "
            "cancele a tentativa sem Pix antes de escolher outro produto.",
            "reconciliation_rate_limited": "Aguarde um minuto antes de atualizar novamente.",
            "pushinpay_expiration_unverified": "Não foi possível confirmar a validade deste Pix. "
            "Não efetue o pagamento; fale com o suporte.",
            "pix_already_created": "Este Pix já foi gerado. Aguarde a confirmação ou "
            "o vencimento; não gere outro pagamento.",
            "omega_resource_not_found": "A tentativa ainda não foi localizada. "
            "Não gere outro Pix; fale com o suporte para verificar esta compra.",
            "purchase_reconciliation_required": "Verifique sua tentativa anterior "
            "antes de iniciar outra compra.",
            "pushinpay_transaction_unknown": "Não foi possível confirmar a geração deste Pix. "
            "Fale com o suporte antes de iniciar outra compra.",
            "legacy_gateway_unavailable": "Sua tentativa anterior precisa ser conferida "
            "pelo suporte antes de iniciar outra compra. A consulta dessa tentativa "
            "está temporariamente indisponível.",
        }.get(
            code,
            "Não foi possível concluir agora. Confira a situação da compra antes de "
            "tentar novamente.",
        )
        try:
            state = await asyncio.to_thread(
                self.service.status, user_id, order_id, product_code=back_product
            )
        except (DomainError, SQLAlchemyError):
            from vip.bot_ui import context_keyboard

            await self.show_menu(bot, user_id, notice, context_keyboard(support=True))
        else:
            if code in {"purchase_in_progress", "purchase_reconciliation_required"}:
                LOG.warning(
                    "chat_purchase_conflict account_ref=%s order_ref=%s provider=%s state=%s",
                    diagnostic_ref(bot, "buyer", user_id),
                    diagnostic_ref(bot, "order", state["order_id"]),
                    state.get("payment_provider", "omega"),
                    state["state"],
                )
                notice = (
                    f"Você tem uma compra pendente de {escape(state['product_name'])}. "
                    "Para evitar outra cobrança, conclua ou verifique essa compra "
                    "antes de trocar de produto."
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Ver compra pendente",
                                callback_data="pay:view:" + state["order_id"],
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="Voltar",
                                callback_data="pay:product:"
                                + (back_product or state["product_code"]),
                            ),
                            InlineKeyboardButton(text="Início", callback_data="pay:home"),
                        ],
                    ]
                )
                await self.show_menu(bot, user_id, notice, keyboard, media="text")
                return
            if state["state"] == "buyer_required":
                # Keep the same draft and ask again; no payer values are echoed.
                await self.render(bot, user_id, state, notice)
            else:
                await self.render(bot, user_id, state, notice)

    def register(self, router, private):
        @router.callback_query(F.data.startswith("pay:"))
        async def payment_callback(query):
            try:
                await query.answer()
            except TelegramAPIError:
                pass
            if (
                query.from_user.is_bot
                or not isinstance(query.message, Message)
                or query.message.chat.type != "private"
                or query.message.chat.id != query.from_user.id
            ):
                return
            user_id, bot = query.from_user.id, query.bot
            action, _, argument = query.data[4:].partition(":")
            if action in {"buy", "replace", "status", "recover", "cancel"}:
                LOG.warning(
                    "chat_purchase_action action=%s account_ref=%s",
                    action,
                    diagnostic_ref(bot, "buyer", user_id),
                )
            try:
                if action in {"home", "support", "video", "vip", "product"}:
                    page = {
                        "home": "home",
                        "support": "support",
                        "video": "video",
                        "vip": "access",
                    }.get(
                        action,
                        {"sample_video": "sample", "vip_permanent": "vip"}.get(argument, "home"),
                    )
                    await self.public_screen(bot, user_id, page, query.message)
                    return
                if action == "buy":
                    data = await asyncio.to_thread(self.service.begin, user_id, argument)
                elif action == "replace":
                    previous = await asyncio.to_thread(self.service.status, user_id, argument)
                    data = await asyncio.to_thread(self.service.replace, user_id, argument)
                    if data["order_id"] != argument and previous.get("pix_message_id"):
                        try:
                            await bot.edit_message_caption(
                                chat_id=user_id,
                                message_id=previous["pix_message_id"],
                                caption="Pix substituído. Use apenas o código mais recente. "
                                "Se já pagou, não pague novamente.",
                                reply_markup=None,
                            )
                        except TelegramAPIError:
                            # A presentation failure never retries the financial POST.
                            LOG.warning("replaced_pix_caption_unavailable")
                elif action in {"status", "recover"}:
                    data = await asyncio.to_thread(self.service.refresh, user_id, argument)
                elif action in {"view", "copy"}:
                    data = await asyncio.to_thread(self.service.status, user_id, argument)
                    if action == "copy":
                        await self.copy_code(bot, user_id, data)
                        return
                elif action == "cancel":
                    await asyncio.to_thread(self.service.cancel, user_id, argument)
                    await self.public_screen(bot, user_id, "home")
                    return
                else:
                    return
                await self.render(bot, user_id, data, source=query.message)
            except (DomainError, GatewayError, IntegrationUnavailable, SQLAlchemyError) as error:
                await self.error(
                    bot,
                    user_id,
                    error,
                    phase=action,
                    back_product=argument if action == "buy" else None,
                    order_id=argument
                    if action in {"replace", "status", "recover", "view", "copy", "cancel"}
                    else None,
                )
            except TelegramAPIError as error:
                LOG.warning("chat_purchase_display_failed exception=%s", type(error).__name__)

        @private.message(F.text & ~F.text.startswith("/"))
        async def buyer_details(message):
            try:
                if not await asyncio.to_thread(self.service.wants_buyer, message.from_user.id):
                    return
                data = await asyncio.to_thread(
                    self.service.submit,
                    message.from_user.id,
                    message.text,
                    message.from_user.full_name,
                )
                await self.render(message.bot, message.from_user.id, data)
            except (DomainError, GatewayError, IntegrationUnavailable, SQLAlchemyError) as error:
                await self.error(message.bot, message.from_user.id, error, phase="submit")
            except TelegramAPIError as error:
                LOG.warning("chat_purchase_display_failed exception=%s", type(error).__name__)
