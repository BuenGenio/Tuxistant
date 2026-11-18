"""Mail search against 0.5.0 in the five languages the support scope names, done the way real
senders write: made-up sign-in mails in English, Spanish, Portuguese, Chinese and Arabic with the
code shapes Google, Microsoft, Apple, WhatsApp, Steam, Slack, banks and Chinese and Arabic senders
use, and everyday mails with dates, times, prices, order, flight and phone numbers, with and
without a line that talks of signing in, verifying or an offer's end. Each as plain text,
quoted-printable, base64 and HTML, in UTF-8 and in each language's older charset, with that
charset's label as senders get it wrong (Windows-1252 labelled ISO-8859-1, GBK labelled GB2312),
with decomposed accents and Arabic bidi marks, and with the subject as unencoded 8-bit bytes or
as encoded words split through a character. And everyday mails under a subject, or a text top,
with a word for an everyday code, and longer than what is read, whose rest talks of signing in.

Each mail is read three ways: by 0.5.0 (old_read, the copy in test_mail_differential, which agrees
with the v0.5.0 tag on every one of these mails) from the text Thunderbird's index holds; by this
version from the same text; and through Online Accounts, from the mail as an IMAP server sends it.

Every mail is made up. What no one sees is written as an escape here.
"""
import base64
import email.header
import html as htmlmod
import quopri
import random
import re
import sys
import unicodedata
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import imap_fake  # noqa: E402
from tests.test_mail_differential import html_text, old_read, old_tokens  # noqa: E402
from tuxistant import goamail, mail  # noqa: E402

SEED = 2203

# ---- sign-in mails ---------------------------------------------------------------------------
# (subject, body). {C} a code, {P} a password.
SIGN_IN = {
    "en": [
        ("Your Google verification code", "Your Google verification code is {C}.\n\nIf you didn't "
         "request this code, you can safely ignore this email."),
        ("{C} is your Google verification code", "Enter it on the sign-in page."),
        ("Google", "G-{C} is your Google verification code."),
        ("Facebook password reset", "Hi Sam,\nWe received a request to reset your Facebook "
         "password.\nEnter the following password reset code:\n{C}"),
        ("Your Microsoft account security code", "Security code: {C}\n\nIf you don't recognize "
         "the Microsoft account sam@example.com, you can remove your email address."),
        ("Your Apple ID Code", "Your Apple ID Code is: {C}. Don't share it with anyone."),
        ("Amazon.com: Sign-in attempt", "To continue, complete this verification step. We've "
         "sent a One Time Password (OTP). Please enter it to complete verification.\n\n{C}\n\n"
         "Don't share this OTP with anyone."),
        ("Your Steam account: Access from new computer", "Here is the Steam Guard code you need "
         "to login to account sam:\n\n{C}\n\nThis email was generated because of a login attempt."),
        ("Confirm your email address", "Your confirmation code is below. Enter it in your open "
         "browser window and we'll help you get signed in.\n\n{C}\n\nIf you didn't request this "
         "email, there's nothing to worry about."),
        ("PayPal: Your security code", "Your PayPal security code is {C}. It expires in 10 "
         "minutes. Don't share this code with anyone."),
        ("Sign in to Acme Bank", "Your one-time passcode is {C}. It expires in 5 minutes. Acme "
         "Bank will never ask you for it."),
        ("Discord", "Here is your Discord login verification code:\n{C}"),
        ("Acme login code: {C}", "Use it within 10 minutes."),
        ("Your Acme PIN", "Your new card PIN is {C}. Keep it safe."),
        ("Acme", "Enter this code to continue: {C}"),
        ("Your new Acme account", "Your username is sam.p and your temporary password is {P}\n"
         "Please change it after you sign in."),
        ("Wi-Fi details for your stay",
         "Network: Hotel-Guest\nPassword: {P}\nCheck-out is at 11:00."),
    ],
    "es": [
        ("Código de verificación de Google", "Tu código de verificación de Google es {C}.\n\nSi "
         "no has solicitado este código, puedes ignorar este correo."),
        ("{C} es tu código de verificación de Google", "Introdúcelo en la página."),
        ("Google", "G-{C} es tu código de verificación de Google."),
        ("Restablecer contraseña de Facebook", "Hola, Sam:\nHemos recibido una solicitud para "
         "restablecer tu contraseña de Facebook.\nIntroduce el siguiente código:\n{C}"),
        ("Código de seguridad de la cuenta Microsoft", "Código de seguridad: {C}\n\nSi no "
         "reconoces la cuenta Microsoft sam@example.com, puedes quitar tu dirección."),
        ("Tu código de WhatsApp",
         "Tu código de WhatsApp: {C}\nNo compartas este código con nadie."),
        ("Banco Acme: clave de un solo uso", "Tu clave de un solo uso es {C}. Caduca en 5 "
         "minutos. No la compartas con nadie."),
        ("Iniciar sesión en Acme", "Usa {C} para iniciar sesión en Acme. Caduca en 10 minutos."),
        ("Acme: verifica tu correo", "Introduce este código para verificar tu dirección de "
         "correo:\n\n{C}\n\nEl código caduca en 15 minutos."),
        ("Tu código de acceso", "Hola,\nTu código de acceso es: {C}\nVálido durante 10 minutos."),
        ("OTP Acme", "Su OTP para la operación de 1.250,00 MXN es {C}. Vigencia: 5 min."),
        ("{C} es tu código de Acme", "Gracias por registrarte."),
        ("Tu PIN de Acme", "Tu nuevo PIN es {C}. Guárdalo bien."),
        ("Bienvenido a Acme", "Tu usuario es sam.p y tu contraseña temporal es {P}\nCámbiala al "
         "iniciar sesión."),
        ("Datos del wifi", "Red: Hotel-Invitados\nContraseña: {P}\nLa salida es a las 11:00."),
    ],
    "pt": [
        ("Código de verificação do Google", "Seu código de verificação do Google é {C}.\n\nSe "
         "você não solicitou este código, ignore este e-mail."),
        ("{C} é seu código de verificação do Google", "Digite-o na página de login."),
        ("Google", "G-{C} é seu código de verificação do Google."),
        ("Redefinição de senha do Facebook", "Olá, Sam\nRecebemos uma solicitação para redefinir "
         "sua senha do Facebook.\nInsira o seguinte código:\n{C}"),
        ("Seu código do WhatsApp", "Seu código do WhatsApp: {C}\nNão compartilhe este código."),
        ("Nubank: código de acesso", "Seu código de acesso Nubank é {C}. Não compartilhe com "
         "ninguém."),
        ("Entrar na Acme", "Use {C} para entrar na sua conta Acme. Expira em 10 minutos."),
        ("Acme: confirme seu e-mail", "Digite este código para confirmar seu e-mail:\n\n{C}\n\n"
         "O código é válido por 15 minutos."),
        ("Código de segurança", "Código de segurança: {C}"),
        ("Token Acme", "Seu token de acesso é {C}. Válido por 5 minutos."),
        ("{C} é seu código Acme", "Obrigado por se cadastrar."),
        ("Acme", "Seu código de verificação é: {C}"),
        ("Bem-vindo à Acme",
         "Seu usuário é sam.p e sua senha provisória é {P}\nAltere-a ao entrar."),
        ("Dados do Wi-Fi", "Rede: Hotel-Hospedes\nSenha: {P}\nO check-out é às 11h."),
    ],
    "zh": [
        ("【阿里云】验证码", "【阿里云】验证码{C}，您正在登录，若非本人操作，请勿泄露。"),
        ("微信验证码", "【微信】验证码：{C}，5分钟内有效，请勿泄露给他人。"),
        ("支付宝校验码", "校验码{C}，您正在进行支付宝身份验证，请勿告诉别人。"),
        ("登录验证", "您的登录验证码是 {C}，10分钟内有效。"),
        ("Acme 账户安全", "您的动态密码为：{C}（请勿向任何人提供）"),
        ("Acme 注册", "您的验证码为：{C}，请在页面中输入完成验证。"),
        ("京东", "【京东】您的验证码为{C}，请勿告知他人。"),
        ("{C} 是您的 Acme 验证码", "感谢注册。"),
        ("Acme", "验证码：\n{C}\n请勿泄露"),
        ("Google 验证码", "您的 Google 验证码是 {C}。"),
        ("Acme 新账户", "您的用户名：sam.p\n初始密码：{P}\n请登录后修改。"),
        ("酒店Wi-Fi", "网络：Hotel-Guest\n密码：{P}\n退房时间：11:00"),
    ],
    "ar": [
        ("رمز التحقق من Google", "رمز التحقق من Google هو {C}.\n\nإذا لم تطلب هذا الرمز، "
         "يمكنك تجاهل هذه الرسالة."),
        ("Google", "G-{C} هو رمز التحقق من Google."),
        ("رمز واتساب", "رمز واتساب الخاص بك: {C}\nلا تشارك هذا الرمز مع أحد."),
        ("Acme: رمز الدخول", "رمز الدخول الخاص بك هو {C}. صالح لمدة 5 دقائق."),
        ("بنك Acme", "كلمة المرور لمرة واحدة: {C}\nلا تشاركها مع أي شخص."),
        ("تسجيل الدخول إلى Acme",
         "استخدم {C} لتسجيل الدخول إلى Acme. تنتهي الصلاحية خلال 10 دقائق."),
        ("تأكيد البريد الإلكتروني", "أدخل الرمز التالي لتأكيد بريدك الإلكتروني:\n\n{C}\n\n"
         "الرمز صالح لمدة 15 دقيقة."),
        ("رمز OTP", "رمز OTP الخاص بك هو {C}"),
        ("{C} هو رمز Acme الخاص بك", "شكرا لتسجيلك."),
        ("رمز التحقق", "رمز التحقق: \u200e{C}\u200f"),
        ("Acme", "رمزك هو {C} ولا تشاركه مع أحد."),
        # كود, "code", as many Arabic senders write it beside رمز.
        ("كود التحقق من Acme", "كود التحقق الخاص بك هو {C}. لا تشاركه مع أحد."),
        ("Acme", "كود التفعيل: {C}"),
        ("Acme", "{C} هو كود التحقق الخاص بك."),
        ("Acme", "كود التأكيد الخاص بك هو {C}"), ("Acme", "كود تسجيل الدخول: {C}"),
        ("Acme", "كود الأمان: {C}"), ("Acme", "كودك هو {C}"),
        # A one-time, a temporary and a secret code.
        ("Acme", "كود لمرة واحدة: {C}"), ("Acme", "الكود المؤقت {C}"),
        ("Acme", "الكود السري {C}"), ("Acme", "كودك المؤقت هو {C}"),
        ("حسابك الجديد", "اسم المستخدم: sam.p\nكلمة المرور المؤقتة: {P}\nيرجى تغييرها بعد "
         "تسجيل الدخول."),
        ("بيانات الواي فاي", "الشبكة: Hotel-Guest\nكلمة السر: {P}\nموعد المغادرة 11:00"),
    ],
}


