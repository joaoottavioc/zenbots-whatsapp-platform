# app/email_service.py
import logging
import os
from fastapi_mail import FastMail, MessageSchema, ConnectionConfig, MessageType
from pydantic import EmailStr

logger = logging.getLogger(__name__)

# Configurações lidas do docker-compose / .env
conf = ConnectionConfig(
    MAIL_USERNAME=os.getenv("MAIL_USERNAME", "test"),
    MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", "test"),
    MAIL_FROM=os.getenv("MAIL_FROM", "noreply@zenbots.com.br"),
    MAIL_PORT=int(os.getenv("MAIL_PORT", 1025)),
    MAIL_SERVER=os.getenv("MAIL_SERVER", "mailhog"),
    MAIL_STARTTLS=os.getenv("MAIL_STARTTLS", "False") == "True",
    MAIL_SSL_TLS=os.getenv("MAIL_SSL_TLS", "False") == "True",
    USE_CREDENTIALS=os.getenv("USE_CREDENTIALS", "False") == "True",
    VALIDATE_CERTS=os.getenv("VALIDATE_CERTS", "False") == "True",
)


def validate_email_config():
    """Raise RuntimeError if production is using default mailhog config."""
    env = os.getenv("ENVIRONMENT", "development").lower()
    mail_server = os.getenv("MAIL_SERVER", "mailhog").lower()
    if env in ("production", "prod") and mail_server == "mailhog":
        raise RuntimeError(
            "Production environment must have MAIL_SERVER configured "
            "(currently defaults to 'mailhog'). Set MAIL_SERVER env var."
        )


async def send_verification_email(email: EmailStr, token: str):
    """
    Sends an email verification link to a newly registered user.
    """
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    verify_link = f"{frontend_url}/verificar-email?token={token}"

    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                <h2 style="color: #059669;">Bem-vindo ao ZenBots!</h2>
                <p>Olá,</p>
                <p>Obrigado por se cadastrar! Para ativar sua conta, confirme seu e-mail clicando no botão abaixo:</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{verify_link}" style="background-color: #059669; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Verificar Meu E-mail
                    </a>
                </div>
                <p style="font-size: 12px; color: #666;">Este link expira em 24 horas. Se você não criou uma conta no ZenBots, ignore este e-mail.</p>
            </div>
        </body>
    </html>
    """

    message = MessageSchema(
        subject="Confirme seu e-mail - ZenBots AI",
        recipients=[email],
        body=html,
        subtype=MessageType.html,
    )

    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception:
        logger.exception(
            "Failed to send verification email to %s via %s", email, conf.MAIL_SERVER
        )
        raise
    logger.info("Verification email sent to %s via %s", email, conf.MAIL_SERVER)


async def send_password_reset_email(email: EmailStr, token: str):
    """
    Envia o e-mail com o link de recuperação.
    """
    # Link que aponta para o Frontend
    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")
    reset_link = f"{frontend_url}/redefinir-senha?token={token}"

    html = f"""
    <html>
        <body style="font-family: Arial, sans-serif; color: #333;">
            <div style="max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #eee; border-radius: 8px;">
                <h2 style="color: #059669;">Recuperação de Senha</h2>
                <p>Olá,</p>
                <p>Recebemos um pedido para redefinir a senha da sua conta ZenBots.</p>
                <p>Clique no botão abaixo para criar uma nova senha:</p>
                <div style="text-align: center; margin: 30px 0;">
                    <a href="{reset_link}" style="background-color: #059669; color: white; padding: 12px 24px; text-decoration: none; border-radius: 5px; font-weight: bold;">
                        Redefinir Minha Senha
                    </a>
                </div>
                <p style="font-size: 12px; color: #666;">Se você não solicitou isso, apenas ignore este e-mail. O link expira em 15 minutos.</p>
            </div>
        </body>
    </html>
    """

    message = MessageSchema(
        subject="Redefinir sua senha - ZenBots AI",
        recipients=[email],
        body=html,
        subtype=MessageType.html,
    )

    fm = FastMail(conf)
    try:
        await fm.send_message(message)
    except Exception:
        logger.exception(
            "Failed to send password reset email to %s via %s", email, conf.MAIL_SERVER
        )
        raise
    logger.info("Password reset email sent via %s", conf.MAIL_SERVER)
