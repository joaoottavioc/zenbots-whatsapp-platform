"""Dog House SP — Hot dog restaurant menu for simulation tests."""

RESTAURANT_NAME = "Dog House SP"

PRODUCTS = [
    # ── Hot Dogs Clássicos ──
    {
        "name": "Dog Tradicional",
        "price": 18.90,
        "category": "Hot Dogs Clássicos",
        "description": "Salsicha, purê de batata, vinagrete, milho, ervilha, batata palha e ketchup",
        "keywords": "dog, tradicional, clássico, completo",
        "is_available": True,
    },
    {
        "name": "Dog Simples",
        "price": 14.90,
        "category": "Hot Dogs Clássicos",
        "description": "Salsicha com ketchup, mostarda e batata palha no pão",
        "keywords": "dog, simples, básico",
        "is_available": True,
    },
    {
        "name": "Dog Duplo",
        "price": 22.90,
        "category": "Hot Dogs Clássicos",
        "description": "Duas salsichas, purê, vinagrete, milho, ervilha, batata palha e molhos",
        "keywords": "dog, duplo, duas salsichas",
        "is_available": True,
    },
    # ── Hot Dogs Gourmet ──
    {
        "name": "Dog Cheddar Bacon",
        "price": 26.90,
        "category": "Hot Dogs Gourmet",
        "description": "Salsicha artesanal, cheddar cremoso, bacon crocante e cebola caramelizada",
        "keywords": "dog, cheddar, bacon, gourmet",
        "is_available": True,
    },
    {
        "name": "Dog Calabresa",
        "price": 24.90,
        "category": "Hot Dogs Gourmet",
        "description": "Linguiça calabresa fatiada, catupiry, pimentão e cebola",
        "keywords": "dog, calabresa, catupiry",
        "is_available": True,
    },
    {
        "name": "Dog Costela",
        "price": 28.90,
        "category": "Hot Dogs Gourmet",
        "description": "Costela desfiada, molho barbecue, coleslaw e cebola crispy",
        "keywords": "dog, costela, desfiada, bbq",
        "is_available": True,
    },
    {
        "name": "Dog Tex-Mex",
        "price": 27.90,
        "category": "Hot Dogs Gourmet",
        "description": "Salsicha, chili con carne, cheddar, jalapeño e sour cream",
        "keywords": "dog, tex-mex, chili, jalapeño",
        "is_available": True,
    },
    {
        "name": "Dog Frango",
        "price": 25.90,
        "category": "Hot Dogs Gourmet",
        "description": "Frango desfiado, catupiry, milho e batata palha",
        "keywords": "dog, frango, catupiry",
        "is_available": True,
    },
    # ── Hot Dogs Especiais ──
    {
        "name": "Dog Mega",
        "price": 32.90,
        "category": "Hot Dogs Especiais",
        "description": "Pão de 30cm, duas salsichas, todos os complementos e cheddar",
        "keywords": "dog, mega, grande, especial",
        "is_available": True,
    },
    {
        "name": "Dog Vegetariano",
        "price": 22.90,
        "category": "Hot Dogs Especiais",
        "description": "Salsicha de soja, guacamole, vinagrete e batata palha",
        "keywords": "dog, vegetariano, soja",
        "is_available": True,
    },
    # ── Acompanhamentos ──
    {
        "name": "Batata Frita",
        "price": 14.90,
        "category": "Acompanhamentos",
        "description": "Porção de batata frita crocante",
        "keywords": "batata, frita, porção",
        "is_available": True,
    },
    {
        "name": "Nuggets",
        "price": 16.90,
        "category": "Acompanhamentos",
        "description": "10 nuggets de frango crocantes",
        "keywords": "nuggets, frango, crocante",
        "is_available": True,
    },
    # ── Bebidas ──
    {
        "name": "Refrigerante Lata",
        "price": 6.00,
        "category": "Bebidas",
        "description": "Coca-Cola, Guaraná ou Sprite lata 350ml",
        "keywords": "refrigerante, lata, coca",
        "is_available": True,
    },
    {
        "name": "Água Mineral",
        "price": 4.00,
        "category": "Bebidas",
        "description": "Água mineral sem gás 500ml",
        "keywords": "água, mineral",
        "is_available": True,
    },
    # ── Unavailable ──
    {
        "name": "Dog Cheddar Especial",
        "price": 30.90,
        "category": "Hot Dogs Gourmet",
        "description": "Dog cheddar com bacon duplo e cebola crispy",
        "keywords": "dog, cheddar, especial, bacon",
        "is_available": False,
    },
    {
        "name": "Dog Costela Especial",
        "price": 34.90,
        "category": "Hot Dogs Especiais",
        "description": "Dog costela com porção extra de costela desfiada",
        "keywords": "dog, costela, especial",
        "is_available": False,
    },
]

TEST_SCENARIOS = {
    "add_single_msg": "quero um dog cheddar bacon",
    "add_single_expected": [("Dog Cheddar Bacon", 1)],
    "add_multi_msg": "quero 3 dog tradicional e 2 dog calabresa",
    "add_multi_expected": [("Dog Tradicional", 3), ("Dog Calabresa", 2)],
    "unavailable_msg": "quero um dog cheddar especial e um dog frango",
    "unavailable_available": "Dog Frango",
    "unavailable_name": "Dog Cheddar Especial",
    "remove_add_msg": "quero 2 dog duplo",
    "remove_add_expected": ("Dog Duplo", 2),
    "remove_msg": "tira o dog duplo",
    "suggestion_msg": "me fala o que tem",
    "checkout_product_msg": "quero 1 dog tex-mex",
    "checkout_product_expected": ("Dog Tex-Mex", 1),
}
