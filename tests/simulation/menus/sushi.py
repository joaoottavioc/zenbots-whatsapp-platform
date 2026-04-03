"""Sushi Kento — Japanese restaurant menu for simulation tests."""

RESTAURANT_NAME = "Sushi Kento"

PRODUCTS = [
    # ── Temakis ──
    {
        "name": "Temaki Salmão",
        "price": 28.90,
        "category": "Temakis",
        "description": "Temaki de salmão fresco com arroz temperado e cebolinha",
        "keywords": "temaki, salmão, peixe, japonês",
        "is_available": True,
    },
    {
        "name": "Temaki Atum",
        "price": 30.90,
        "category": "Temakis",
        "description": "Temaki de atum fresco com arroz e gergelim",
        "keywords": "temaki, atum, peixe, japonês",
        "is_available": True,
    },
    {
        "name": "Temaki Skin",
        "price": 24.90,
        "category": "Temakis",
        "description": "Temaki de pele de salmão crocante com cream cheese",
        "keywords": "temaki, skin, salmão, crocante",
        "is_available": True,
    },
    {
        "name": "Temaki Camarão Empanado",
        "price": 34.90,
        "category": "Temakis",
        "description": "Temaki de camarão empanado com cream cheese e cebolinha",
        "keywords": "temaki, camarão, empanado",
        "is_available": True,
    },
    {
        "name": "Temaki Philadelphia",
        "price": 29.90,
        "category": "Temakis",
        "description": "Temaki de salmão com cream cheese e cebolinha",
        "keywords": "temaki, philadelphia, cream cheese, salmão",
        "is_available": True,
    },
    # ── Sashimis ──
    {
        "name": "Sashimi de Salmão",
        "price": 39.90,
        "category": "Sashimis",
        "description": "10 fatias finas de salmão fresco",
        "keywords": "sashimi, salmão, fatias, cru",
        "is_available": True,
    },
    {
        "name": "Sashimi de Atum",
        "price": 44.90,
        "category": "Sashimis",
        "description": "10 fatias de atum fresco selecionado",
        "keywords": "sashimi, atum, fatias, cru",
        "is_available": True,
    },
    {
        "name": "Sashimi Misto",
        "price": 54.90,
        "category": "Sashimis",
        "description": "Mix de 15 fatias de salmão, atum e peixe branco",
        "keywords": "sashimi, misto, variado",
        "is_available": True,
    },
    # ── Combinados ──
    {
        "name": "Combinado Zen",
        "price": 59.90,
        "category": "Combinados",
        "description": "20 peças: 10 hot rolls, 5 uramakis e 5 niguiris de salmão",
        "keywords": "combinado, zen, peças, variado",
        "is_available": True,
    },
    {
        "name": "Combinado Premium",
        "price": 89.90,
        "category": "Combinados",
        "description": "30 peças: 10 hot rolls, 8 uramakis, 6 sashimis e 6 niguiris",
        "keywords": "combinado, premium, grande",
        "is_available": True,
    },
    {
        "name": "Combinado Casal",
        "price": 109.90,
        "category": "Combinados",
        "description": "40 peças para duas pessoas com seleção especial variada",
        "keywords": "combinado, casal, duas pessoas",
        "is_available": True,
    },
    # ── Hot Rolls ──
    {
        "name": "Hot Roll Salmão",
        "price": 32.90,
        "category": "Hot Rolls",
        "description": "8 unidades de rolo empanado e frito recheado com salmão e cream cheese",
        "keywords": "hot roll, salmão, frito, empanado",
        "is_available": True,
    },
    {
        "name": "Hot Roll Camarão",
        "price": 36.90,
        "category": "Hot Rolls",
        "description": "8 unidades de rolo empanado com camarão e cream cheese",
        "keywords": "hot roll, camarão, frito",
        "is_available": True,
    },
    # ── Bebidas ──
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
        "description": "Suco natural de laranja, maracujá ou limão 300ml",
        "keywords": "suco, natural, fruta",
        "is_available": True,
    },
    # ── Unavailable ──
    {
        "name": "Temaki Salmão Especial",
        "price": 35.90,
        "category": "Temakis",
        "description": "Temaki duplo de salmão com cream cheese e crispy",
        "keywords": "temaki, salmão, especial, duplo",
        "is_available": False,
    },
    {
        "name": "Combinado Família",
        "price": 149.90,
        "category": "Combinados",
        "description": "60 peças variadas para toda a família",
        "keywords": "combinado, família, grande",
        "is_available": False,
    },
]

TEST_SCENARIOS = {
    # Single item add
    "add_single_msg": "quero um temaki salmão",
    "add_single_expected": [("Temaki Salmão", 1)],
    # Multi-item add with quantities
    "add_multi_msg": "quero 2 hot roll salmão e 3 temaki atum",
    "add_multi_expected": [("Hot Roll Salmão", 2), ("Temaki Atum", 3)],
    # Unavailable product + available in same message
    "unavailable_msg": "quero um temaki salmão especial e um temaki skin",
    "unavailable_available": "Temaki Skin",
    "unavailable_name": "Temaki Salmão Especial",
    # Remove item (add first, then remove)
    "remove_add_msg": "quero 3 sashimi de salmão",
    "remove_add_expected": ("Sashimi de Salmão", 3),
    "remove_msg": "tira o sashimi de salmão",
    # Suggestion trigger
    "suggestion_msg": "o que tem de bom?",
    # Checkout flow
    "checkout_product_msg": "quero 2 temaki philadelphia",
    "checkout_product_expected": ("Temaki Philadelphia", 2),
}
