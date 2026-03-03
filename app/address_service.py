# app/address_service.py
import httpx
import logging
import re
from typing import Dict, Optional

logger = logging.getLogger(__name__)


async def get_address_from_cep(cep: str) -> Optional[Dict]:
    """
    Busca um endereço a partir de um CEP usando a API ViaCEP.
    Retorna um dicionário com os dados do endereço ou None se não for encontrado.
    """
    # 1. Limpa o CEP, deixando apenas os dígitos
    cleaned_cep = re.sub(r"\D", "", cep)
    if len(cleaned_cep) != 8:
        return None

    # 2. Monta a URL e faz a chamada
    url = f"https://viacep.com.br/ws/{cleaned_cep}/json/"
    logger.debug("CEP lookup attempt: %s", cleaned_cep)
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
    ) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()  # Lança um erro para respostas 4xx ou 5xx
            data = response.json()

            # 3. Verifica se a API retornou um erro (ex: CEP inexistente)
            if data.get("erro"):
                logger.debug("CEP %s not found (API returned erro=true)", cleaned_cep)
                return None

            # 4. Retorna os dados do endereço
            return {
                "cep": data.get("cep"),
                "street": data.get("logradouro"),
                "neighborhood": data.get("bairro"),
                "city": data.get("localidade"),
                "state": data.get("uf"),
            }
        except (httpx.RequestError, httpx.HTTPStatusError, KeyError) as e:
            logger.warning("ViaCEP lookup failed for CEP %s: %s", cleaned_cep, e)
            return None