def _digits(rng, n):
    while True:
        code = "".join(rng.choice("0123456789") for _ in range(n))
        if n != 4 or not re.fullmatch(r"(?:19|20)\d\d", code):
            return code


def _alnum(rng, n, alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"):
    while True:
        code = "".join(rng.choice(alphabet) for _ in range(n))
        if re.search(r"\d", code) and re.search(r"[A-Za-z]", code):
            return code


AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
WIDE_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


def code_shapes(rng, lang):
    """(shape name, code) in the shapes real senders use."""
    d6 = _digits(rng, 6)
    shapes = [("6 digits", d6), ("4 digits", _digits(rng, 4)), ("5 digits", _digits(rng, 5)),
              ("7 digits", _digits(rng, 7)), ("8 digits", _digits(rng, 8)),
              ("3-3 dash", f"{d6[:3]}-{d6[3:]}"), ("3 3 space", f"{d6[:3]} {d6[3:]}"),
              ("3 nbsp 3", f"{d6[:3]}\u00a0{d6[3:]}"),
              ("alnum 6", _alnum(rng, 6)), ("steam 5", _alnum(rng, 5)),
              ("slack", "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(3))
               + "-" + _alnum(rng, 3)),
              ("lower 8", _alnum(rng, 8, "abcdefghjkmnpqrstuvwxyz23456789"))]
    if lang == "ar":
        shapes += [("arabic-indic 6", d6.translate(AR_DIGITS)),
                   ("arabic-indic 4", _digits(rng, 4).translate(AR_DIGITS))]
    if lang == "zh":
        shapes += [("wide 6", d6.translate(WIDE_DIGITS))]
    return shapes


PASSWORDS = ("Xk9#pL2q", "Verano2026!", "Sol-Mar-42", "abc12345", "Senha@123", "hT7vQ2mW")

# ---- everyday mails ----------------------------------------------------------------------------
EVERYDAY = {
    "en": [
        ("Your Acme order {ORDER} has shipped", "Hi Sam,\n\nGood news: order {ORDER} is on its way."
         "\nEstimated delivery: {DATE}, {TIME}.\nOrder total: {PRICE}\n\nQuestions? Call us on "
         "{PHONE}.\n\nThanks for shopping with Acme."),
        ("Your flight {FLIGHT} to Madrid", "Booking reference: HMX4K2\nFlight {FLIGHT} departs "
         "London Heathrow on {DATE} at {TIME}.\nFare paid: {PRICE}\nManage your booking online or "
         "call {PHONE}."),
        ("Appointment reminder", "Your appointment with Dr Lee is on {DATE} at {TIME}.\nTo "
         "reschedule, call {PHONE}.\nA no-show fee of {PRICE} applies.\nReference {ORDER}."),
        ("Invoice {ORDER}", "Invoice {ORDER}\nAmount due: {PRICE}\nDue date: {DATE}\nPay by phone: "
         "{PHONE}"),
        ("Your table is booked", "We look forward to seeing you on {DATE} at {TIME}.\nDeposit: "
         "{PRICE}\nReservation no. {ORDER}\nCall {PHONE} to change it."),
        ("Acme receipt",
         "Thank you for your purchase.\nOrder {ORDER}\n{DATE} {TIME}\nTotal\n{PRICE}"
         "\nFlight {FLIGHT}\nHelpline {PHONE}"),
    ],
    "es": [
        ("Tu pedido {ORDER} ha sido enviado", "Hola, Sam:\n\nTu pedido {ORDER} está en camino.\n"
         "Entrega prevista: {DATE}, {TIME}.\nTotal del pedido: {PRICE}\n\n¿Dudas? Llámanos al "
         "{PHONE}.\n\nGracias por comprar en Acme."),
        ("Tu vuelo {FLIGHT} a Lisboa", "Localizador: HMX4K2\nEl vuelo {FLIGHT} sale de Madrid el "
         "{DATE} a las {TIME}.\nImporte pagado: {PRICE}\nGestiona tu reserva o llama al {PHONE}."),
        ("Recordatorio de cita", "Tu cita con la Dra. López es el {DATE} a las {TIME}.\nPara "
         "cambiarla, llama al {PHONE}.\nSe cobrará {PRICE} si no acudes.\nReferencia {ORDER}."),
        ("Factura {ORDER}", "Factura n.º {ORDER}\nImporte: {PRICE}\nVencimiento: {DATE}\nTeléfono "
         "de atención: {PHONE}"),
        ("Reserva confirmada", "Te esperamos el {DATE} a las {TIME}.\nSeñal: {PRICE}\nReserva "
         "n.º {ORDER}\nLlama al {PHONE} para modificarla."),
        ("Recibo de Acme", "Gracias por tu compra.\nPedido {ORDER}\n{DATE} {TIME}\nTotal\n{PRICE}"
         "\nVuelo {FLIGHT}\nAtención al cliente {PHONE}"),
    ],
    "pt": [
        ("Seu pedido {ORDER} foi enviado", "Olá, Sam\n\nSeu pedido {ORDER} está a caminho.\n"
         "Previsão de entrega: {DATE}, {TIME}.\nTotal do pedido: {PRICE}\n\nDúvidas? Ligue para "
         "{PHONE}.\n\nObrigado por comprar na Acme."),
        ("Seu voo {FLIGHT} para Madri", "Localizador: HMX4K2\nO voo {FLIGHT} parte de Guarulhos "
         "em {DATE} às {TIME}.\nValor pago: {PRICE}\nGerencie sua reserva ou ligue para {PHONE}."),
        ("Lembrete de consulta", "Sua consulta com a Dra. Souza é em {DATE} às {TIME}.\nPara "
         "remarcar, ligue para {PHONE}.\nTaxa de não comparecimento: {PRICE}\nProtocolo {ORDER}."),
        ("Fatura {ORDER}", "Fatura nº {ORDER}\nValor: {PRICE}\nVencimento: {DATE}\nCentral de "
         "atendimento: {PHONE}"),
        ("Reserva confirmada", "Esperamos você em {DATE} às {TIME}.\nSinal: {PRICE}\nReserva nº "
         "{ORDER}\nLigue para {PHONE} para alterar."),
        ("Recibo Acme", "Obrigado pela compra.\nPedido {ORDER}\n{DATE} {TIME}\nTotal\n{PRICE}\n"
         "Voo {FLIGHT}\nSAC {PHONE}"),
    ],
    "zh": [
        ("您的订单 {ORDER} 已发货", "尊敬的客户：\n您的订单{ORDER}已发货。\n预计送达：{DATE} {TIME}\n"
         "订单金额：{PRICE}\n如有疑问，请致电{PHONE}。"),
        ("航班{FLIGHT}行程确认", "预订编号：HMX4K2\n航班{FLIGHT}将于{DATE} {TIME}从北京首都机场起飞。\n"
         "票价：{PRICE}\n如需改签，请致电{PHONE}。"),
        ("预约提醒", "您预约的就诊时间为{DATE} {TIME}。\n如需改约，请致电{PHONE}。\n挂号费：{PRICE}\n"
         "预约号：{ORDER}"),
        ("订座成功", "期待您{DATE} {TIME}光临。\n订金：{PRICE}\n预订号：{ORDER}\n如需更改请致电{PHONE}。"),
        ("Acme 收据", "感谢您的购买。\n订单 {ORDER}\n{DATE} {TIME}\n合计\n{PRICE}\n航班 {FLIGHT}\n"
         "客服 {PHONE}"),
    ],
    "ar": [
        ("تم شحن طلبك {ORDER}", "مرحبا سام،\nطلبك رقم {ORDER} في الطريق إليك.\nموعد التسليم "
         "المتوقع: {DATE}، {TIME}.\nإجمالي الطلب: {PRICE}\nللاستفسار اتصل على {PHONE}.\nشكرا "
         "لتسوقك من Acme."),
        ("رحلتك {FLIGHT} إلى دبي", "رقم الحجز: HMX4K2\nتغادر الرحلة {FLIGHT} من الرياض في {DATE} "
         "الساعة {TIME}.\nالمبلغ المدفوع: {PRICE}\nلإدارة حجزك اتصل على {PHONE}."),
        ("تذكير بالموعد", "موعدك مع الدكتورة سارة في {DATE} الساعة {TIME}.\nلإعادة الجدولة اتصل "
         "على {PHONE}.\nرسوم عدم الحضور: {PRICE}\nالمرجع {ORDER}."),
        ("فاتورة {ORDER}", "رقم الفاتورة: {ORDER}\nالمبلغ المستحق: {PRICE}\nتاريخ الاستحقاق: "
         "{DATE}\nخدمة العملاء: {PHONE}"),
        ("تأكيد الحجز", "بانتظارك يوم {DATE} الساعة {TIME}.\nالعربون: {PRICE}\nرقم الحجز: "
         "{ORDER}\nللتعديل اتصل على {PHONE}."),
        ("إيصال Acme", "شكرا لشرائك.\nالطلب {ORDER}\n{DATE} {TIME}\nالإجمالي\n{PRICE}\nالرحلة "
         "{FLIGHT}\nخدمة العملاء {PHONE}"),
    ],
}
# A line real everyday mail ends with, which talks of signing in, checking or an offer's end.
FOOTERS = {
    "en": ("Log in to your account to track your order.", "Sign in to manage your booking.",
           "This offer expires on 31 March.", "Please verify your details before you travel.",
           "Verified by Acme Pay."),
    "es": ("Inicia sesión en tu cuenta para seguir tu pedido.",
           "Verifica tus datos antes de viajar.",
           "Haz login en tu cuenta para ver la factura.", "Esta oferta caduca el 31 de marzo."),
    "pt": ("Faça login na sua conta para acompanhar o pedido.",
           "Verifique seus dados antes de viajar.", "Esta oferta expira em 31 de março."),
    "zh": ("登录您的账户查看订单详情。", "请在出行前核实您的信息。", "登录 login 查看详情。"),
    "ar": ("سجل الدخول إلى حسابك لتتبع طلبك.", "يرجى التحقق من بياناتك قبل السفر.",
           "سجل login إلى حسابك."),
}
VALUES = {
    "DATE": {
        "en": ("12 March 2026", "Tue 12 Mar 2026", "12/03/2026", "03/12/2026", "2026-03-12",
               "March 12, 2026", "12.03.2026"),
        "es": ("12 de marzo de 2026", "12/03/2026", "12-03-2026", "martes, 12 de marzo",
               "12.03.2026", "12/03/26"),
        "pt": ("12 de março de 2026", "12/03/2026", "12/03", "terça-feira, 12/03/2026",
               "12.03.2026"),
        "zh": ("2026年3月12日", "3月12日", "2026-03-12", "2026/03/12", "２０２６年３月１２日",
               "2026.03.12"),
        "ar": ("12 مارس 2026", "12/03/2026", "١٢/٠٣/٢٠٢٦", "2026-03-12", "١٢ مارس ٢٠٢٦",
               "الثلاثاء 12 مارس"),
    },
    "TIME": {
        "en": ("10:00", "14:35", "10:00–12:00", "9am–1pm", "2:30 pm", "18:45", "10:00-12:00"),
        "es": ("10:00", "14:35", "10:00–12:00", "9:30", "14:00-16:00", "14.35 h", "20:30 h"),
        "pt": ("14h35", "10:00", "10h às 12h", "14:00–16:00", "9h30", "20h"),
        "zh": ("14:35", "10:00-12:00", "下午2:30", "上午9点", "18:45", "10:00—12:00"),
        "ar": ("14:35", "10:00–12:00", "٢:٣٠ م", "9:30 ص", "١٨:٤٥", "10:00 - 12:00"),
    },
    "PRICE": {
        "en": ("£49.99", "$1,250.00", "€129.95", "USD 1,299.00", "£1250", "$2,499", "GBP 450.00",
               "1250 GBP"),
        "es": ("49,99 €", "1.250,00 €", "$1.299", "1.299,00 MXN", "MXN 1,299.00", "4999 pesos",
               "125 €", "S/ 1,250.00", "1250 €", "1250 euros"),
        "pt": ("R$ 1.250,00", "R$ 89,90", "€ 49,99", "1250 reais", "R$1.299", "R$ 4999",
               "1.299,00 BRL"),
        "zh": ("¥1,250.00", "1250元", "人民币1,299.00元", "￥4999", "１２５０元", "HK$1,250",
               "4999 元", "RMB 1250"),
        "ar": ("1,250.00 ر.س", "250 درهم", "SAR 1,250", "AED 4,999", "١٢٥٠ ريال", "4999 جنيه",
               "$1,250", "1250 ريال", "٤٩٩٩ درهم"),
    },
    "ORDER": {
        "en": ("4829137", "112-4829137-5590011", "#A1B2C3", "ORD-2026-004821", "48291375",
               "W123456789", "#4829137"),
        "es": ("4829137", "PED-2026-004821", "48291375", "ES-2026-0042", "2026/0042", "#4829137"),
        "pt": ("4829137", "#48291375", "BR-2026-004821", "2026-0042", "482913"),
        "zh": ("202603124829", "4829137", "DD20260312001", "48291375", "#4829137"),
        "ar": ("4829137", "٤٨٢٩١٣٧", "SA-2026-004821", "48291375", "#4829137"),
    },
    "FLIGHT": {
        "en": ("BA2491", "BA 2491", "U2 8142", "EZY8142", "AA100", "VS3"),
        "es": ("IB3166", "IB 3166", "VY1004", "UX 1093", "LA8084"),
        "pt": ("TP1351", "LA 8084", "G3 1234", "AD4521", "JJ3456"),
        "zh": ("CA1831", "MU5101", "CZ 3101", "HU7605", "3U8888"),
        "ar": ("EK202", "SV 1020", "QR1023", "MS 777", "EY 101", "MS777"),
    },
    "PHONE": {
        "en": ("+44 20 7946 0000", "0800 123 4567", "(555) 010-4477", "1-800-555-0199",
               "020 7946 0018", "555-010-4477"),
        "es": ("900 123 456", "+34 912 345 678", "91 234 56 78", "+52 55 1234 5678",
               "(55) 1234-5678", "912345678"),
        "pt": ("(11) 3456-7890", "+55 11 98765-4321", "0800 123 4567", "+351 21 123 4567",
               "4004-1234", "11 3456-7890"),
        "zh": ("400-123-4567", "138 0013 8000", "+86 10 6552 9988", "010-65529988", "95588",
               "13800138000"),
        "ar": ("+971 4 123 4567", "800 123 4567", "920012345", "٠٥٠١٢٣٤٥٦٧", "+966 11 123 4567",
               "19999", "0501234567"),
    },
}
SLOTS = ("DATE", "TIME", "PRICE", "ORDER", "FLIGHT", "PHONE")
CHARSETS = {"en": "utf-8", "es": "iso-8859-1", "pt": "iso-8859-1", "zh": "gb18030",
            "ar": "windows-1256"}
# What senders really send: Windows-1252 labelled ISO-8859-1, as Thunderbird and every browser
# read that label, and GBK labelled GB2312, as they read that one. charset is the label, encoding
# the bytes.
MISLABELLED = {"en": ("iso-8859-1", "cp1252"), "es": ("iso-8859-1", "cp1252"),
               "pt": ("iso-8859-1", "cp1252"), "zh": ("gb2312", "gbk"),
               "ar": ("iso-8859-6", "iso-8859-6")}
# Smart punctuation, a euro sign and a character GB2312 lacks, which the mislabelled forms carry.
SMART = {"en": ("Total: €{N}", "Delivery window 10:00–12:00 … see you then"),
         "es": ("Total: €{N}", "Entrega entre 10:00–12:00 … hasta pronto"),
         "pt": ("Total: €{N}", "Entrega das 10:00–12:00 … até logo"),
         "zh": ("尊敬的王喆先生：金额{N}元", "配送时间 10:00–12:00…"),
         "ar": ("المبلغ {N}", "التسليم 10:00-12:00")}
SMART_NUMBERS = ("1250", "4999", "129", "48291")


# Everyday words for a code that name no sign-in code, under which a text of dates, prices, orders,
# flights and phone numbers was hidden whole: a pickup, gift card, referral or ticket code, an
# event's access code, a booking's confirmation code, a promo code given in the subject, a word in
# capitals, and the same in Spanish, Portuguese, Chinese and Arabic. And a security notice's
# subject that names a password or a PIN, over a text that names neither. And a bare "code" that
# goes on, a word that holds one for a password, as "resenha" and "desenhado" hold senha, or is
# one in another sense, as clave is a key date, 密码锁 a combination lock, 口令红包 a voucher and
# كلمة السر "the key to", and Pinterest's pins.
EVERYDAY_CODE_SUBJECTS = {
    "en": ("Your Acme pickup code", "Your Acme collection code is ready",
           "Your Acme gift card code", "Your Acme e-gift card code", "Your Acme referral code",
           "Your Acme offer code inside", "Your Acme rewards code",
           "Your Acme access code for the event", "Your Acme ticket code",
           "Your Acme door code for your stay", "Your Acme parking code", "Your Acme order code",
           "Your Acme redemption code", "Your Acme claim code",
           "Your Acme booking confirmation code", "Your Acme parcel locker pickup code",
           "Your invite code for Acme", "Use code SAVE20 for 20% off at Acme",
           "Extra 20% off with code FALL20", "Enter code ACME10 at checkout",
           "Your QR code ticket for Acme Live", "Your Acme Pickup Code",
           "Your Acme password was changed", "Your Acme code for 20% off", "Your code is inside",
           "Here's your code for free delivery", "Your Acme code expires Sunday",
           "Your new code for Acme Rewards",
           "Show the code at the counter to collect your Acme order",
           "Your code for 20% off at Acme", "Here's your code for a free Acme coffee",
           "Scan the code at the gate – Acme Live", "This code gets you 20% off at Acme",
           "Our code of conduct has changed", "New Pins for you on Acme", "Fresh Pins for you"),
    "es": ("Tu código de recogida Acme", "Código de canje de tu tarjeta regalo Acme",
           "Tu código de confirmación de la reserva Acme", "Código de barras de tu entrada Acme",
           "Tu código de invitado Acme", "Código de acceso al evento Acme",
           "Tu código de cupón Acme", "Tu código de pedido Acme", "Tu código de canje Acme",
           "Código de confirmación de tu reserva Acme", "Usa el código AHORRO20 en Acme",
           "Código de cupón Acme", "5 claves para ahorrar en Acme",
           "Palabras clave de tu búsqueda en Acme", "Acme: 4 claves para tu viaje",
           "Fechas clave de tu viaje a Madrid", "Momentos clave de tu pedido Acme",
           "Acme Air: tu clave de reservación", "Clave de rastreo de tu transferencia SPEI",
           "Tu clave de cliente Acme"),
    "pt": ("Código de barras do seu boleto Acme", "Seu código Pix Acme",
           "Código de retirada do seu pedido Acme", "Seu código de confirmação da reserva Acme",
           "Código do cupom Acme", "Seu código de resgate Acme", "Código de indicação Acme",
           "Seu código de retirada Acme", "Código do seu vale-presente Acme",
           "Use o código ACME10 e ganhe 10%", "Seu boleto Acme: código de barras",
           "Desenhado para você: a nova coleção Acme", "Resenha da semana Acme"),
    "zh": ("您的取件码 Acme", "Acme 兑换码", "Acme 优惠码已发放", "Acme 电子票二维码", "Acme 取餐码",
           "Acme 智能密码锁限时优惠", "Acme 口令红包来了"),
    "ar": ("رمز الطلب الخاص بك من Acme", "رمز الاستلام Acme", "رمز القسيمة Acme",
           "رمز الهدية من Acme", "رمز الدعوة Acme", "كود الكوبون Acme", "رمز الشحنة Acme",
           "كلمة السر لرحلة مثالية إلى دبي"),
}
# A text that opens with a word for a code or a password that gives its own, a promo banner, a
# booking's confirmation code or a hotel's Wi-Fi, or with an everyday code, a word for a code in
# another sense or one that holds a word for a password, under the subject of an order: the
# order's number and date in the subject were hidden.
EVERYDAY_CODE_TOPS = {
    "en": ("Use code SPRING10 for 10% off your next order.", "Your confirmation code is HMX4K2.",
           "Wi-Fi password: acme-guest", "Your Acme gift card code is inside.",
           "Thanks! Use code SAVE20 on your next order.",
           "Hi Sam, scan the code below at the locker to open it.", "Show the code at the gate.",
           "Track it with the code in the app.", "We've updated our code of conduct for sellers.",
           "Use the code at checkout for 10% off your next order.",
           "Scan the code at the door to get in.", "Enter the code from your receipt at the kiosk.",
           "Here's your code for free delivery on your next order."),
    "es": ("Usa el código VERANO10 en tu próximo pedido.", "Tu código de confirmación es HMX4K2.",
           "Contraseña del wifi: acme-guest", "Estos son los datos clave de tu pedido.",
           "Descubre las claves para disfrutar de tu compra."),
    "pt": ("Use o código VERAO10 no seu próximo pedido.", "Seu código de confirmação é HMX4K2.",
           "Senha do Wi-Fi: acme-guest",
           "Conte-nos o que achou: escreva uma resenha do seu pedido.",
           "Gostou? Deixe sua resenha e ajude outros clientes.", "Use the code below na loja."),
    "zh": ("Wi-Fi 密码：acmeguest", "使用优惠码 SPRING10 立减10元。", "复制淘口令打开手机淘宝，领取优惠。"),
    "ar": ("كلمة سر الواي فاي: acme-guest", "استخدم الكود SPRING10 لخصم 10%.",
           "كلمة السر لتجربة تسوق مثالية."),
}
ORDER_SUBJECTS = {"en": "Your Acme order {ORDER} has shipped – delivery {DATE}",
                  "es": "Tu pedido Acme {ORDER} ha sido enviado – entrega {DATE}",
                  "pt": "Seu pedido Acme {ORDER} foi enviado – entrega {DATE}",
                  "zh": "您的 Acme 订单 {ORDER} 已发货，预计 {DATE} 送达",
                  "ar": "تم شحن طلبك {ORDER} من Acme – التسليم {DATE}"}
# A security notice whose subject names a password or a PIN, and whose text names neither, with
# what it says past where that word reaches: its numbers were all hidden.
NOTICES = {
    "en": (("Your Acme PIN was changed", "Your Acme password was changed"),
           "Hi Sam,\n\nWe're writing to let you know about a change to your Acme account today. If "
           "you made it, there's nothing else to do, and you can carry on as before.\n\nYour "
           "next trip: flight {FLIGHT}, {DATE} at {TIME}. If this wasn't you, call {PHONE} and "
           "quote case {ORDER}."),
    "es": (("Tu contraseña de Acme ha cambiado", "Tu PIN de Acme ha cambiado"),
           "Hola, Sam:\n\nTe escribimos para avisarte de que hoy se ha hecho un cambio en tu "
           "cuenta de Acme. Si lo has hecho tú, no tienes que hacer nada más.\n\nTu próximo "
           "viaje: vuelo {FLIGHT}, {DATE} a las {TIME}. Si no has sido tú, llama al {PHONE} y "
           "cita el caso {ORDER}."),
    "pt": (("A sua senha Acme foi alterada",),
           "Olá, Sam\n\nEscrevemos para avisar que hoje houve uma alteração na sua conta Acme. "
           "Se foi você, não precisa fazer mais nada e pode continuar como antes.\n\nPróxima "
           "viagem: voo {FLIGHT}, {DATE} às {TIME}. Se não foi você, ligue {PHONE} e informe o "
           "protocolo {ORDER}."),
    "zh": (("您的Acme密码已更改",),
           "您好！\n\n" + "我们写信通知您，您的Acme账户今天发生了变更。如果是您本人操作，则无需进行任何"
           "操作，您可以照常使用。" * 4 + "\n\n下次行程：航班 {FLIGHT}，{DATE} {TIME}。如果不是您"
           "本人操作，请致电 {PHONE}，案例编号 {ORDER}。"),
}
# A line real everyday mail has that talks of signing in beside a date, a time, a price or an
# order: the time as Portuguese writes it, 14h35 or 14h35min, and a span of time, 24-hour, 24hrs,
# 10-min or 2-night, were hidden as codes, as were letters and digits on such a line, which
# docs/ROADMAP.md lists.
TALKING_LINES = {
    "en": ("Your e-ticket is valid for travel on {DATE} only.",
           "This fare expires at {TIME} today.",
           "Log in to check in from {TIME}.", "Please verify your seat before {TIME}.",
           "Sign in to see order {ORDER} and pay {PRICE}.", "Sign in for 24-hour support.",
           "Log in to manage your 2-night stay from {DATE}.",
           "Sign in to claim your 10-min slot at {TIME}.",
           "Log in for the 30-minute webinar on {DATE}.", "Sign in for our 24hrs support line."),
    "es": ("Verifica tus datos del vuelo antes de las {TIME}.",
           "Haz login para facturar desde las {TIME} del {DATE}."),
    "pt": ("Faça login para fazer o check-in a partir das {TIME}.",
           "Embarque às 14h35, faça login para ver o cartão.",
           "Verifique o seu assento até 21h30.",
           "Embarque às 14h35min, faça login para ver o cartão.",
           "Faça login até as 21h30min para fazer o check-in.",
           "Verifique o seu assento e faça login até 16h50min."),
    "zh": ("请 login 后办理值机，{TIME} 开始。",),
    "ar": ("سجل login لإتمام إجراءات السفر من الساعة {TIME}.",),
}
# An airline's HTML itinerary, longer than what Online Accounts fetches, with a footer past that
# which talks of signing in, and a plain e-ticket longer than what is read in Thunderbird's index,
# whose rest talks of it: their flights and the booking's reference at the top were hidden.
ITINERARY = {
    "en": ("Flight {F1}: London Heathrow T5 to Madrid, Fri 17 April 2026, departs 16:50.",
           "Flight {F2}: Madrid to London Heathrow, Sun 26 April 2026, departs 09:15.",
           "Baggage allowance: 1 cabin bag up to 23kg. Seat selection opens at check-in.",
           "Booking reference: {REF}. Log in to manage your booking."),
    "es": ("Vuelo {F1}: Madrid a Lisboa, viernes 17 de abril de 2026, sale a las 16:50.",
           "Vuelo {F2}: Lisboa a Madrid, domingo 26 de abril de 2026, sale a las 09:15.",
           "Equipaje: 1 maleta de cabina de hasta 10 kg. Elige asiento al facturar.",
           "Localizador: {REF}. Haz login para gestionar tu reserva."),
    "pt": ("Voo {F1}: Lisboa para Madrid, sexta-feira 17/04/2026, parte às 16h50.",
           "Voo {F2}: Madrid para Lisboa, domingo 26/04/2026, parte às 09h15.",
           "Bagagem: 1 mala de cabine até 10 kg. Escolha o assento no check-in.",
           "Reserva: {REF}. Faça login para gerir a sua reserva."),
    "zh": ("航班 {F1}：北京首都 至 上海虹桥，2026年4月17日 16:50 起飞。",
           "航班 {F2}：上海虹桥 至 北京首都，2026年4月26日 09:15 起飞。",
           "行李额：手提行李1件，不超过10公斤。值机时可选座。", "预订编号：{REF}。登录 login 管理您的预订。"),
    "ar": ("الرحلة {F1}: من الرياض إلى دبي، الجمعة 17 أبريل 2026، المغادرة 16:50.",
           "الرحلة {F2}: من دبي إلى الرياض، الأحد 26 أبريل 2026، المغادرة 09:15.",
           "الأمتعة: حقيبة يد واحدة حتى 7 كغ. اختر مقعدك عند تسجيل الوصول.",
           "رقم الحجز: {REF}. سجل login لإدارة حجزك."),
}
ITINERARY_FLIGHTS = {"en": ("BA2617", "IB3402"), "es": ("IB3402", "TP1352"),
                     "pt": ("TP1352", "IB3403"), "zh": ("CA1832", "MU5102"),
                     "ar": ("EK203", "SV1021")}
TICKET_REST = {"en": "This ticket is valid for one year from the date of issue.",
               "es": "Verifica tus datos antes de viajar.",
               "pt": "Verifique os seus dados antes de viajar."}
# A text that opens with a promo code, and then a long link or a run of blank lines before an
# order: the second pass measured the code's reach with the link and the blank lines taken out.
JOINED_TOPS = {"en": "Use code SPRING10 at checkout for 10% off your next order:",
               "es": "Usa el código VERANO10 en tu próxima compra:",
               "pt": "Use o código VERAO10 na sua próxima compra:",
               "zh": "优惠码 SPRING10 下次购物可用：", "ar": "رمز الخصم SPRING10 لطلبك القادم:"}
JOINED_TEXTS = {"en": "Your order {ORDER} ships on {DATE}. Call {PHONE}.",
                "es": "Tu pedido {ORDER} llega el {DATE}. Llámanos al {PHONE}.",
                "pt": "Seu pedido {ORDER} chega em {DATE}. Ligue {PHONE}.",
                "zh": "您的订单 {ORDER} 已发货，预计 {DATE} 送达。客服电话 {PHONE}。",
                "ar": "طلبك {ORDER} يصل في {DATE}. اتصل على {PHONE}."}
LINK = "https://www.acme.example/track?id=" + "a1b2c3d4e5" * 14
# A label over a number alone on its line with a "#" glued to it, as a receipt sets an order out.
# A footer's talk of signing in split by a line end, as a plain part wrapped at a width splits it,
# which 0.5.0 didn't read as talk.
WRAPPED_TALK = ("You can log\nin to your account to see your orders.",
                "Please sign\nin to the app to change your delivery.",
                "This offer is valid\nfor 30 days from today.",
                "Use the one\ntime link in the app to manage alerts.",
                "Turn on two\nfactor protection in your settings.",
                "Our bags are single\nuse free.",
                "Logging\nin is not needed to track it.")
ORDER_LABELS = {"en": "Order number", "es": "Número de pedido", "pt": "Número do pedido",
                "zh": "订单号", "ar": "رقم الطلب"}
# A part that names no charset, in the older one of its language, as an older sender's script
# sends it: Thunderbird guesses the charset, and its index holds "código", 验证码 and رمز.
UNLABELLED = {"en": "cp1252", "es": "cp1252", "pt": "cp1252", "zh": "gbk", "ar": "cp1256"}


# ---- HTML -------------------------------------------------------------------------------------
def html_of(lang, body, tokens, style, rng):
    """An HTML part for a body: each line a paragraph, each token in an inline tag, and spaces in
    prices and dashes in ranges written as character references, as templates write them."""
    rtl = ' dir="rtl"' if lang == "ar" else ""
    out = []
    for line in body.split("\n"):
        esc = htmlmod.escape(line, quote=False)
        for tok, _kind in tokens:
            if not tok or htmlmod.escape(tok, quote=False) not in esc:
                continue
            t = htmlmod.escape(tok, quote=False)
            t2 = t.replace("\u00a0", "&nbsp;").replace("–", "&ndash;").replace("—", "&mdash;")
            if style == "cells" and re.fullmatch(r"[0-9A-Za-z]{4,8}", tok):
                inner = "".join(f'<td style="padding:4px">{c}</td>' for c in tok)
                wrapped = f"</p><table><tr>{inner}</tr></table><p{rtl}>"
            elif style == "spans" and re.fullmatch(r"[0-9A-Za-z]{4,8}", tok):
                wrapped = "".join(f'<span class="d">{c}</span>' for c in tok)
            elif style == "pairs" and re.fullmatch(r"[0-9A-Za-z]{6}", tok):
                wrapped = "".join(f"<span>{tok[i:i + 2]}</span>" for i in range(0, 6, 2))
            elif lang == "ar":
                wrapped = f'<span dir="ltr">{t2}</span>'
            else:
                wrapped = f"<strong>{t2}</strong>"
            esc = esc.replace(t, wrapped, 1)
        out.append(f"<p{rtl}>{esc}</p>" if esc.strip() else "<br>")
    page = "".join(out)
    if style == "table":   # a label and value in two cells, as receipts do
        page = re.sub(r"<p([^>]*)>([^<:：]{1,40})[:：] ?(.*?)</p>",
                      r"<table><tr><td>\2</td><td>\3</td></tr></table>", page)
    return ('<!DOCTYPE html><html><head><style>.d{display:inline-block}</style></head>'
            f'<body{rtl}><div style="font-family:Arial">{page}</div></body></html>')


def index_text(plain, markup):
    """The body as Thunderbird's index holds it (see test_mail_differential.html_text)."""
    return plain if plain is not None else html_text(markup)


# ---- the corpus -------------------------------------------------------------------------------
FORMS = ("plain-8bit", "plain-qp", "plain-b64", "plain-legacy", "html-8bit", "html-qp",
         "alt-qp", "html-legacy-b64")


def corpus():
    rng = random.Random(SEED)
    mails = []

    def add(**m):
        cte, kind = m.pop("form").split("-", 1)[1], m["form_name"]
        m["cte"] = {"8bit": "8bit", "qp": "qp", "b64": "base64", "legacy": "8bit",
                    "legacy-b64": "base64"}[cte]
        m["charset"] = CHARSETS[m["lang"]] if "legacy" in kind else "utf-8"
        m["index"] = index_text(m["plain"] if m["plain"] is not None else None, m["html"])
        mails.append(m)

    # Sign-in mails: every template with every shape, in a form picked in turn.
    n = 0
    for lang, templates in SIGN_IN.items():
        for subject, body in templates:
            if "{P}" in body:
                for password in PASSWORDS:
                    form = FORMS[n % len(FORMS)]
                    n += 1
                    plain = body.replace("{P}", password)
                    tokens = [(password, "password")]
                    style = rng.choice(("inline", "table"))
                    markup = html_of(lang, plain, tokens, style, rng)
                    add(lang=lang, kind="code", why=f"{lang} password {subject[:28]!r} | "
                        f"{password!r} | {form}", subject=subject, form=form, form_name=form,
                        plain=None if form.startswith("html") else plain,
                        html=markup if form.startswith(("html", "alt")) else None,
                        tokens=tokens)
                continue
            for shape, code in code_shapes(rng, lang):
                form = FORMS[n % len(FORMS)]
                n += 1
                text = body.replace("{C}", code)
                subj = subject.replace("{C}", code)
                tokens = [(code, "code:" + shape)]
                style = rng.choice(("inline", "inline", "cells", "spans", "pairs"))
                markup = html_of(lang, text, tokens, style, rng)
                add(lang=lang, kind="code", why=f"{lang} {subject[:28]!r} {body[:20]!r} | "
                    f"{shape} {code!r} | {form}/{style}", subject=subj, form=form, form_name=form,
                    plain=None if form.startswith("html") else text,
                    html=markup if form.startswith(("html", "alt")) else None, tokens=tokens)
    # Everyday mails: every template with values picked in turn, in every form, with and
    # without a footer that talks of signing in.
    for lang, templates in EVERYDAY.items():
        for t, (subject, body) in enumerate(templates):
            for f, form in enumerate(FORMS):
                for footer in (None, *FOOTERS[lang]):
                    if footer is not None and f % 2:
                        continue
                    picked = {slot: VALUES[slot][lang][rng.randrange(len(VALUES[slot][lang]))]
                              for slot in SLOTS}
                    text, subj = body, subject
                    for slot, value in picked.items():
                        text, subj = text.replace("{" + slot + "}", value), \
                            subj.replace("{" + slot + "}", value)
                    if footer:
                        text += "\n\n" + footer
                    tokens = [(value, slot.lower()) for slot, value in picked.items()
                              if "{" + slot + "}" in body or "{" + slot + "}" in subject]
                    markup = html_of(lang, text, tokens, rng.choice(("inline", "table")), rng)
                    add(lang=lang, kind="footer" if footer else "ordinary",
                        why=f"{lang} {subject[:24]!r} | {form} | footer={footer!r}",
                        subject=subj, form=form, form_name=form,
                        plain=None if form.startswith("html") else text,
                        html=markup if form.startswith(("html", "alt")) else None, tokens=tokens)
    # Mislabelled charsets: Windows-1252 bytes labelled ISO-8859-1, GBK labelled GB2312, with
    # smart punctuation, a euro sign or a character GB2312 lacks next to numbers; with and
    # without a footer that talks of signing in, and a password with an ellipsis or a dash.
    for lang in EVERYDAY:
        label, encoding = MISLABELLED[lang]
        for line in SMART[lang]:
            for number in SMART_NUMBERS:
                for footer in (None, FOOTERS[lang][0], FOOTERS["en"][0]):
                    for cte in ("8bit", "qp"):
                        shown_line = line.replace("{N}", number)
                        text = f"Acme\n{shown_line}\n" + (footer or "")
                        token = shown_line[shown_line.index(number) - 1:] if line.startswith(
                            "Total: €") else number
                        mails.append({
                            "lang": lang, "kind": "footer" if footer else "ordinary",
                            "why": f"{lang} mislabelled {encoding} as {label} {line[:18]!r} "
                                   f"{number} | {cte} | footer={footer!r}",
                            "subject": "Acme", "plain": text, "html": None, "cte": cte,
                            "charset": label, "encoding": encoding, "index": text,
                            "tokens": [(token.strip("“”"), "price")]})
        for password in ("Sol…Mar42", "Playa…Soleado", "Sol–Mar42", "“Sol2026”"):
            body = {"en": "Your temporary password is {P} for today.",
                    "es": "Tu contraseña temporal es: {P} para hoy.",
                    "pt": "Sua senha temporária: {P} para hoje.",
                    "zh": "临时密码：{P}，今日有效。",
                    "ar": "كلمة المرور المؤقتة: {P}"}[lang].replace("{P}", password)
            mails.append({"lang": lang, "kind": "code",
                          "why": f"{lang} mislabelled {encoding} as {label} password {password!r}",
                          "subject": "Acme", "plain": body, "html": None, "cte": "8bit",
                          "charset": label, "encoding": encoding, "index": body,
                          "tokens": [(password.strip("“”"), "password")]})
    # Unencoded 8-bit subjects (UTF-8, or the body's own charset), as many senders write them,
    # on sign-in mails whose code is in the subject and whose text doesn't name it near its top.
    raw_subjects = {
        "en": ("Your Acme code: {C}", "Hello Sam, thanks for using Acme. — The Acme team"),
        "es": ("Tu código de Acme es {C}", "Hola, Sam: gracias por usar Acme."),
        "pt": ("Seu código de acesso: {C}", "Olá! Obrigado por usar a Acme."),
        "zh": ("【Acme】您的验证码是{C}", "亲爱的用户，感谢您使用Acme。"),
        "ar": ("رمز الدخول إلى Acme هو {C}", "مرحبا، شكرا لاستخدامك Acme."),
    }
    for lang, (subject, body) in raw_subjects.items():
        for shape, code in code_shapes(rng, lang)[:6]:
            for raw in ("utf-8", {"en": "cp1252", "es": "cp1252", "pt": "cp1252", "zh": "gbk",
                                  "ar": "cp1256"}[lang]):
                mails.append({"lang": lang, "kind": "code",
                              "why": f"{lang} raw {raw} subject {subject[:24]!r} | {shape} "
                                     f"{code!r}",
                              "subject": subject.replace("{C}", code), "subject_raw": raw,
                              "plain": body, "html": None, "cte": "8bit",
                              "charset": "utf-8" if raw == "utf-8" else
                              {"cp1252": "iso-8859-1", "gbk": "gb2312",
                               "cp1256": "windows-1256"}[raw],
                              "encoding": None if raw == "utf-8" else raw, "index": body,
                              "tokens": [(code, "code:" + shape)]})
    # Everyday mails with a word that holds one for a code but names none: Chinese 号码 ("number":
    # order, phone), a postal code, and a subject that names a discount code.
    near = {
        "zh": [("订单通知", "订单号码：{ORDER}\n下单时间：{DATE} {TIME}\n联系电话：{PHONE}\n会员有效期：2025–2026"),
               ("您的优惠码", "您好，\n活动时间：{DATE} {TIME}\n客服：{PHONE}\n满{PRICE}可用")],
        "es": [("Envío confirmado", "Código postal: 28013\nEntrega: {DATE} a las {TIME}\n"
                "Teléfono: {PHONE}\nTotal: {PRICE}"),
               ("Tu código de descuento", "Hola:\nLa oferta es válida hasta el {DATE}, a las "
                "{TIME}.\nLlámanos al {PHONE}. Total: {PRICE}")],
        "pt": [("Envio confirmado", "CEP: 01310-100\nCódigo de rastreio: LX123456789BR\n"
                "Entrega: {DATE} às {TIME}\nTelefone: {PHONE}\nTotal: {PRICE}"),
               ("Seu código de desconto", "Olá!\nA oferta vale até {DATE}, às {TIME}.\n"
                "Ligue {PHONE}. Total: {PRICE}")],
        "ar": [("تأكيد الشحن", "الرمز البريدي: 12345\nموعد التسليم: {DATE} الساعة {TIME}\n"
                "الهاتف: {PHONE}\nالمبلغ: {PRICE}"),
               # كود ("code") for a discount, at the top of a bill, names no code being given.
               ("فاتورة Acme", "كود الخصم موجود في التطبيق.\nرقم الطلب: {ORDER}\nموعد التسليم: "
                "{DATE} الساعة {TIME}\nخدمة العملاء: {PHONE}"),
               ("رمز الخصم الخاص بك", "مرحبا،\nالعرض ساري حتى {DATE} الساعة {TIME}.\n"
                "اتصل على {PHONE}. المبلغ: {PRICE}")],
        "en": [("Your Acme discount code inside", "Hello,\nThe sale ends {DATE} at {TIME}.\n"
                "Call {PHONE}. Spend {PRICE} or more."),
               ("Your Acme promo code", "Hi Sam,\nYour order {ORDER} on flight {FLIGHT} is on "
                "{DATE} at {TIME}.\nCall {PHONE}.")],
    }
    for lang, frames in near.items():
        for subject, body in frames:
            for i in range(10):
                picked = {slot: VALUES[slot][lang][(i + k) % len(VALUES[slot][lang])]
                          for k, slot in enumerate(SLOTS)}
                text = body
                for slot, value in picked.items():
                    text = text.replace("{" + slot + "}", value)
                tokens = [(value, slot.lower()) for slot, value in picked.items()
                          if "{" + slot + "}" in body]
                for form in ("plain-8bit", "html-qp"):
                    markup = html_of(lang, text, tokens, "inline", rng)
                    add(lang=lang, kind="ordinary", why=f"{lang} near {subject[:20]!r} | {form}",
                        subject=subject, form=form, form_name=form,
                        plain=None if form.startswith("html") else text,
                        html=markup if form.startswith("html") else None, tokens=tokens)
    # A code written two characters to an inline tag that a browser and Thunderbird's index join,
    # as goamail's list of inline tags has <span> and <code> but not <kbd>, <samp> or <bdi>.
    for lang, line in (("en", "Your Acme verification code is {T}"),
                       ("es", "Tu código de Acme es {T}"),
                       ("pt", "Seu código Acme é {T}"), ("zh", "您的验证码是{T}"),
                       ("ar", "رمز التحقق الخاص بك هو {T}")):
        for tag in ("span", "kbd", "samp", "var", "bdi", "abbr", "time", "ins"):
            code = _alnum(rng, 6)
            pairs = "".join(f"<{tag}>{code[i:i + 2]}</{tag}>" for i in range(0, 6, 2))
            markup = f"<html><body><p>{line.replace('{T}', pairs)}</p></body></html>"
            mails.append({"lang": lang, "kind": "code", "why": f"{lang} pairs in <{tag}> {code!r}",
                          "subject": "Acme", "plain": None, "html": markup, "cte": "8bit",
                          "charset": "utf-8", "index": index_text(None, markup),
                          "tokens": [(code, "code:pairs")]})
    # Parts that name no charset, in each language's older one (see UNLABELLED): sign-in mails,
    # plain and in HTML with no <meta>, and an everyday one.
    for lang, encoding in UNLABELLED.items():
        frames = [frame for frame in SIGN_IN[lang] if "{P}" not in frame[1]][:4]
        for n, (subject, body) in enumerate(frames):
            code = _digits(rng, 6)
            text = body.replace("{C}", code)
            for form in ("plain-8bit", "plain-qp", "html-8bit"):
                markup = html_of(lang, text, [(code, "code")], "inline", rng) \
                    if form.startswith("html") else None
                mails.append({"lang": lang, "kind": "code",
                              "why": f"{lang} unlabelled {encoding} {subject[:24]!r} | {form}",
                              "subject": subject.replace("{C}", code),
                              "plain": None if markup else text, "html": markup,
                              "cte": "qp" if form.endswith("qp") else "8bit", "charset": None,
                              "encoding": encoding, "index": index_text(None if markup else text,
                                                                         markup),
                              "tokens": [(code, "code:6 digits")]})
        subject, body = EVERYDAY[lang][0]
        picked = {slot: VALUES[slot][lang][0] for slot in SLOTS}
        for slot, value in picked.items():
            body, subject = body.replace("{" + slot + "}", value), \
                subject.replace("{" + slot + "}", value)
        mails.append({"lang": lang, "kind": "ordinary", "why": f"{lang} unlabelled {encoding} everyday",
                      "subject": subject, "plain": body, "html": None, "cte": "8bit",
                      "charset": None, "encoding": encoding, "index": body,
                      "tokens": [(value, slot.lower()) for slot, value in picked.items()
                                 if value in body]})
    # Decomposed accents (NFD), as some mail programs write them, in Spanish and Portuguese.
    for m in [m for m in mails if m["lang"] in ("es", "pt") and m.get("encoding") is None
              and m["cte"] == "8bit" and m["html"] is None][::3]:
        nfd = dict(m, why="NFD " + m["why"], charset="utf-8",
                   subject=unicodedata.normalize("NFD", m["subject"]),
                   plain=unicodedata.normalize("NFD", m["plain"]),
                   index=unicodedata.normalize("NFD", m["index"]))
        mails.append(nfd)
    # Arabic with the bidi marks templates put round numbers: LRM/RLM, and ALM.
    for m in [m for m in mails if m["lang"] == "ar" and m.get("encoding") is None
              and m["html"] is None][::2]:
        for mark in ("\u200e", "\u061c", "\u2066"):
            close = "\u2069" if mark == "\u2066" else mark

            def marked(text):
                for token, _kind in m["tokens"]:
                    text = text.replace(token, mark + token + close)
                return text
            mails.append(dict(m, why=f"bidi U+{ord(mark):04X} " + m["why"], charset="utf-8",
                              subject=marked(m["subject"]), plain=marked(m["plain"]),
                              index=marked(m["index"])))
    return mails + more(random.Random(SEED + 1))


def more(rng) -> list[dict]:
    """Subjects split into encoded words through a character; everyday mails under a subject or
    a text top with a word for an everyday code, or for a code or a password that gives its own;
    security notices; lines that talk of signing in beside a time; and mails longer than what is
    read, whose rest talks of it."""
    mails = []

    def fill(template, picked):
        for slot, value in picked.items():
            template = template.replace("{" + slot + "}", value)
        return template

    def pick(lang, i):
        return {slot: VALUES[slot][lang][(i * 7 + j * 3) % len(VALUES[slot][lang])]
                for j, slot in enumerate(SLOTS)}

    def everyday(lang, why, subject, text, tokens, form):
        if not tokens:
            return
        markup = html_of(lang, text, tokens, "inline", rng) if form == "html-qp" else None
        mails.append({"lang": lang, "kind": "ordinary", "why": f"{lang} {why} | {form}",
                      "subject": subject, "plain": text if markup is None else None,
                      "html": markup, "cte": "qp" if markup else "8bit", "charset": "utf-8",
                      "encoding": None, "index": index_text(text if markup is None else None,
                                                            markup),
                      "tokens": tokens})

    # A code in a subject sent as encoded words split every few bytes, through a character
    # where the width falls inside one, as some senders split them, B and Q, over a text that
    # doesn't name the code.
    raw_subjects = {"en": ("Your Acme code: {C}", "Hello Sam, thanks for using Acme."),
                    "es": ("Tu código de Acme es {C}", "Hola, Sam: gracias por usar Acme."),
                    "pt": ("Seu código de acesso Acme: {C}", "Olá! Obrigado por usar a Acme."),
                    "zh": ("【Acme】您的验证码是{C}", "亲爱的用户，感谢您使用Acme。"),
                    "ar": ("رمز الدخول إلى Acme هو {C}", "مرحبا، شكرا لاستخدامك Acme.")}
    for lang, (subject, body) in raw_subjects.items():
        for shape, code in code_shapes(rng, lang)[:3]:
            for kind in ("b", "q"):
                for width in (5, 6, 7, 9, 10, 11, 13):
                    mails.append({"lang": lang, "kind": "code",
                                  "why": f"{lang} {kind} subject split every {width} bytes "
                                         f"{subject[:24]!r} | {shape} {code!r}",
                                  "subject": subject.replace("{C}", code),
                                  "subject_split": (kind, width), "plain": body, "html": None,
                                  "cte": "8bit", "charset": "utf-8", "encoding": None,
                                  "index": body, "tokens": [(code, "code:" + shape)]})
    i = 0
    for lang, subjects in EVERYDAY_CODE_SUBJECTS.items():
        for n, subject in enumerate(subjects):
            body = EVERYDAY[lang][n % len(EVERYDAY[lang])][1]
            for form in ("plain-8bit", "html-qp"):
                i += 1
                picked = pick(lang, i)
                everyday(lang, f"code subject {subject!r}", subject, fill(body, picked),
                         [(value, slot.lower()) for slot, value in picked.items()
                          if "{" + slot + "}" in body], form)
    thanks = {"en": "Thanks for shopping with Acme.", "es": "Gracias por comprar en Acme.",
              "pt": "Obrigado por comprar na Acme.", "zh": "感谢您在Acme购物。",
              "ar": "شكرا لتسوقك من Acme."}
    for lang, tops in EVERYDAY_CODE_TOPS.items():
        for top in tops:
            for t in range(3):
                i += 1
                picked = pick(lang, i)
                everyday(lang, f"text top {top!r}", fill(ORDER_SUBJECTS[lang], picked),
                         f"{top}\n\n{thanks[lang]}",
                         [(picked["ORDER"], "order"), (picked["DATE"], "date")],
                         ("plain-8bit", "html-qp")[t % 2])
    for lang, (subjects, body) in NOTICES.items():
        for subject in subjects:
            for form in ("plain-8bit", "html-qp"):
                i += 1
                picked = pick(lang, i)
                everyday(lang, f"notice {subject!r}", subject, fill(body, picked),
                         [(value, slot.lower()) for slot, value in picked.items()
                          if "{" + slot + "}" in body], form)
    for lang, lines in TALKING_LINES.items():
        for line in lines:
            for form in ("plain-8bit", "html-qp"):
                i += 1
                picked = pick(lang, i)
                tokens = [(value, slot.lower()) for slot, value in picked.items()
                          if "{" + slot + "}" in line
                          and not (re.search(r"\d", value) and re.search(r"[A-Za-z]", value))]
                tokens += [(time, "time") for time in ("14h35", "21h30", "14h35min", "21h30min",
                                                       "16h50min", "24-hour", "2-night", "10-min",
                                                       "30-minute", "24hrs") if time in line]
                everyday(lang, f"talking line {line!r}", "Acme travel update",
                         "Hello,\n\n" + fill(line, picked) + "\n\nThanks, Acme", tokens, form)
    cell = ('<tr><td style="padding:8px 16px;font-family:Arial,sans-serif;font-size:14px;'
            'color:#333333;line-height:20px">{}</td></tr>\n')
    for lang, (leg1, leg2, filler, footer) in ITINERARY.items():
        f1, f2 = ITINERARY_FLIGHTS[lang]
        legs = [leg1.replace("{F1}", f1), leg2.replace("{F2}", f2)]
        for rows, cte in ((40, "qp"), (140, "qp"), (140, "8bit")):
            markup = ("<html><body><table>" + "".join(cell.format(htmlmod.escape(leg))
                                                      for leg in legs)
                      + cell.format(filler) * rows
                      + cell.format(footer.replace("{REF}", "HMX4K2")) + "</table></body></html>")
            mails.append({"lang": lang, "kind": "ordinary",
                          "why": f"{lang} itinerary of {len(markup.encode())} bytes | {cte}",
                          "subject": "Acme Air", "plain": None, "html": markup, "cte": cte,
                          "charset": "utf-8", "encoding": None, "index": html_text(markup),
                          "tokens": [(f1, "flight"), (f2, "flight")]})
        if lang in TICKET_REST:
            # Its flights written as a ticket does and with a space, "BA 2617", whose digits were
            # hidden once the rest past what 0.5.0 read talked of signing in.
            for spaced in (False, True):
                flights = [f"{f[:2]} {f[2:]}" if spaced else f for f in (f1, f2)]
                text = ("\n".join(leg.replace(f, flight) for leg, f, flight
                                  in zip(legs, (f1, f2), flights))
                        + "\nBooking reference HMX4K2. Seat 14C. Booking 48213377.\n"
                        + (filler + "\n") * (22_000 // len(filler)) + TICKET_REST[lang])
                mails.append({"lang": lang, "kind": "ordinary",
                              "why": f"{lang} e-ticket of {len(text)} characters, its rest talks"
                                     f"{', spaced' if spaced else ''}",
                              "subject": "Your Acme e-ticket", "plain": text, "html": None,
                              "cte": "8bit", "charset": "utf-8", "encoding": None, "index": text,
                              "tokens": [*((flight, "flight") for flight in flights),
                                         ("HMX4K2", "reference"), ("48213377", "order")]})
    for lang, top in JOINED_TOPS.items():
        for n, between in enumerate((f"\n{LINK}\n\n", "\n" * 170) * 3):
            i += 1
            picked = pick(lang, i)
            everyday(lang, f"joined reach {between[:5]!r}", "Your Acme order",
                     top + between + fill(JOINED_TEXTS[lang], picked),
                     [(picked[slot], slot.lower()) for slot in ("ORDER", "DATE", "PHONE")],
                     ("plain-8bit", "html-qp")[n // 2 % 2])
    for lang, label in ORDER_LABELS.items():
        for order in ("#5928374", "#A1B2C3", "(5928374)", "5928374."):
            for form in ("plain-8bit", "html-qp"):
                everyday(lang, f"own line {order}", "Your Acme order",
                         f"{thanks[lang]}\n{label}\n{order}\n", [(order, "order")], form)
    for form in ("plain-8bit", "html-qp"):
        everyday("pt", "a queue's senha", "Sua senha de atendimento",
                 "Protocolo 4829137. Clínica (11) 3456-7890.",
                 [("4829137", "order"), ("(11) 3456-7890", "phone")], form)
    for n, (subject, body) in enumerate(EVERYDAY["en"]):
        for t, footer in enumerate(WRAPPED_TALK):
            i += 1
            picked = pick("en", i)
            everyday("en", f"wrapped talk {footer.split(chr(10))[0]!r}", fill(subject, picked),
                     fill(body, picked) + "\n" + footer,
                     [(value, slot.lower()) for slot, value in picked.items()
                      if "{" + slot + "}" in body], ("plain-8bit", "html-qp")[(n + t) % 2])
    return mails


# ---- how each version reads a mail ----------------------------------------------------------
STEMS = mail.terms("Acme")
AUTHOR = "Acme <account@acme.example>"


def thunderbird_read(m: dict) -> str:
    """This version from Thunderbird's index, as mail._rows and mail._note hide it: the first
    SCAN_CHARS of the text, as 0.5.0 read it, and nothing past them."""
    index = m["index"]
    subject, body = mail._secret_free(m["subject"], index[:mail.SCAN_CHARS], False,
                                      len(index) > mail.SCAN_CHARS)
    return mail._shown(subject, mail.SUBJECT_CHARS) + "\n" + mail._shown(body, mail.BODY_CHARS)


def server_talks(m: dict) -> bool:
    """Whether a mail server's BODY search for goamail.TALK_TERMS finds any of them in a mail's
    parts, matching whole words only, as Gmail is taken to (see goamail._talking)."""
    parts = "\n".join(part for part in (m["plain"], m["html"]) if part is not None)
    return any(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", parts, re.I)
               for term in goamail.TALK_TERMS)


def _encoded(text: str, charset: str | None, encoding: str | None, cte: str, token: str):
    """(the Content-Type's parameters and the transfer encoding's header, the part's bytes). A
    charset of None is a part that names none, in encoding."""
    try:
        data = text.encode(encoding or charset)
    except UnicodeEncodeError:
        charset, data = charset and "utf-8", text.encode("utf-8")
    if cte == "qp":     # with a soft line break inside the token, as a server folding at 76 might
        body = quopri.encodestring(data)
        raw = token.encode(encoding or charset, "replace")
        if len(raw) > 3 and raw in body:
            body = body.replace(raw, raw[:3] + b"=\n" + raw[3:], 1)
        body = body.replace(b"\n", b"\r\n")
    elif cte == "base64":
        body = base64.encodebytes(data).replace(b"\n", b"\r\n")
    else:
        body = data.replace(b"\n", b"\r\n")
    name = "quoted-printable" if cte == "qp" else cte
    label = f"; charset={charset}" if charset else ""
    return f"{label}\r\nContent-Transfer-Encoding: {name}\r\n", body


def fetched(m: dict) -> dict:
    """The mail as an IMAP FETCH gives it to goamail: header fields and text."""
    subject = m["subject"] if m["subject"].isascii() else \
        email.header.Header(m["subject"], "utf-8").encode()
    line = f"Subject: {subject}\r\n".encode()
    if m.get("subject_raw"):    # unencoded 8-bit bytes, as many senders write a subject
        line = b"Subject: " + m["subject"].encode(m["subject_raw"]) + b"\r\n"
    if m.get("subject_split"):  # encoded words split every few bytes, through a character too
        kind, width = m["subject_split"]
        data = m["subject"].encode()
        line = b"Subject: " + b"\r\n ".join(
            (b"=?UTF-8?B?" + base64.b64encode(data[at:at + width]) if kind == "b" else
             b"=?utf-8?q?" + "".join(chr(byte) if byte < 128 and chr(byte).isalnum() else
                                     "_" if byte == 32 else f"={byte:02X}"
                                     for byte in data[at:at + width]).encode()) + b"?="
            for at in range(0, len(data), width)) + b"\r\n"
    head = f"From: {AUTHOR}\r\n".encode() + line + b"MIME-Version: 1.0\r\n"
    parts = [(kind, *_encoded(text, m["charset"], m.get("encoding"), m["cte"], m["tokens"][0][0]))
             for text, kind in ((m["plain"], "plain"), (m["html"], "html")) if text is not None]
    if len(parts) == 1:
        kind, fields, body = parts[0]
        return {b"HEADER": head + f"Content-Type: text/{kind}{fields}\r\n".encode(),
                b"TEXT": body}
    text = b"".join(b"--b\r\n" + f"Content-Type: text/{kind}{fields}\r\n".encode() + body
                    + b"\r\n" for kind, fields, body in parts)
    return {b"HEADER": head + b'Content-Type: multipart/alternative; boundary="b"\r\n\r\n',
            b"TEXT": text + b"--b--\r\n"}


def online_read(m: dict) -> tuple[str | None, str | None]:
    """(what the model is shown through Online Accounts, the text goamail decoded), as goamail
    reads a fetched mail itself, fetching on and all (see imap_fake.read_by_goamail), and
    mail._note hides it. Where goamail asks the server about sign-in talk, it finds it where
    server_talks says so."""
    whole = fetched(m)
    kept = imap_fake.read_by_goamail(whole[b"HEADER"], whole[b"TEXT"], server_talks(m))
    decoded = imap_fake.decoded_by_goamail(whole[b"HEADER"], whole[b"TEXT"])
    if not kept or not decoded:
        return None, None
    return (mail._shown(kept[1], mail.SUBJECT_CHARS) + "\n" + mail._shown(kept[2], mail.BODY_CHARS),
            decoded[0] + "\n" + decoded[1].replace(mail.JOIN, ""))


# ---- what is shown of a token ---------------------------------------------------------------
def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return "".join(str(unicodedata.digit(c)) if unicodedata.category(c) == "Nd" else c
                   for c in text)


def _chars(token: str) -> list[str]:
    return [c for c in _plain(token) if c.isalnum()]


def _edge(c: str, before: bool) -> str:
    if c.isascii() and c.isalnum():
        kind = "0-9" if c.isdigit() else "A-Za-z0-9"
        return f"(?<![{kind}])" if before else f"(?![{kind}])"
    return ""


def count(token, read: str | None) -> int:
    """How many times the token's letters and digits stand in what the model reads, in order with
    at most three marks between any two, and nothing of their kind either side (see
    test_mail_differential._stands), in any script."""
    cs = token if isinstance(token, list) else _chars(token)
    if not cs or read is None:
        return 0
    pattern = _edge(cs[0], True) + r"[^\w\n]{0,3}".join(map(re.escape, cs)) + _edge(cs[-1], False)
    return len(re.findall(pattern, _plain(read)))


def part_shown(token: str, read: str | None) -> bool:
    """Whether a run of at least three of its characters, or half if that is more, stands."""
    cs = _chars(token)
    least = max(3, (len(cs) + 1) // 2)
    return read is not None and any(count(cs[i:j], read) for i in range(len(cs))
                                    for j in range(i + least, len(cs)))


class FiveLanguages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.read = []
        for m in corpus():
            online, decoded = online_read(m)
            cls.read.append((m, old_read(m["subject"], m["index"][:mail.SCAN_CHARS]),
                             thunderbird_read(m), online, decoded))

    def report(self, found: dict, what: str):
        if found:
            self.fail("\n".join(f"{key}, {len(seen)} {what}:\n  " + "\n  ".join(seen[:12])
                                for key, seen in sorted(found.items())))

    def test_there_are_enough_mails(self):
        langs = {m["lang"] for m, *_ in self.read}
        self.assertEqual(langs, {"en", "es", "pt", "zh", "ar"})
        self.assertGreater(sum(m["kind"] == "code" for m, *_ in self.read), 400)
        self.assertGreater(sum(m["kind"] != "code" for m, *_ in self.read), 400)

    def test_both_paths_read_every_mail(self):
        dropped = [m["why"] for m, _old, _tb, online, _decoded in self.read if online is None]
        self.assertEqual(dropped, [])

    def test_online_accounts_decodes_what_thunderbird_holds(self):
        """The subject and text goamail decodes are those Thunderbird's index holds, whatever the
        charset, its label or whether the subject is encoded."""
        found = defaultdict(list)
        for m, _old, _tb, _online, decoded in self.read:
            index = " ".join(f"{m['subject']}\n{m['index']}".split())
            cut = len(fetched(m)[b"TEXT"]) > goamail.READ_ON_BYTES \
                or len(m["index"]) > mail.SCAN_CHARS
            if decoded is not None and cut:
                decoded = " ".join(decoded.split()).rsplit(" ", 1)[0]    # where the fetch cut it
                index = index[:len(decoded)]
            if decoded is not None and " ".join(decoded.split()) != index:
                found[m["lang"]].append(f"{m['why']}\n    {index[:90]!r}\n    {decoded[:90]!r}")
        self.report(found, "mails decoded otherwise")

    def test_no_piece_0_5_0_hid_reaches_the_model(self):
        found = defaultdict(list)
        for m, old, *now, _decoded in self.read:
            for piece in old_tokens(m["subject"]) + old_tokens(m["index"]):
                before = count(piece, old)
                for path, seen in zip(("Thunderbird", "Online Accounts"), now):
                    if count(piece, seen) > before or not before and _chars(piece) and \
                            not part_shown(piece, old) and part_shown(piece, seen):
                        found[f"{path}, {m['lang']}"].append(
                            f"{m['why']}: {piece!r}\n    0.5.0: {old!r}\n    now:   {seen!r}")
        self.report(found, "pieces 0.5.0 hid that reach the model")

    def test_no_everyday_number_0_5_0_read_out_is_hidden(self):
        """A date, a time, a price, an order, flight or phone number 0.5.0 read out, in any of
        these everyday mails, with or without a line that talks of signing in, verifying or an
        offer's end; under a subject that names an everyday code or a password that was changed,
        or the order's, with a promo code or a Wi-Fi password at the top of the text; a time on a
        line that talks of signing in; and in a mail longer than what is read, whose rest talks
        of it."""
        found = defaultdict(list)
        for m, old, *now, _decoded in self.read:
            if m["kind"] == "code" or m["why"].startswith("NFD"):  # 0.5.0 couldn't read NFD
                continue
            for token, kind in m["tokens"]:
                before = count(token, old)
                for path, seen in zip(("Thunderbird", "Online Accounts"), now):
                    if before and count(token, seen) < before:
                        found[f"{path}, {m['lang']}, {kind}"].append(
                            f"{m['why']}: {token!r}\n    0.5.0: {old!r}\n    now:   {seen!r}")
        self.report(found, "everyday numbers 0.5.0 read out that are hidden")

    def test_sign_in_codes_real_senders_send_are_hidden(self):
        """Every code and password in a sign-in mail, on both paths, but for the shapes
        docs/ROADMAP.md lists as read out (see KNOWN_MISSES)."""
        found = defaultdict(list)
        for m, _old, *now, _decoded in self.read:
            if m["kind"] != "code" or any(miss in m["why"] for miss in KNOWN_MISSES):
                continue
            token = m["tokens"][0][0]
            for path, seen in zip(("Thunderbird", "Online Accounts"), now):
                if count(token, seen) or part_shown(token, seen):
                    found[f"{path}, {m['lang']}"].append(f"{m['why']}: {seen!r}")
        self.report(found, "sign-in codes or passwords shown")


# Shapes 0.5.0 also read out, which docs/ROADMAP.md lists: letters and digits before the word for
# a code, sign-in wording without a word for a code in a language whose sign-in words the list
# doesn't have, a password that holds the word for one, and through Online Accounts, letters and
# digits two to an inline tag glued to the Chinese word for a code, which this version's own rules
# read as pairs and 0.5.0's as glued to a word.
KNOWN_MISSES = ("'{C} es tu código", "'{C} é seu código", "'{C} 是您的", "'{C} هو رمز",
                "'Usa {C} para iniciar", "'Use {C} para entrar", "'استخدم {C} لتسجيل",
                "G-{C} هو رمز", "'{C} هو كود", "'Senha@123'", "zh pairs in <")


if __name__ == "__main__":
    unittest.main()
