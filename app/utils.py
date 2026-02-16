import re
import math
import httpx
import os
from fastapi import APIRouter

router = APIRouter(prefix="/utils", tags=["Utils"])

def normalize_phone(number: str) -> str:
    """Remove caracteres não numéricos e o prefixo 55."""
    return re.sub(r"[^\d]", "", number).lstrip("55")

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
        print(f"❌ [ERRO DISTÂNCIA] Falha no cálculo: {e}")
        return 9999.0

async def check_delivery_radius(bot, customer_lat, customer_lon):
    """
    Valida se o cliente está dentro do raio permitido pelo bot.
    Retorna (is_within_radius, distance).
    """
    print(f"🕵️ [CHECK RAIO] Bot: ({bot.latitude}, {bot.longitude}) | Cliente: ({customer_lat}, {customer_lon})")
    
    if not bot.latitude or not bot.longitude:
        print(f"⚠️ [CONFIG] Bot {bot.id} sem coordenadas no banco. Bloqueando entrega.")
        return False, 0.0

    if customer_lat is None or customer_lon is None:
        print(f"⚠️ [GEO] Coordenadas do cliente ausentes. Bloqueando entrega.")
        return False, 9999.0

    distance = calculate_distance(bot.latitude, bot.longitude, customer_lat, customer_lon)
    max_radius = float(getattr(bot, "max_delivery_radius", 10.0))
    
    is_ok = distance <= max_radius
    status_icon = "✅ OK" if is_ok else "❌ FORA DO RAIO"
    print(f"📍 [RESULTADO] Distância: {distance:.2f}km | Limite: {max_radius}km | Status: {status_icon}")
    
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
            print(f"📡 [API] Coordenadas via: BRASIL API")
            return float(lat), float(lng)

    # 2. TENTATIVA: GOOGLE MAPS (Se a BrasilAPI não trouxe lat/lng)
    google_key = os.getenv("GOOGLE_MAPS_API_KEY")
    print("GOOGLE KEY IS:" + str(google_key))
    
    if google_key:
        try:
            print(f"🚀 [API] Chamando GOOGLE MAPS para o CEP: {zipcode}...")
            g_url = "https://maps.googleapis.com/maps/api/geocode/json"
            # Buscamos pelo CEP + Endereço retornado para garantir precisão máxima
            address_query = f"{zipcode}, {street}, {city}, {state}, Brazil"
            params = {"address": address_query, "key": google_key}
            resp = await client.get(g_url, params=params, timeout=5.0)
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "OK":
                    loc = data["results"][0]["geometry"]["location"]
                    print(f"💎 [GOOGLE] Sucesso: ({loc['lat']}, {loc['lng']})")
                    return float(loc['lat']), float(loc['lng'])
                else:
                    print(f"⚠️ [GOOGLE] Status: {data.get('status')} | Motivo: {data.get('error_message', 'N/A')}")
        except Exception as e:
            print(f"❌ [GOOGLE] Erro: {e}")
    else:
        print("🚨 [AMBIENTE] GOOGLE_MAPS_API_KEY não configurada no .env!")

    # 3. TENTATIVA: NOMINATIM (Fallback final por texto)
    try:
        print(f"🐢 [API] Chamando NOMINATIM para: {street}, {city}...")
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
            print(f"✅ [NOMINATIM] Sucesso: ({data['lat']}, {data['lon']})")
            return float(data['lat']), float(data['lon'])
    except Exception as e:
        print(f"❌ [NOMINATIM] Erro: {e}")

    print(f"🚨 [GEO FAIL] Falha total para o CEP {zipcode}")
    return None, None

async def get_address_from_cep(cep: str):
    """Busca endereço completo e garante a tentativa de lat/lng."""
    clean_cep = re.sub(r'\D', '', cep)
    if len(clean_cep) != 8: 
        print(f"❌ [CEP] Formato inválido: {cep}")
        return None

    async with httpx.AsyncClient() as client:
        try:
            print(f"🔎 [BUSCA] CEP: {clean_cep}")
            resp = await client.get(f"https://brasilapi.com.br/api/cep/v2/{clean_cep}", timeout=10.0)
            
            if resp.status_code != 200:
                print(f"❌ [BRASIL API] CEP não encontrado.")
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
            print(f"❌ [FATAL] Erro em get_address_from_cep: {e}")
            return None

@router.get("/lookup-cep/{cep}")
async def lookup_cep_endpoint(cep: str):
    """
    Endpoint público para consultar CEP via Frontend.
    """
    result = await get_address_from_cep(cep)
    
    if not result:
        raise HTTPException(status_code=404, detail="CEP não encontrado ou inválido.")
        
    return result