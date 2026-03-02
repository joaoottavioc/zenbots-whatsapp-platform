import logging
import re
import math
import time
import httpx
import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlmodel.ext.asyncio.session import AsyncSession
from app.database import get_session
from app.monitoring import record_api_usage

logger = logging.getLogger(__name__)

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")


def mask_phone(phone: str) -> str:
    """Mask a phone number, keeping first 4 and last 2 digits."""
    digits = re.sub(r"[^\d]", "", phone)
    if len(digits) <= 6:
        return "***"
    return digits[:4] + "***" + digits[-2:]


def mask_email(email: str) -> str:
    """Mask an email address, keeping first 2 chars + domain."""
    if "@" not in email:
        return "***"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return f"**@{domain}"
    return f"{local[:2]}***@{domain}"

router = APIRouter(prefix="/utils", tags=["Utils"])

def normalize_phone(number: str) -> str | None:
    """
    Normalize a Brazilian phone number.
    Returns the 10-11 digit number (DDD + number) or None if invalid.
    """
    digits = re.sub(r"[^\d]", "", number)
    # Strip country code "55" only when the number is long enough (12+ digits)
    if len(digits) >= 12 and digits.startswith("55"):
        digits = digits[2:]
    # Valid Brazilian phone: 10 digits (landline) or 11 digits (mobile), DDD 11-99
    if len(digits) not in (10, 11):
        return None
    ddd = int(digits[:2])
    if ddd < 11 or ddd > 99:
        return None
    return digits

def calculate_distance(lat1, lon1, lat2, lon2):
    """
    Calcula a distância entre dois pontos (Haversine).
    Retorna 9999.0 se as coordenadas forem inválidas para forçar o bloqueio do raio.
    """
    if any(v is None for v in [lat1, lon1, lat2, lon2]):
        return 9999.0
    try:
        lat1, lon1, lat2, lon2 = map(float, [lat1, lon1, lat2, lon2])
        R = 6371 # Raio da Terra em km
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
             math.sin(dlon / 2) ** 2)
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return R * c
    except Exception as e:
        logger.error("Distance calculation failed: %s", e)
        return 9999.0

async def check_delivery_radius(bot, customer_lat, customer_lon):
    """
    Valida se o cliente está dentro do raio permitido pelo bot.
    Retorna (is_within_radius, distance).
    """
    logger.info("Checking delivery radius for bot_id=%s", bot.id)
    
    if not bot.latitude or not bot.longitude:
        logger.warning("Bot %s has no coordinates configured, blocking delivery", bot.id)
        return False, 0.0

    if customer_lat is None or customer_lon is None:
        logger.warning("Customer coordinates missing, blocking delivery")
        return False, 9999.0

    distance = calculate_distance(bot.latitude, bot.longitude, customer_lat, customer_lon)
    max_radius = float(getattr(bot, "max_delivery_radius", 10.0))
    
    is_ok = distance <= max_radius
    if is_ok:
        logger.info("Delivery radius OK: %.2fkm <= %.1fkm limit", distance, max_radius)
    else:
        logger.warning("Outside delivery radius: %.2fkm > %.1fkm limit", distance, max_radius)
    
    return is_ok, distance

