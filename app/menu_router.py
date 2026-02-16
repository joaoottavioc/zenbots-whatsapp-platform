from fastapi import APIRouter, UploadFile, File, Depends, HTTPException, Form
from sqlmodel import Session, select
from app.database import get_session 
from app.models import Bot
from app.menu_storage import upload_file_to_s3
from sqlmodel.ext.asyncio.session import AsyncSession

router = APIRouter(prefix="/bots", tags=["Menu"])

@router.post("/{bot_id}/menu")
async def upload_menu(
    bot_id: int,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session) # Mudei o Type Hint para AsyncSession
):
    # 1. Validar se o bot existe (ADICIONE AWAIT AQUI 👇)
    bot = await session.get(Bot, bot_id)
    
    if not bot:
        raise HTTPException(status_code=404, detail="Bot não encontrado")

    # 2. Validar tipo de arquivo
    if file.content_type not in ["application/pdf", "image/jpeg", "image/png"]:
        raise HTTPException(status_code=400, detail="Apenas PDF, JPG ou PNG são permitidos.")

    # 3. Upload pro S3 (Mantém igual, pois sua função no storage é síncrona/normal)
    s3_folder = f"menus/bot_{bot_id}"
    public_url = upload_file_to_s3(file, folder=s3_folder)

    # 4. Atualizar o Banco de Dados
    bot.menu_url = public_url
    
    session.add(bot)
    
    # (ADICIONE AWAIT AQUI 👇)
    await session.commit()
    
    # (ADICIONE AWAIT AQUI 👇)
    await session.refresh(bot)

    return {
        "message": "Cardápio enviado com sucesso!",
        "menu_url": public_url,
        "bot_id": bot.id
    }