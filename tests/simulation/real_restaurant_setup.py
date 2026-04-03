"""
Setup script: creates 5 real restaurant bots via the ZenBots API.

Fetches menus from real São Paulo restaurants (Nakato Sushi, Bullguer,
Black Dog, Pizzaria Macedos, Tubarão do Açaí), creates bots, adds products,
and marks some as unavailable.

Run ONCE before test_real_restaurants.py:
  docker compose exec backend python -m tests.simulation.real_restaurant_setup
"""

import httpx
import asyncio
import sys
import json

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"

# ---------------------------------------------------------------------------
# Real restaurant data (from web search: iFood, Delivery Direto, restaurant sites)
# ---------------------------------------------------------------------------

RESTAURANTS = {
    "sushi": {
        "name": "Nakato Sushi",
        "phone_number_id": "real-test-sushi",
        "products": [
            {"name": "Temaki Grande", "price": 19.99, "category": "Temakis", "description": "Temaki grande de salmão fresco com arroz temperado"},
            {"name": "Combinado de Salmão 40 peças", "price": 84.90, "category": "Combinados", "description": "Hossomaki, hot roll, niguiri, sashimi e uramaki de salmão"},
            {"name": "Combinado Clássico 18 peças", "price": 79.90, "category": "Combinados", "description": "Sashimi, niguiri, hossomaki e uramaki variados"},
            {"name": "Hot Rolls 20 unidades", "price": 29.92, "category": "Hot Rolls", "description": "Enrolado frito com arroz e recheio de salmão com cream cheese"},
            {"name": "Combinado de Uramakis 20 peças", "price": 49.90, "category": "Combinados", "description": "20 peças com skin, salmão e california"},
            {"name": "Sashimi de Salmão", "price": 9.95, "category": "Sashimis", "description": "Sashimi avulso de salmão fresco"},
            {"name": "Sushis Jhou 18 peças", "price": 89.90, "category": "Combinados", "description": "Sushi de salmão com cebolinha e cream cheese"},
            {"name": "Yakisoba Clássico", "price": 29.92, "category": "Pratos Quentes", "description": "Macarrão oriental com legumes, opções de camarão, frango ou carne"},
            {"name": "Gyoza 4 unidades", "price": 15.92, "category": "Entradas", "description": "Pastéis de carne e legumes fritos ou no vapor"},
            {"name": "Harumaki 4 unidades", "price": 15.92, "category": "Entradas", "description": "Rolinhos primavera de queijo"},
            {"name": "Chicken Katsu", "price": 11.92, "category": "Entradas", "description": "Tirinhas de peito de frango empanadas"},
            {"name": "Ceviche", "price": 31.92, "category": "Entradas", "description": "Saint peter, salmão, polvo marinados no limão"},
            {"name": "Coca-Cola", "price": 7.92, "category": "Bebidas", "description": "Coca-Cola lata 350ml"},
            {"name": "Suco de Laranja", "price": 7.92, "category": "Bebidas", "description": "Suco de laranja natural 300ml"},
            {"name": "Petit Gateau", "price": 19.99, "category": "Sobremesas", "description": "Bolinho de chocolate com sorvete"},
        ],
        # Products to mark unavailable (by index)
        "unavailable_indices": [3, 11],  # Hot Rolls, Ceviche
    },
    "burger": {
        "name": "Bullguer",
        "phone_number_id": "real-test-burger",
        "products": [
            {"name": "Standard", "price": 27.00, "category": "Hambúrgueres", "description": "Pão brioche, carne 100% Angus e queijo prato"},
            {"name": "Bullguer", "price": 33.00, "category": "Hambúrgueres", "description": "Pão brioche, carne, queijo prato, picles e molho Bullguer"},
            {"name": "Lumberjack", "price": 39.00, "category": "Hambúrgueres", "description": "Pão brioche, carne, queijo prato, bacon, picles e molho Bullguer"},
            {"name": "Super", "price": 50.00, "category": "Hambúrgueres", "description": "Pão brioche, 2 carnes 100g cada, queijo cheddar, bacon e molho bbq"},
            {"name": "Chicken Hot Honey", "price": 39.00, "category": "Hambúrgueres", "description": "Pão com gergelim, sobrecoxa de frango frita, queijo prato, picles, maionese e mel apimentado"},
            {"name": "Green One", "price": 38.00, "category": "Hambúrgueres", "description": "Pão brioche, hambúrguer de falafel com coentro, coleslaw, ovo e picles"},
            {"name": "Fisherman", "price": 39.00, "category": "Hambúrgueres", "description": "Pão brioche, peixe empanado, queijo prato e molho tártaro"},
            {"name": "Batatas Crinkles", "price": 20.00, "category": "Acompanhamentos", "description": "Batata com páprica e sal, acompanha maionese Bullguer"},
            {"name": "Onion Rings", "price": 21.00, "category": "Acompanhamentos", "description": "7 anéis de cebola empanados e crocantes"},
            {"name": "Cheese Bacon Fries", "price": 30.00, "category": "Acompanhamentos", "description": "Batatas Crinkles com queijo cremoso e bacon frito picado"},
            {"name": "Hotdog", "price": 18.00, "category": "Hot Dogs", "description": "Pão, salsicha e mostarda"},
            {"name": "Bulldog", "price": 28.00, "category": "Hot Dogs", "description": "Pão, salsicha enrolada no bacon e coleslaw"},
            {"name": "Brownie com Sorvete", "price": 25.00, "category": "Sobremesas", "description": "Brownie quente com sorvete e calda de chocolate"},
            {"name": "Milkshake", "price": 22.00, "category": "Bebidas", "description": "Milkshake cremoso 300ml"},
            {"name": "Refrigerante", "price": 9.00, "category": "Bebidas", "description": "Refrigerante lata 350ml"},
        ],
        "unavailable_indices": [4, 6],  # Chicken Hot Honey, Fisherman
    },
    "hotdog": {
        "name": "Black Dog",
        "phone_number_id": "real-test-hotdog",
        "products": [
            {"name": "Pop Médio", "price": 15.00, "category": "Hot Dogs Clássicos", "description": "Pão, 1 salsicha, vinagrete, maionese e purê"},
            {"name": "Super Dog Médio", "price": 23.00, "category": "Hot Dogs Clássicos", "description": "Cheddar, vinagrete, curry, maionese, purê, orégano, batata palha"},
            {"name": "Tradicional Médio", "price": 30.00, "category": "Hot Dogs Clássicos", "description": "Carro chefe! Salsicha, cheddar, milho, vinagrete, batata palha, purê, crosta de parmesão"},
            {"name": "Tradicional Grande", "price": 40.00, "category": "Hot Dogs Clássicos", "description": "2 salsichas com todos os acompanhamentos e crosta de parmesão"},
            {"name": "Maximus Médio", "price": 32.00, "category": "Hot Dogs Gourmet", "description": "Hot dog com pepperoni, marinara mexicana picante, crosta de parmesão"},
            {"name": "Cheddar Gourmet Médio", "price": 27.00, "category": "Hot Dogs Gourmet", "description": "Salsicha viena, cheddar extra cremoso, cebola caramelizada com shoyu"},
            {"name": "Cheddar Bacon Grande", "price": 30.00, "category": "Hot Dogs Gourmet", "description": "2 salsichas, cheddar extra cremoso, bacon, orégano"},
            {"name": "Chicken Dog Grande", "price": 42.00, "category": "Hot Dogs Especiais", "description": "2 tiras de frango empanado, pão baguete com parmesão, cheddar, milho, vinagrete"},
            {"name": "Insano!", "price": 60.00, "category": "Hot Dogs Especiais", "description": "Hambúrguer artesanal 120g + salsicha, bacon, pepperoni, marinara picante"},
            {"name": "Cheese Burger", "price": 30.00, "category": "Burgers", "description": "Blend Angus 120g, queijo derretido, pickles, ketchup, pão brioche"},
            {"name": "Fritas Individual", "price": 13.00, "category": "Acompanhamentos", "description": "Porção de fritas individual"},
            {"name": "Onion Rings", "price": 13.00, "category": "Acompanhamentos", "description": "6 anéis de cebola empanados"},
            {"name": "Nuggets", "price": 13.00, "category": "Acompanhamentos", "description": "4 nuggets de frango crocantes"},
            {"name": "Refrigerante Lata", "price": 9.90, "category": "Bebidas", "description": "Refrigerante lata 350ml"},
            {"name": "Churros", "price": 12.00, "category": "Sobremesas", "description": "Churros com recheio à escolha"},
        ],
        "unavailable_indices": [4, 8],  # Maximus Médio, Insano!
    },
    "pizza": {
        "name": "Pizzaria Macedos",
        "phone_number_id": "real-test-pizza",
        "products": [
            {"name": "Mussarela", "price": 95.00, "category": "Pizzas Tradicionais", "description": "Molho de tomate e mussarela"},
            {"name": "Marguerita", "price": 106.00, "category": "Pizzas Tradicionais", "description": "Molho de tomate, mussarela, fatias de tomate, parmesão, manjericão"},
            {"name": "Calabresa", "price": 106.00, "category": "Pizzas Tradicionais", "description": "Calabresa fatiada, cebola, azeitona e orégano"},
            {"name": "Portuguesa", "price": 108.00, "category": "Pizzas Tradicionais", "description": "Presunto, ovo, cebola, azeitona"},
            {"name": "Quatro Queijos", "price": 120.00, "category": "Pizzas Tradicionais", "description": "Parmesão, gorgonzola, catupiry, mussarela"},
            {"name": "Frango", "price": 118.00, "category": "Pizzas Tradicionais", "description": "Frango desfiado com catupiry"},
            {"name": "Pepperoni", "price": 120.00, "category": "Pizzas Especiais", "description": "Pepperoni fatiado com mussarela"},
            {"name": "Camarão", "price": 215.00, "category": "Pizzas Especiais", "description": "Camarão temperado no alho, azeite, cebolinha e salsa"},
            {"name": "Carne Seca", "price": 119.00, "category": "Pizzas Especiais", "description": "Carne seca desfiada, cebola, mussarela, catupiry"},
            {"name": "Chocolate com Morango", "price": 115.00, "category": "Pizzas Doces", "description": "Chocolate ao leite com morangos frescos"},
            {"name": "Lasanha ao Forno", "price": 103.00, "category": "Massas", "description": "Lasanha com molho de tomate e mussarela"},
            {"name": "Maceditas", "price": 30.00, "category": "Entradas", "description": "Pedacinhos de massa de pizza"},
            {"name": "Coca-Cola Lata", "price": 9.80, "category": "Bebidas", "description": "Coca-Cola lata 350ml"},
            {"name": "Guaraná Antarctica Lata", "price": 9.80, "category": "Bebidas", "description": "Guaraná Antarctica lata 350ml"},
            {"name": "Heineken", "price": 19.00, "category": "Bebidas", "description": "Heineken long neck 330ml"},
        ],
        "unavailable_indices": [7, 10],  # Camarão, Lasanha ao Forno
    },
    "acai": {
        "name": "Tubarão do Açaí",
        "phone_number_id": "real-test-acai",
        "products": [
            {"name": "Açaí na Tigela 200ml", "price": 14.00, "category": "Açaí na Tigela", "description": "Açaí puro na tigela com complementos à escolha"},
            {"name": "Açaí na Tigela 300ml", "price": 21.00, "category": "Açaí na Tigela", "description": "Açaí puro na tigela 300ml"},
            {"name": "Açaí na Tigela 500ml", "price": 26.00, "category": "Açaí na Tigela", "description": "Açaí puro na tigela 500ml"},
            {"name": "Pote 500g", "price": 24.00, "category": "Potes", "description": "Pote de açaí 500g para viagem"},
            {"name": "Pote 1kg", "price": 39.00, "category": "Potes", "description": "Pote de açaí 1,02kg para viagem"},
            {"name": "Creme de Cupuaçu 300ml", "price": 20.00, "category": "Cremes", "description": "Tigela de creme de cupuaçu 300ml"},
            {"name": "Açaí com Cupuaçu 300ml", "price": 22.00, "category": "Casadinhos", "description": "Mix de açaí com cupuaçu 300ml"},
            {"name": "Taça Trufada com Creme de Avelã", "price": 47.00, "category": "Taças Trufadas", "description": "Taça de açaí trufado com creme de avelã premium"},
            {"name": "Suco de Açaí 500ml", "price": 20.00, "category": "Sucos", "description": "Suco de açaí puro 500ml"},
            {"name": "Suco de Açaí com Morango 500ml", "price": 18.90, "category": "Sucos", "description": "Suco de açaí batido com morango 500ml"},
            {"name": "Suco de Laranja 400ml", "price": 14.00, "category": "Sucos", "description": "Suco de laranja natural 400ml"},
            {"name": "Granola", "price": 3.00, "category": "Complementos", "description": "Porção extra de granola 30g"},
            {"name": "Leite Condensado", "price": 3.00, "category": "Complementos", "description": "Porção extra de leite condensado"},
            {"name": "Água Mineral", "price": 4.00, "category": "Bebidas", "description": "Água mineral sem gás"},
            {"name": "Coca-Cola", "price": 8.00, "category": "Bebidas", "description": "Coca-Cola lata"},
        ],
        "unavailable_indices": [5, 7],  # Creme de Cupuaçu, Taça Trufada
    },
}

