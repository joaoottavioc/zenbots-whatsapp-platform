"""Smash & Co. Hamburgueria — Burger restaurant menu for simulation tests."""

RESTAURANT_NAME = "Smash & Co. Hamburgueria"

PRODUCTS = [
    # ── Smash Burgers ──
    {
        "name": "Smash Clássico",
        "price": 29.90,
        "category": "Smash Burgers",
        "description": "Dois smash de blend bovino 90g, queijo cheddar, cebola caramelizada e molho da casa no pão brioche",
        "keywords": "smash, clássico, cheddar, cebola",
        "is_available": True,
    },
    {
        "name": "Smash Bacon",
        "price": 33.90,
        "category": "Smash Burgers",
        "description": "Dois smash de blend bovino 90g, bacon crocante, queijo cheddar e maionese defumada",
        "keywords": "smash, bacon, cheddar, defumado",
        "is_available": True,
    },
    {
        "name": "Smash Trufado",
        "price": 37.90,
        "category": "Smash Burgers",
        "description": "Dois smash de blend bovino 90g, queijo brie, rúcula e maionese trufada",
        "keywords": "smash, trufado, brie, rúcula",
        "is_available": True,
    },
    {
        "name": "Smash Costela",
        "price": 36.90,
        "category": "Smash Burgers",
        "description": "Dois smash de blend com costela desfiada, queijo provolone e cebola crispy",
        "keywords": "smash, costela, provolone, crispy",
        "is_available": True,
    },
    # ── Burgers Artesanais ──
    {
        "name": "Texas BBQ",
        "price": 38.90,
        "category": "Burgers Artesanais",
        "description": "Hambúrguer 180g, bacon, cheddar, onion rings e molho barbecue",
        "keywords": "texas, bbq, bacon, barbecue",
        "is_available": True,
    },
    {
        "name": "Burger Pulled Pork",
        "price": 42.90,
        "category": "Burgers Artesanais",
        "description": "Hambúrguer 180g com pulled pork, coleslaw e molho mostarda e mel",
        "keywords": "pulled pork, coleslaw, mostarda",
        "is_available": True,
    },
    {
        "name": "Veggie Burger",
        "price": 31.90,
        "category": "Burgers Artesanais",
        "description": "Hambúrguer de grão-de-bico, queijo muçarela, alface, tomate e maionese verde",
        "keywords": "veggie, vegetariano, grão de bico",
        "is_available": True,
    },
    # ── Acompanhamentos ──
    {
        "name": "Batata Frita",
        "price": 16.90,
        "category": "Acompanhamentos",
        "description": "Porção de batata frita crocante com sal",
        "keywords": "batata, frita, porção",
        "is_available": True,
    },
    {
        "name": "Batata com Cheddar e Bacon",
        "price": 24.90,
        "category": "Acompanhamentos",
        "description": "Batata frita coberta com cheddar cremoso e bacon",
        "keywords": "batata, cheddar, bacon",
        "is_available": True,
    },
    {
        "name": "Onion Rings",
        "price": 19.90,
        "category": "Acompanhamentos",
        "description": "10 anéis de cebola empanados e fritos",
        "keywords": "onion rings, cebola, empanada",
        "is_available": True,
    },
    # ── Bebidas ──
    {
        "name": "Milk-Shake Ovomaltine",
        "price": 18.90,
        "category": "Bebidas",
        "description": "Milk-shake cremoso de Ovomaltine 400ml",
        "keywords": "milkshake, ovomaltine, cremoso",
        "is_available": True,
    },
    {
        "name": "Refrigerante Lata",
        "price": 6.00,
        "category": "Bebidas",
        "description": "Coca-Cola, Guaraná ou Sprite lata 350ml",
        "keywords": "refrigerante, lata, coca, guaraná",
        "is_available": True,
    },
    {
        "name": "Suco Natural",
        "price": 12.90,
        "category": "Bebidas",
        "description": "Suco de laranja, limão ou maracujá 300ml",
        "keywords": "suco, natural, fruta",
        "is_available": True,
    },
    # ── Unavailable ──
    {
        "name": "Smash Duplo Bacon",
        "price": 39.90,
        "category": "Smash Burgers",
        "description": "Quatro smash de blend bovino com bacon duplo e cheddar",
        "keywords": "smash, duplo, bacon",
        "is_available": False,
    },
    {
        "name": "Texas BBQ Especial",
        "price": 44.90,
        "category": "Burgers Artesanais",
        "description": "Texas BBQ com costela desfiada e onion rings extras",
        "keywords": "texas, bbq, especial, costela",
        "is_available": False,
    },
]

TEST_SCENARIOS = {
    "add_single_msg": "quero um smash bacon",
    "add_single_expected": [("Smash Bacon", 1)],
    "add_multi_msg": "quero 2 texas bbq e 1 batata frita",
    "add_multi_expected": [("Texas BBQ", 2), ("Batata Frita", 1)],
    "unavailable_msg": "quero um smash duplo bacon e um smash trufado",
    "unavailable_available": "Smash Trufado",
    "unavailable_name": "Smash Duplo Bacon",
    "remove_add_msg": "quero 2 onion rings",
    "remove_add_expected": ("Onion Rings", 2),
    "remove_msg": "tira as onion rings",
    "suggestion_msg": "o que vocês tem?",
    "checkout_product_msg": "quero 1 smash clássico",
    "checkout_product_expected": ("Smash Clássico", 1),
}