async def _fetch_coordinates(client, zipcode, street, city, state, brasilapi_location=None):
    """
    Motor de geolocalização com hierarquia e Fallback real.
    """
    
    # 1. TENTATIVA: BRASIL API (Objeto location da V2)
    if brasilapi_location and isinstance(brasilapi_location, dict):
        coords = brasilapi_location.get('coordinates', {})
        lat = coords.get('latitude')
        lng = coords.get('longitude')
        if lat and lng:
            logger.info("Coordinates resolved via BrasilAPI for cep=%s", zipcode)
            return float(lat), float(lng)

    # 2. TENTATIVA: GOOGLE MAPS (Se a BrasilAPI não trouxe lat/lng)
    google_key = os.getenv("GOOGLE_MAPS_API_KEY")

    if google_key:
        start = time.perf_counter_ns()
        try:
            logger.info("Calling Google Maps geocoding for cep=%s", zipcode)
            g_url = "https://maps.googleapis.com/maps/api/geocode/json"
            address_query = f"{zipcode}, {street}, {city}, {state}, Brazil"
            params = {"address": address_query, "key": google_key}
            resp = await client.get(g_url, params=params, timeout=5.0)
            elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000

            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "OK":
                    loc = data["results"][0]["geometry"]["location"]
                    logger.info("Coordinates resolved via Google Maps for cep=%s", zipcode)
                    await record_api_usage(None, "google_maps", "geocode", cost_usd=0.005, duration_ms=elapsed_ms)
                    return float(loc['lat']), float(loc['lng'])
                else:
                    logger.warning("Google Maps geocoding failed: status=%s", data.get("status"))
                    await record_api_usage(None, "google_maps", "geocode", cost_usd=0.005, duration_ms=elapsed_ms, success=False)
        except Exception as e:
            elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
            await record_api_usage(None, "google_maps", "geocode", cost_usd=0.0, duration_ms=elapsed_ms, success=False)
            logger.error("Google Maps geocoding error: %s", e)
    else:
        logger.warning("GOOGLE_MAPS_API_KEY not configured")

    # 3. TENTATIVA: NOMINATIM (Fallback final por texto)
    try:
        logger.info("Calling Nominatim geocoding for cep=%s", zipcode)
        n_url = "https://nominatim.openstreetmap.org/search"
        headers = {'User-Agent': 'ZenBots-Marketplace/1.0'}
        # Tentamos buscar pela rua e cidade se o CEP falhou
        params = {
            "q": f"{street}, {city}, {state}, Brazil",
            "format": "json",
            "limit": 1
        }
        resp = await client.get(n_url, params=params, headers=headers, timeout=5.0)
        
        if resp.status_code == 200 and resp.json():
            data = resp.json()[0]
            logger.info("Coordinates resolved via Nominatim for cep=%s", zipcode)
            return float(data['lat']), float(data['lon'])
    except Exception as e:
        logger.error("Nominatim geocoding error: %s", e)

    logger.error("All geocoding attempts failed for cep=%s", zipcode)
    return None, None

async def get_address_from_cep(cep: str):
    """Busca endereço completo e garante a tentativa de lat/lng."""
    clean_cep = re.sub(r'\D', '', cep)
    if len(clean_cep) != 8:
        logger.warning("CEP invalid format: %s", cep)
        return None
    if clean_cep == "00000000" or int(clean_cep) < 1_000_000:
        logger.warning("CEP out of valid range: %s", clean_cep)
        return None

    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)) as client:
        try:
            logger.info("Looking up CEP %s", clean_cep)
            resp = await client.get(f"https://brasilapi.com.br/api/cep/v2/{clean_cep}", timeout=10.0)
            
            if resp.status_code != 200:
                logger.warning("BrasilAPI returned status=%d for cep=%s", resp.status_code, clean_cep)
                return None
            
            data = resp.json()
            
            # Agora passamos os dados de rua/cidade para o motor de busca ter mais contexto
            lat, lng = await _fetch_coordinates(
                client, 
                clean_cep, 
                data.get('street', ''), 
                data.get('city', ''), 
                data.get('state', ''), 
                data.get('location')
            )

            return {
                "cep": clean_cep,
                "street": data.get("street"),
                "neighborhood": data.get("neighborhood"),
                "city": data.get("city"),
                "state": data.get("state"),
                "lat": lat,
                "lng": lng
            }
        except Exception as e:
            logger.error("get_address_from_cep failed for cep=%s: %s", clean_cep, e)
            return None

async def _require_authenticated_user(
    token: str = Depends(_oauth2_scheme),
    session: AsyncSession = Depends(get_session),
):
    """Lazy import wrapper to avoid circular dependency (auth → crud → utils → auth)."""
    from app.auth import get_current_user
    return await get_current_user(token=token, session=session)


@router.get("/lookup-cep/{cep}")
async def lookup_cep_endpoint(cep: str, current_user=Depends(_require_authenticated_user)):
    """
    Endpoint público para consultar CEP via Frontend.
    """
    result = await get_address_from_cep(cep)
    
    if not result:
        raise HTTPException(status_code=404, detail="CEP não encontrado ou inválido.")
        
    return result