# ---------------------------------------------------------------------------
# Test scenarios per restaurant (uses real product names)
# ---------------------------------------------------------------------------

TEST_SCENARIOS = {
    "sushi": {
        "add_single_msg": "quero um temaki grande",
        "add_single_expected": [("Temaki Grande", 1)],
        "add_multi_msg": "quero 2 gyoza e 1 yakisoba clássico",
        "add_multi_expected": [("Gyoza", 2), ("Yakisoba", 1)],
        "unavailable_msg": "quero um ceviche e uma harumaki",
        "unavailable_available": "Harumaki",
        "unavailable_name": "Ceviche",
        "remove_add_msg": "quero 2 chicken katsu",
        "remove_add_expected": ("Chicken Katsu", 2),
        "remove_msg": "tira o chicken katsu",
        "suggestion_msg": "o que tem de bom?",
        "checkout_product_msg": "quero 1 sashimi de salmão",
        "checkout_product_expected": ("Sashimi", 1),
    },
    "burger": {
        "add_single_msg": "quero um lumberjack",
        "add_single_expected": [("Lumberjack", 1)],
        "add_multi_msg": "quero 2 standard e 1 onion rings",
        "add_multi_expected": [("Standard", 2), ("Onion Rings", 1)],
        "unavailable_msg": "quero um chicken hot honey e um bullguer",
        "unavailable_available": "Bullguer",
        "unavailable_name": "Chicken Hot Honey",
        "remove_add_msg": "quero 3 batatas crinkles",
        "remove_add_expected": ("Batatas Crinkles", 3),
        "remove_msg": "tira as batatas crinkles",
        "suggestion_msg": "o que vocês tem?",
        "checkout_product_msg": "quero 1 super",
        "checkout_product_expected": ("Super", 1),
    },
    "hotdog": {
        "add_single_msg": "quero um tradicional médio",
        "add_single_expected": [("Tradicional Médio", 1)],
        "add_multi_msg": "quero 2 pop médio e 1 fritas individual",
        "add_multi_expected": [("Pop", 2), ("Fritas", 1)],
        "unavailable_msg": "quero um maximus médio e um super dog médio",
        "unavailable_available": "Super Dog",
        "unavailable_name": "Maximus",
        "remove_add_msg": "quero 2 cheddar bacon grande",
        "remove_add_expected": ("Cheddar Bacon Grande", 2),
        "remove_msg": "tira o cheddar bacon",
        "suggestion_msg": "me fala o que tem",
        "checkout_product_msg": "quero 1 cheddar gourmet médio",
        "checkout_product_expected": ("Cheddar Gourmet", 1),
    },
    "pizza": {
        "add_single_msg": "quero uma calabresa",
        "add_single_expected": [("Calabresa", 1)],
        "add_multi_msg": "quero 2 marguerita e 1 guaraná",
        "add_multi_expected": [("Marguerita", 2), ("Guaraná", 1)],
        "unavailable_msg": "quero uma pizza de camarão e uma portuguesa",
        "unavailable_available": "Portuguesa",
        "unavailable_name": "Camarão",
        "remove_add_msg": "quero 2 quatro queijos",
        "remove_add_expected": ("Quatro Queijos", 2),
        "remove_msg": "tira a quatro queijos",
        "suggestion_msg": "quais sabores tem?",
        "checkout_product_msg": "quero 1 pepperoni",
        "checkout_product_expected": ("Pepperoni", 1),
    },
    "acai": {
        "add_single_msg": "quero um açaí na tigela 300ml",
        "add_single_expected": [("Açaí na Tigela 300ml", 1)],
        "add_multi_msg": "quero 2 suco de açaí 500ml e 1 granola",
        "add_multi_expected": [("Suco de Açaí", 2), ("Granola", 1)],
        "unavailable_msg": "quero uma taça trufada com creme de avelã e um açaí na tigela 200ml",
        "unavailable_available": "Açaí na Tigela 200ml",
        "unavailable_name": "Taça Trufada",
        "remove_add_msg": "quero 2 pote 500g",
        "remove_add_expected": ("Pote 500g", 2),
        "remove_msg": "tira o pote 500g",
        "suggestion_msg": "o que tem pra pedir?",
        "checkout_product_msg": "quero 1 açaí na tigela 500ml",
        "checkout_product_expected": ("Açaí na Tigela 500ml", 1),
    },
}


