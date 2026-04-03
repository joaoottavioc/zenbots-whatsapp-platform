"""
Setup script v2: creates 5 MORE real restaurant bots via the ZenBots API.

Pastelaria Yokoyama, Marmitaria Express, Santiago Padaria Artesanal,
Tapiocaria Tapilicia, Espetinhos.com — all from São Paulo.

Run ONCE:
  python -m tests.simulation.real_restaurant_setup_v2
"""

import httpx
import asyncio
import sys
import json

BASE_URL = "http://localhost:8000"
EMAIL = "test1@exemplo.com"
PASSWORD = "Test123$"

RESTAURANTS = {
    "pastel": {
        "name": "Pastelaria Yokoyama",
        "phone_number_id": "real-test-pastel",
        "products": [
            {"name": "Pastel de Carne", "price": 15.50, "category": "Pastéis Simples", "description": "Pastel frito com recheio de carne moída temperada"},
            {"name": "Pastel de Queijo Branco", "price": 15.50, "category": "Pastéis Simples", "description": "Pastel frito com queijo branco temperado"},
            {"name": "Pastel de Palmito", "price": 15.50, "category": "Pastéis Simples", "description": "Pastel frito com palmito"},
            {"name": "Pastel de Catupiry", "price": 18.50, "category": "Pastéis Diversos", "description": "Recheio de catupiry original"},
            {"name": "Pastel de Frango", "price": 18.50, "category": "Pastéis Diversos", "description": "Recheio de frango desfiado"},
            {"name": "Pastel de Pizza", "price": 18.50, "category": "Pastéis Diversos", "description": "Recheio estilo pizza com muçarela e tomate"},
            {"name": "Pastel de Calabresa", "price": 18.50, "category": "Pastéis Diversos", "description": "Recheio de calabresa fatiada"},
            {"name": "Pastel de Carne Seca", "price": 18.50, "category": "Pastéis Diversos", "description": "Recheio de carne seca desfiada"},
            {"name": "Pastel de Bacalhau", "price": 31.00, "category": "Pastéis Especiais", "description": "Bacalhau desfiado premium"},
            {"name": "Pastel de Frango com Catupiry", "price": 29.50, "category": "Pastéis Especiais", "description": "Frango desfiado com catupiry original"},
            {"name": "Coxinha", "price": 18.50, "category": "Salgados", "description": "Coxinha de frango cremosa"},
            {"name": "Kibe", "price": 16.50, "category": "Salgados", "description": "Kibe frito tradicional"},
            {"name": "Pastel de Banana com Chocolate", "price": 16.50, "category": "Pastéis Doces", "description": "Banana com chocolate derretido"},
            {"name": "Caldo de Cana 500ml", "price": 16.50, "category": "Bebidas", "description": "Caldo de cana natural 500ml"},
            {"name": "Suco de Laranja 500ml", "price": 16.50, "category": "Bebidas", "description": "Suco de laranja natural 500ml"},
        ],
        "unavailable_indices": [8, 3],  # Pastel de Bacalhau, Pastel de Catupiry
    },
    "marmita": {
        "name": "Marmitaria Express",
        "phone_number_id": "real-test-marmita",
        "products": [
            {"name": "Mini Frango à Parmegiana", "price": 29.69, "category": "Parmegiana Individual", "description": "Frango empanado, molho de tomate, queijo. Arroz e fritas"},
            {"name": "Mini Berinjela à Parmegiana", "price": 26.99, "category": "Parmegiana Individual", "description": "Berinjela empanada, molho de tomate, queijo, arroz e fritas"},
            {"name": "Mini Filé Mignon à Parmegiana", "price": 62.99, "category": "Parmegiana Individual", "description": "Filé mignon empanado, molho de tomate, arroz e fritas"},
            {"name": "Frango Parmegiana Executivo", "price": 62.99, "category": "Pratos Executivos", "description": "Peito de frango empanado e frito, arroz branco e fritas. Serve 2"},
            {"name": "Strogonoff de Frango", "price": 62.99, "category": "Pratos para 2", "description": "Strogonoff com arroz e fritas. Serve 2 pessoas"},
            {"name": "Costela Suína ao Barbecue", "price": 71.99, "category": "Pratos para 2", "description": "Costela com molho barbecue, arroz branco e fritas. Serve 2"},
            {"name": "Picanha Fatiada", "price": 152.99, "category": "Pratos para 2", "description": "Picanha fatiada com arroz branco e fritas. Serve 2"},
            {"name": "Filé de Peixe Santista", "price": 71.99, "category": "Pratos para 2", "description": "Pescada branca grelhada, arroz, purê, banana à milanesa. Serve 2"},
            {"name": "Feijoada Pequena", "price": 62.99, "category": "Feijoada", "description": "Feijoada completa, arroz, torresmo e couve refogada"},
            {"name": "Virado Individual", "price": 53.99, "category": "Virado à Paulista", "description": "Tutu de feijão, calabresa, couve refogada, bisteca suína"},
            {"name": "Salada Grande Completa", "price": 41.99, "category": "Saladas", "description": "Alface, tomate, palmito, cenoura, batata, presunto e cebola"},
            {"name": "Banana Split", "price": 21.59, "category": "Sobremesas", "description": "Banana com 3 bolas de sorvete, chantili e calda"},
            {"name": "Coca-Cola 350ml", "price": 8.55, "category": "Bebidas", "description": "Coca-Cola Original lata 350ml"},
            {"name": "Guaraná Antarctica 350ml", "price": 6.75, "category": "Bebidas", "description": "Guaraná Antarctica lata 350ml"},
            {"name": "Suco de Laranja 300ml", "price": 8.55, "category": "Bebidas", "description": "Suco de laranja natural 300ml"},
        ],
        "unavailable_indices": [2, 7],  # Mini Filé Mignon, Filé de Peixe Santista
    },
    "padaria": {
        "name": "Santiago Padaria Artesanal",
        "phone_number_id": "real-test-padaria",
        "products": [
            {"name": "Pão de Queijo", "price": 14.30, "category": "Clássicos", "description": "Feito com queijo da serra da canastra"},
            {"name": "Croissant", "price": 16.50, "category": "Clássicos", "description": "Massa amanteigada tradicional"},
            {"name": "Croissant Canastra", "price": 24.20, "category": "Clássicos", "description": "Com queijo da serra da canastra"},
            {"name": "Focaccia", "price": 22.55, "category": "Clássicos", "description": "Focaccia de fermentação natural"},
            {"name": "Pão na Chapa com Ovo Mexido", "price": 27.50, "category": "Clássicos", "description": "2 fatias com porção de ovo caipira mexido"},
            {"name": "Misto Quente", "price": 42.90, "category": "Sanduíches", "description": "Queijo e presunto artesanal no pão de fermentação natural"},
            {"name": "Steak Cheese", "price": 58.30, "category": "Sanduíches", "description": "Carne bovina em tiras, muçarela, rúcula e tomate"},
            {"name": "Queijo Quente", "price": 37.40, "category": "Sanduíches", "description": "Mix de queijos no pão artesanal"},
            {"name": "Cookie", "price": 16.50, "category": "Doces", "description": "Cookie com gotas de chocolate ao leite"},
            {"name": "Cheesecake", "price": 25.30, "category": "Doces", "description": "Fatia de cheesecake artesanal"},
            {"name": "Pudim de Baunilha", "price": 20.90, "category": "Doces", "description": "Pudim de fava de baunilha"},
            {"name": "Cappuccino", "price": 14.30, "category": "Bebidas", "description": "Cappuccino com chocolate e canela"},
            {"name": "Chocolate Quente", "price": 19.80, "category": "Bebidas", "description": "Chocolate artesanal quente"},
            {"name": "Limonada Santiago 500ml", "price": 24.20, "category": "Bebidas", "description": "Limonada especial da casa 500ml"},
            {"name": "Café Coado", "price": 11.00, "category": "Bebidas", "description": "Café coado tradicional"},
        ],
        "unavailable_indices": [3, 6],  # Focaccia, Steak Cheese
    },
    "tapioca": {
        "name": "Tapiocaria Tapilícia",
        "phone_number_id": "real-test-tapioca",
        "products": [
            {"name": "Tap Mec", "price": 28.00, "category": "Tapiocas Salgadas", "description": "Hambúrguer artesanal, picles, queijo, alface e massa de tapioca com gergelim"},
            {"name": "Tap Carne Seca com Queijo", "price": 29.00, "category": "Tapiocas Salgadas", "description": "Tapioca com carne seca desfiada e queijo"},
            {"name": "Tap Omelete", "price": 24.00, "category": "Tapiocas Salgadas", "description": "Ovo, queijo, presunto e tomate"},
            {"name": "Tap Pizza", "price": 22.00, "category": "Tapiocas Salgadas", "description": "Queijo, presunto, molho de pizza, tomate e orégano"},
            {"name": "Tap Fitness", "price": 22.00, "category": "Tapiocas Salgadas", "description": "Ovo cozido, queijo, orégano, tomate e massa de tapioca com aveia"},
            {"name": "Tap Frango com Catupiry", "price": 22.00, "category": "Tapiocas Salgadas", "description": "Frango desfiado com catupiry original"},
            {"name": "Tap Muçarela", "price": 20.00, "category": "Tapiocas Salgadas", "description": "Muçarela e orégano"},
            {"name": "Tapioca com Manteiga", "price": 12.00, "category": "Tapiocas Salgadas", "description": "Tapioca simples com manteiga"},
            {"name": "Tap de Nutella com Morango", "price": 24.00, "category": "Tapiocas Doces", "description": "Nutella original com morango fresco"},
            {"name": "Tap de Coco Ralado", "price": 20.00, "category": "Tapiocas Doces", "description": "Com leite condensado e doce de leite"},
            {"name": "Tap de Banana", "price": 18.00, "category": "Tapiocas Doces", "description": "Com leite condensado e canela"},
            {"name": "Coca-Cola 355ml", "price": 7.00, "category": "Bebidas", "description": "Coca-Cola Original 355ml"},
            {"name": "Guaraná Antarctica 350ml", "price": 7.00, "category": "Bebidas", "description": "Guaraná Antarctica 350ml"},
            {"name": "Água com Gás 500ml", "price": 5.00, "category": "Bebidas", "description": "Água mineral com gás 500ml"},
            {"name": "Del Valle Uva 290ml", "price": 7.00, "category": "Bebidas", "description": "Suco Del Valle sabor uva 290ml"},
        ],
        "unavailable_indices": [0, 8],  # Tap Mec, Tap de Nutella com Morango
    },
    "espeto": {
        "name": "Espetinhos.com",
        "phone_number_id": "real-test-espeto",
        "products": [
            {"name": "Espetinho Bovino", "price": 10.90, "category": "Espetinhos", "description": "Espeto de carne bovina grelhada"},
            {"name": "Espetinho de Frango", "price": 10.90, "category": "Espetinhos", "description": "Espeto de frango grelhado"},
            {"name": "Espetinho de Linguiça", "price": 10.90, "category": "Espetinhos", "description": "Espeto de linguiça grelhada"},
            {"name": "Espetinho de Kafta", "price": 10.90, "category": "Espetinhos", "description": "Espeto de kafta temperada grelhada"},
            {"name": "Espetinho de Panceta", "price": 10.90, "category": "Espetinhos", "description": "Espeto de panceta suína grelhada"},
            {"name": "Espetinho de Queijo Coalho", "price": 11.90, "category": "Espetinhos", "description": "Espeto de queijo coalho grelhado"},
            {"name": "Espetinho de Coração", "price": 11.90, "category": "Espetinhos", "description": "Espeto de coração de frango grelhado"},
            {"name": "Espetinho de Picanha 130g", "price": 22.00, "category": "Espetinhos Premium", "description": "Espeto de picanha premium 130g"},
            {"name": "Espetinho de Filé Mignon 130g", "price": 22.00, "category": "Espetinhos Premium", "description": "Espeto de filé mignon premium 130g"},
            {"name": "Porção de Farofa", "price": 10.00, "category": "Acompanhamentos", "description": "Farofa temperada"},
            {"name": "Porção de Vinagrete", "price": 14.00, "category": "Acompanhamentos", "description": "Vinagrete de tomate, cebola e pimentão"},
            {"name": "Espeto Doce", "price": 20.00, "category": "Sobremesas", "description": "Espeto de morango com chocolate"},
            {"name": "Coca-Cola 350ml", "price": 8.00, "category": "Bebidas", "description": "Coca-Cola Original lata 350ml"},
            {"name": "Guaraná 350ml", "price": 8.00, "category": "Bebidas", "description": "Guaraná lata 350ml"},
            {"name": "Cerveja Skol 350ml", "price": 8.00, "category": "Bebidas", "description": "Cerveja Skol lata 350ml"},
        ],
        "unavailable_indices": [4, 8],  # Espetinho de Panceta, Espetinho de Filé Mignon
    },
}

