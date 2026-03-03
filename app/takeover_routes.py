import logging

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlmodel.ext.asyncio.session import AsyncSession
from app.database import get_session
from app.auth import get_current_user
from app import crud

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/takeover",
    tags=["Human Takeover"],
    dependencies=[Depends(get_current_user)],  # Protege todas as rotas neste arquivo
)


@router.post("/{bot_id}/{phone_number}/activate", status_code=200)
async def activate_takeover(
    bot_id: int,
    phone_number: str = Path(
        ..., description="O número de telefone do cliente, ex: 55119..."
    ),
    session: AsyncSession = Depends(get_session),
):
    """Ativa o modo de atendimento humano, silenciando o bot para este cliente."""
    success = await crud.set_human_takeover_by_phone(
        session, bot_id, phone_number, active=True
    )
    if not success:
        raise HTTPException(status_code=404, detail="Cliente ou bot não encontrado.")
    logger.info("Human takeover activated: bot_id=%s phone=%s", bot_id, phone_number)
    return {"message": f"Atendimento humano ativado para {phone_number}."}


@router.post("/{bot_id}/{phone_number}/deactivate", status_code=200)
async def deactivate_takeover(
    bot_id: int,
    phone_number: str = Path(
        ..., description="O número de telefone do cliente, ex: 55119..."
    ),
    session: AsyncSession = Depends(get_session),
):
    """Desativa o modo de atendimento humano, devolvendo o controle ao bot."""
    success = await crud.set_human_takeover_by_phone(
        session, bot_id, phone_number, active=False
    )
    if not success:
        raise HTTPException(status_code=404, detail="Cliente ou bot não encontrado.")
    logger.info("Human takeover deactivated: bot_id=%s phone=%s", bot_id, phone_number)
    return {"message": f"Bot reativado para {phone_number}."}