async def setup():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
        # --- Login ---
        print("Logging in...")
        resp = await client.post(
            "/auth/token",
            data={"username": EMAIL, "password": PASSWORD},
        )
        if resp.status_code != 200:
            print(f"Login failed: {resp.status_code} {resp.text}")
            sys.exit(1)

        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        print(f"  Logged in as {EMAIL}")

        created_bots = {}

        for label, rdata in RESTAURANTS.items():
            print(f"\n--- {rdata['name']} ({label}) ---")

            # --- Create bot ---
            bot_resp = await client.post(
                "/bots",
                json={
                    "restaurant_name": rdata["name"],
                    "phone_number_id": rdata["phone_number_id"],
                    "whatsapp_token": "fake-token",
                    "whatsapp_number": f"5511{label}00000",
                },
                headers=headers,
            )
            if bot_resp.status_code == 201:
                bot = bot_resp.json()
                bot_id = bot["id"]
                print(f"  Created bot id={bot_id}")
            elif bot_resp.status_code == 400 and "already exists" in bot_resp.text.lower():
                # Bot already exists — find it
                bots_resp = await client.get("/bots", headers=headers)
                all_bots = bots_resp.json()
                existing = [b for b in all_bots if b.get("restaurant_name") == rdata["name"]]
                if existing:
                    bot_id = existing[0]["id"]
                    print(f"  Bot already exists id={bot_id}, skipping product creation")
                    created_bots[label] = bot_id
                    continue
                else:
                    print(f"  Failed to find existing bot: {bot_resp.text}")
                    continue
            else:
                print(f"  Failed to create bot: {bot_resp.status_code} {bot_resp.text}")
                continue

            created_bots[label] = bot_id

            # --- Add products ---
            product_ids = []
            for i, prod in enumerate(rdata["products"]):
                p_resp = await client.post(
                    f"/bots/{bot_id}/products",
                    json=prod,
                    headers=headers,
                )
                if p_resp.status_code == 201:
                    pid = p_resp.json()["id"]
                    product_ids.append(pid)
                    status = " (will be unavailable)" if i in rdata["unavailable_indices"] else ""
                    print(f"  + {prod['name']} (id={pid}){status}")
                else:
                    print(f"  FAIL {prod['name']}: {p_resp.status_code} {p_resp.text[:100]}")
                    product_ids.append(None)

            # --- Mark unavailable ---
            for idx in rdata["unavailable_indices"]:
                pid = product_ids[idx]
                if pid:
                    u_resp = await client.put(
                        f"/bots/{bot_id}/products/{pid}",
                        json={"is_available": False},
                        headers=headers,
                    )
                    if u_resp.status_code == 200:
                        print(f"  [UNAVAIL] Marked unavailable: {rdata['products'][idx]['name']}")
                    else:
                        print(f"  FAIL mark unavailable: {u_resp.status_code}")

        # --- Create subscription for each bot (needed for bot to work) ---
        # We need to do this directly in DB since there's no admin API
        print("\n--- Creating subscriptions (direct DB) ---")
        print("  Subscriptions must be created via DB. Run the test file which handles this.")

        print("\n=== SETUP COMPLETE ===")
        print("Bots created:", json.dumps(created_bots, indent=2))
        print("\nPhone number IDs for tests:")
        for label, rdata in RESTAURANTS.items():
            print(f"  {label}: {rdata['phone_number_id']}")

        return created_bots


if __name__ == "__main__":
    asyncio.run(setup())