TEST_SCENARIOS = {
    "pastel": {
        "add_single_msg": "quero um pastel de frango",
        "add_single_expected": [("Pastel de Frango", 1)],
        "add_multi_msg": "quero 3 pastel de carne e 2 coxinha",
        "add_multi_expected": [("Pastel de Carne", 3), ("Coxinha", 2)],
        "unavailable_msg": "quero um pastel de bacalhau e um pastel de calabresa",
        "unavailable_available": "Pastel de Calabresa",
        "unavailable_name": "Pastel de Bacalhau",
        "remove_add_msg": "quero 2 kibe",
        "remove_add_expected": ("Kibe", 2),
        "remove_msg": "tira o kibe",
        "suggestion_msg": "o que tem de bom?",
        "checkout_product_msg": "quero 1 pastel de pizza",
        "checkout_product_expected": ("Pastel de Pizza", 1),
    },
    "marmita": {
        "add_single_msg": "quero uma feijoada pequena",
        "add_single_expected": [("Feijoada Pequena", 1)],
        "add_multi_msg": "quero 1 virado individual e 1 coca-cola",
        "add_multi_expected": [("Virado Individual", 1), ("Coca-Cola", 1)],
        "unavailable_msg": "quero um mini filé mignon à parmegiana e um strogonoff de frango",
        "unavailable_available": "Strogonoff",
        "unavailable_name": "Filé Mignon",
        "remove_add_msg": "quero 2 banana split",
        "remove_add_expected": ("Banana Split", 2),
        "remove_msg": "tira o banana split",
        "suggestion_msg": "o que vocês tem?",
        "checkout_product_msg": "quero 1 mini frango à parmegiana",
        "checkout_product_expected": ("Frango", 1),
    },
    "padaria": {
        "add_single_msg": "quero um pão de queijo",
        "add_single_expected": [("Pão de Queijo", 1)],
        "add_multi_msg": "quero 2 croissant e 1 cappuccino",
        "add_multi_expected": [("Croissant", 2), ("Cappuccino", 1)],
        "unavailable_msg": "quero uma focaccia e um misto quente",
        "unavailable_available": "Misto Quente",
        "unavailable_name": "Focaccia",
        "remove_add_msg": "quero 3 cookie",
        "remove_add_expected": ("Cookie", 3),
        "remove_msg": "tira o cookie",
        "suggestion_msg": "me fala o que tem",
        "checkout_product_msg": "quero 1 queijo quente",
        "checkout_product_expected": ("Queijo Quente", 1),
    },
    "tapioca": {
        "add_single_msg": "quero uma tap frango com catupiry",
        "add_single_expected": [("Tap Frango com Catupiry", 1)],
        "add_multi_msg": "quero 2 tap pizza e 1 tap omelete",
        "add_multi_expected": [("Tap Pizza", 2), ("Tap Omelete", 1)],
        "unavailable_msg": "quero uma tap de nutella com morango e uma tap de banana",
        "unavailable_available": "Tap de Banana",
        "unavailable_name": "Tap de Nutella",
        "remove_add_msg": "quero 2 tap muçarela",
        "remove_add_expected": ("Tap Muçarela", 2),
        "remove_msg": "tira a tap muçarela",
        "suggestion_msg": "o que tem pra pedir?",
        "checkout_product_msg": "quero 1 tap carne seca com queijo",
        "checkout_product_expected": ("Tap Carne Seca", 1),
    },
    "espeto": {
        "add_single_msg": "quero um espetinho de kafta",
        "add_single_expected": [("Espetinho de Kafta", 1)],
        "add_multi_msg": "quero 3 espetinho bovino e 2 espetinho de frango",
        "add_multi_expected": [("Espetinho Bovino", 3), ("Espetinho de Frango", 2)],
        "unavailable_msg": "quero um espetinho de panceta e um espetinho de linguiça",
        "unavailable_available": "Espetinho de Linguiça",
        "unavailable_name": "Espetinho de Panceta",
        "remove_add_msg": "quero 2 espetinho de queijo coalho",
        "remove_add_expected": ("Espetinho de Queijo Coalho", 2),
        "remove_msg": "tira o espetinho de queijo coalho",
        "suggestion_msg": "o que vocês tem no cardápio?",
        "checkout_product_msg": "quero 1 espetinho de picanha",
        "checkout_product_expected": ("Espetinho de Picanha", 1),
    },
}


async def setup():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30) as client:
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
            else:
                print(f"  Failed: {bot_resp.status_code} {bot_resp.text[:200]}")
                continue

            created_bots[label] = bot_id

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
                    tag = " (UNAVAIL)" if i in rdata["unavailable_indices"] else ""
                    print(f"  + {prod['name']} (id={pid}){tag}")
                else:
                    print(f"  FAIL {prod['name']}: {p_resp.status_code}")
                    product_ids.append(None)

            for idx in rdata["unavailable_indices"]:
                pid = product_ids[idx]
                if pid:
                    u_resp = await client.put(
                        f"/bots/{bot_id}/products/{pid}",
                        json={"is_available": False},
                        headers=headers,
                    )
                    if u_resp.status_code == 200:
                        print(f"  [UNAVAIL] {rdata['products'][idx]['name']}")

        print("\n=== SETUP COMPLETE ===")
        print("Bots:", json.dumps(created_bots, indent=2))
        return created_bots


if __name__ == "__main__":
    asyncio.run(setup())
