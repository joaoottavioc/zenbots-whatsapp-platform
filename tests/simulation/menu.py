"""
Fictional restaurant menu for simulation tests.

"Sabor da Serra" — a lanchonete designed to stress-test:
- Similar product names (Mignon vs Mignon ao molho cheddar)
- Compound names (Picanha com bacon)
- Typo-prone names (catupiry, cheddar, guaraná)
- Unavailable products (em falta flow)
- Multiple categories
"""

RESTAURANT_NAME = "Sabor da Serra"

PRODUCTS = [
    # ── Lanches Premium ──
    {
        "name": "Picanha com bacon",
        "price": 45.00,
        "category": "Lanches Premium",
        "description": "Pão brioche, picanha grelhada, bacon crocante, queijo, salada",
        "keywords": "picanha, bacon, carne, premium",
        "is_available": True,
    },
    {
        "name": "Picanha com catupiry",
        "price": 40.00,
        "category": "Lanches Premium",
        "description": "Pão brioche, picanha grelhada, catupiry, salada",
        "keywords": "picanha, catupiry, carne, premium",
        "is_available": True,
    },
    {
        "name": "Mignon ao molho cheddar",
        "price": 41.00,
        "category": "Lanches Premium",
        "description": "Pão brioche, mignon, molho cheddar, cebola caramelizada",
        "keywords": "mignon, cheddar, molho, premium",
        "is_available": True,
    },
    {
        "name": "Mignon",
        "price": 37.00,
        "category": "Lanches Premium",
        "description": "Pão brioche, mignon grelhado, queijo, salada",
        "keywords": "mignon, carne, premium",
        "is_available": True,
    },
    {
        "name": "Alcatra acebolada",
        "price": 38.00,
        "category": "Lanches Premium",
        "description": "Pão brioche, alcatra, cebola dourada, queijo",
        "keywords": "alcatra, cebola, acebolada, carne",
        "is_available": True,
    },
    # ── Lanches Clássicos ──
    {
        "name": "X-Burger",
        "price": 25.00,
        "category": "Lanches Clássicos",
        "description": "Pão, hambúrguer artesanal, queijo, salada",
        "keywords": "burger, hamburguer, x-burger, classico",
        "is_available": True,
    },
    {
        "name": "X-Salada",
        "price": 27.00,
        "category": "Lanches Clássicos",
        "description": "Pão, hambúrguer, queijo, salada completa, tomate",
        "keywords": "salada, x-salada, hamburguer",
        "is_available": True,
    },
    {
        "name": "X-Tudo",
        "price": 32.00,
        "category": "Lanches Clássicos",
        "description": "Pão, hambúrguer, bacon, ovo, queijo, presunto, salada",
        "keywords": "tudo, x-tudo, completo, hamburguer",
        "is_available": True,
    },
    {
        "name": "Hambúrguer simples",
        "price": 18.00,
        "category": "Lanches Clássicos",
        "description": "Pão, hambúrguer artesanal, ketchup, mostarda",
        "keywords": "hamburguer, simples, basico",
        "is_available": True,
    },
    # ── Porções ──
    {
        "name": "Batata frita",
        "price": 20.00,
        "category": "Porções",
        "description": "Porção de batata frita crocante",
        "keywords": "batata, frita, porção, fritas",
        "is_available": True,
    },
    {
        "name": "Onion rings",
        "price": 22.00,
        "category": "Porções",
        "description": "Anéis de cebola empanados e fritos",
        "keywords": "onion, rings, cebola, porção",
        "is_available": True,
    },
    {
        "name": "Mandioca frita",
        "price": 18.00,
        "category": "Porções",
        "description": "Porção de mandioca frita dourada",
        "keywords": "mandioca, frita, aipim, porção",
        "is_available": True,
    },
    # ── Bebidas ──
    {
        "name": "Coca-Cola 600ml",
        "price": 10.00,
        "category": "Bebidas",
        "description": "Coca-Cola garrafa 600ml",
        "keywords": "coca, cola, coca-cola, refrigerante",
        "is_available": True,
    },
    {
        "name": "Guaraná Antarctica",
        "price": 9.00,
        "category": "Bebidas",
        "description": "Guaraná Antarctica lata 350ml",
        "keywords": "guarana, antarctica, refrigerante",
        "is_available": True,
    },
    {
        "name": "Água mineral",
        "price": 5.00,
        "category": "Bebidas",
        "description": "Água mineral sem gás 500ml",
        "keywords": "agua, mineral, água",
        "is_available": True,
    },
    {
        "name": "Suco de laranja",
        "price": 12.00,
        "category": "Bebidas",
        "description": "Suco de laranja natural 400ml",
        "keywords": "suco, laranja, natural",
        "is_available": True,
    },
    # ── Sobremesas ──
    {
        "name": "Petit gâteau",
        "price": 28.00,
        "category": "Sobremesas",
        "description": "Bolinho de chocolate com sorvete de creme",
        "keywords": "petit, gateau, chocolate, sobremesa",
        "is_available": True,
    },
    {
        "name": "Sorvete 2 bolas",
        "price": 15.00,
        "category": "Sobremesas",
        "description": "Sorvete artesanal, escolha 2 sabores",
        "keywords": "sorvete, bola, sobremesa, gelato",
        "is_available": True,
    },
    # ── Short-name products (stress-test 3-char word matching) ──
    {
        "name": "Big Mix",
        "price": 35.00,
        "category": "Lanches Clássicos",
        "description": "Pão, hambúrguer duplo, bacon, ovo, queijo, salada completa",
        "keywords": "big, mix, duplo, completo",
        "is_available": True,
    },
    {
        "name": "Hot Dog",
        "price": 15.00,
        "category": "Lanches Clássicos",
        "description": "Pão de hot dog, salsicha, purê, vinagrete, batata palha",
        "keywords": "hot, dog, cachorro quente, salsicha",
        "is_available": True,
    },
    {
        "name": "Fit Wrap",
        "price": 29.00,
        "category": "Lanches Clássicos",
        "description": "Wrap integral, frango grelhado, rúcula, tomate seco",
        "keywords": "fit, wrap, integral, frango, light",
        "is_available": True,
    },
    # ── Unavailable (em falta) ──
    {
        "name": "X-Egg",
        "price": 28.00,
        "category": "Lanches Clássicos",
        "description": "Pão, hambúrguer, ovo, queijo, salada",
        "keywords": "egg, ovo, x-egg, hamburguer",
        "is_available": False,
    },
    {
        "name": "Mignon com cebola",
        "price": 39.00,
        "category": "Lanches Premium",
        "description": "Pão brioche, mignon, cebola caramelizada",
        "keywords": "mignon, cebola, premium",
        "is_available": False,
    },
]
