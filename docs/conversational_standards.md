# WhatsApp Food Ordering — Conversational Standards

Real-world patterns of how Brazilian customers order food via WhatsApp. Based on research across Brazilian delivery SaaS platforms (Anota AI, Goomer, Saipos, Delivery Direto, AiPyra, ZaperMenu, WhatsMenu, among others).

This document serves as a reference for evolving ZenBots' conversational capabilities.

---

## 1. Message Structure Patterns

### 1.1 Greeting + Order (very common)
Customers frequently combine a greeting with their order in a single message.
```
"Oi boa noite, quero 2 x-bacon e uma coca 2L"
"Boa noite! Gostaria de fazer um pedido"
"Oi, to querendo pedir um lanche"
"Eae, manda um lanche pra mim"
```

### 1.2 Direct Order (no greeting)
```
"1 pizza calabresa grande"
"Quero um hamburguer artesanal e uma batata frita"
"Me vê 2 x-tudo e 1 guaraná"
"Manda 3 coxinhas e 2 pastéis de carne"
```

### 1.3 Multi-Line Fragmented Orders (very common)
Customers send items one line at a time instead of a single message.
```
Oi
quero um x-bacon
sem cebola
e uma coca lata
é pra entrega
```

### 1.4 Everything-in-One Message
Some customers pack items, customizations, address, and payment into one message.
```
"Quero 1 combo familia, pago no pix, Rua das Flores 123 apt 45"
"2 x-salada e 1 coca 2L, vou pagar em dinheiro, troco pra 100"
```

---

## 2. Quantity Expression Patterns

### 2.1 Digit Before Item
```
"2 x-bacon"
"1 coca lata"
"3 coxinhas"
```

### 2.2 Written-Out Numbers (Portuguese)
```
"dois x-bacon"
"uma pizza grande"
"meia dúzia de coxinha"
```

### 2.3 Implicit Quantity (means 1)
```
"hamburguer artesanal" (= 1 hamburguer artesanal)
"coca cola" (= 1 coca cola)
```

### 2.4 Corrections
```
"ops, são 3 não 2"
"na verdade quero 5"
```

---

## 3. Customization Patterns (sem/com/extra)

Very common in real orders. Currently NOT supported by ZenBots — items are added as-is from the catalog.

### 3.1 Remove Ingredient
```
"x-tudo SEM tomate e cebola"
"pizza sem azeitona"
"lanche sem salada"
```

### 3.2 Add/Extra Ingredient
```
"com maionese extra"
"com bacon extra"
"adiciona queijo"
```

### 3.3 Swap Ingredient
```
"pode trocar o pão por integral?"
"troca a coca por guaraná"
```

### 3.4 Half-and-Half (pizzas)
```
"pizza meia calabresa meia frango"
"meia portuguesa meia margherita"
```

**Status:** Not yet supported. Tracked as a future capability.

---

## 4. Noise Patterns (non-order content mixed in)

### 4.1 Politeness/Filler (very common, low impact)
```
"por favor", "pfv", "obrigado", "valeu", "blz"
```
**Impact on extraction:** Low. These words don't match product names and don't dilute word-overlap scoring.

### 4.2 Payment Method Declarations (common)
```
"pago no pix"
"cartão"
"dinheiro, troco pra 50"
"manda o pix que pago agora"
```
**Impact on extraction:** Low when appended at the end. Can cause issues if mixed between items: "2 x-bacon pago pix e 1 coca" — "pago pix" absorbed into x-bacon's name.

### 4.3 Address Inline (occasional)
```
"Rua das Flores 123, apto 4B"
"é perto da padaria do João"
```
**Impact on extraction:** Medium. Numbers in addresses (e.g. "123") can trigger false quantity extraction.

### 4.4 Questions Mixed In (occasional)
```
"vocês tem guaraná? se tiver manda 2"
"ainda tem x-egg?"
"qual o preço do x-tudo?"
```
**Impact on extraction:** Medium. The question structure confuses intent classification.

### 4.5 Urgency/Timing (occasional)
```
"é pra agora"
"demora quanto?"
"preciso até as 20h"
"pode mandar daqui a 30 min?"
```
**Impact on extraction:** Low-medium. Numbers in time expressions ("30 min", "20h") can trigger false quantity extraction.

### 4.6 Family/Context Comments (rare in ADD, more common in MODIFY)
```
"eh pra 4 pessoas"
"pro aniversário do meu filho"
"minha esposa quer sem cebola"
```
**Impact on extraction:** Low in ADD (rare). Higher in MODIFY: "tira 3 mignon, minha tia não vem mais" — explanation absorbed into item name.

### 4.7 Parenthetical Remarks (rare)
```
"6 picanha com bacon (favorito da família)"
"2 x-tudo (aquele que vem com ovo)"
```
**Impact on extraction:** Medium. Entire parenthetical absorbed into item name. Easy to strip with zero risk.

### 4.8 Emotional/Social (common but harmless)
```
"kkkkk"
"to morrendo de fome"
"😋🔥👍"
```
**Impact on extraction:** None. These don't contain numbers or product-like words.

### 4.9 Previous Experience References (rare)
```
"da última vez veio frio"
"mesmo pedido de sempre"
"aquele lanche bom"
```
**Impact on extraction:** Low. No structured order data to extract.

---

## 5. Abbreviations and Informal Spelling

Very common in Brazilian WhatsApp communication.

| Abbreviation | Meaning |
|---|---|
| qro, kro | quero |
| vc, vcs | você, vocês |
| tb, tbm | também |
| hj | hoje |
| pfv, pf | por favor |
| obg | obrigado |
| blz | beleza |
| pra | para |
| pro | para o |
| tô, to | estou |
| oq | o que |
| msg | mensagem |
| refri | refrigerante |
| hamburguer | hambúrguer |

---

## 6. Modification/Removal Patterns

How customers change existing orders.

### 6.1 Direct Remove
```
"tira o x-bacon"
"pode tirar 2 coca"
"remove a batata frita"
"cancela o x-tudo"
```

### 6.2 Quantity Adjustment
```
"na verdade troca a coca por guaraná"   # swap — NOT supported
"são 3, não 2"                          # delta correction — partial
"coloca mais 1 batata"                  # supported via continuation guard
"diminui pra 2 x-bacon"                 # supported via F2 (deixa só 2 x-bacon)
"deixa só 1 latte"                      # SET semantics — supported via F2
"fica só 2 coca"                        # SET semantics — supported via F2
"muda pra 3 pizza"                      # SET semantics — supported via F2
```

**Status (2026-04-09):** "deixa/fica/muda + só/apenas/pra + qty + product" patterns route through F2's `_QTY_REDUCE_RE` pre-router guard → MODIFY intent → `_programmatic_cart_reduce(set_mode=True)`. The qty becomes the FINAL quantity, not a delta. Item swap (`troca X por Y`) is still not supported.

### 6.3 Remove with Explanation (the noisy case)
```
"tira 3 mignon, minha tia falou que não vem mais"
"cancela 2 x-bacon, mudaram de ideia"
"pode tirar a coca, ele já comeu"
```
**Impact:** Explanation text absorbed into item name by extractor. Most common source of noise in MODIFY intent.

### 6.4 Total cart clearing
```
"limpa tudo"
"esvazia o carrinho"
"zera o pedido"
"apaga tudo"
"limpa td"            # slang abbreviation
"limpa meu carrinho"
"esvazia carrinho"    # no article
"apagar o pedido"     # infinitive
```

**Status (2026-04-09):** Supported via F4's `_CLEAR_KEYWORD_RE` pre-router guard → CLEAR_CART intent → `_handle_clear_cart`. Also bypasses the suggestion-handler when active suggestions are present (F5 Part C). False-positive guard ensures `"limpa o copo"` / `"apaga a luz"` don't match — the object alternation requires `tudo|td|carrinho|pedido`.

Variants NOT supported (and not commonly used by Brazilian customers): `"deleta tudo"`, `"remove tudo"` (these go through the REMOVE path instead).

---

## 7. Voice Messages

Multiple sources highlight that Brazilian customers frequently send **audio messages** instead of text. This is a significant gap — ZenBots currently only processes text messages.

**Status:** Not supported. Would require speech-to-text integration (e.g., Whisper API).

---

## 8. The "19h30 Effect"

Described by AiPyra CRM: delivery orders spike sharply around 7:30 PM BRT. This is when the most complex, multi-item, noisy messages arrive simultaneously. System performance under load matters most during this window.

---

## 9. Platform Capability Status

> Updated 2026-04-09 to reflect Pillar 1 + Pillar 2 fixes (F1, F2, F3, F4, F5, phrase bank qty fix, `_STOP += rola`, embedding graceful degradation).

| Capability | Status | Notes |
|---|---|---|
| Basic item extraction (qty + name) | Supported | Programmatic + LLM fallback |
| Written-out PT-BR numbers | Supported | Including typo variants |
| Compound numbers ("vinte e sete") | Supported | `_collapse_compound_numbers` keeps them as one token |
| Greeting handling | Supported | Separated before extraction |
| Multi-item single message | Supported | Programmatic path |
| Verb-less bare ADD ("1 cookies", "três coxinhas") | Supported | F1 Day 1 — `_BARE_ADD_RE` |
| Continuation ("e uma coca", "mais um lanche") | Supported | F1 Day 1 — `_CONTINUATION_KEYWORD_RE` |
| Slang ADD verbs ("me arruma", "rola", "joga", "solta") | Supported | Pre-router or LLM path; verbs in `_STOP` |
| REMOVE — explicit verb + product | Supported | `_REMOVE_KEYWORD_RE` |
| REMOVE — alt phrasing ("cancela esse X", "esquece o X", "deixa o X pra la") | Supported | `_REMOVE_ALT_RE` |
| REMOVE — negation ("não quero X", "não manda o X") | Supported | `_REMOVE_NEGATION_RE` |
| Multi-target REMOVE in one message | Supported | LLM tool call with multiple `product_ids` |
| Quantity SET ("deixa só 1 X", "muda pra 3 Y") | **Supported (2026-04-09)** | F2 — `_QTY_REDUCE_RE` + `_programmatic_cart_reduce(set_mode=True)` |
| Cart clearing ("limpa tudo", "esvazia o carrinho") | **Supported (2026-04-09)** | F4 — `_CLEAR_KEYWORD_RE` |
| Suggestion-handler bypass for explicit shopping commands | **Supported (2026-04-09)** | F5 — Parts A/B/C — clears `last_suggestions` when ADD/QTY_REDUCE/CLEAR keyword present |
| FINISH ("finalizar", "vamo fechar", "pode mandar") | Supported | F1 + F1b — `_FINISH_KEYWORD_RE` + Option E + Option H multi-intent chain |
| Unavailable item detection (em falta) | Supported | Programmatic path; em falta message built outside the LLM |
| Product name typo tolerance | Supported | Fuzzy word-overlap (Layer 4 pg_trgm) |
| Embedding graceful degradation (HF Hub 429) | **Supported (2026-04-09)** | `EmbeddingsUnavailable` exception → falls back to ILIKE/keyword/trgm only |
| Parenthetical noise stripping | Not yet | Zero-risk, planned |
| Customizations (sem/com/extra) | Not supported | High-impact future feature (P2.3) |
| Item swap ("troca a coca por guaraná") | Not supported | Future feature (P2.5) |
| Multi-line fragmented orders | Partial | Conversation history provides context |
| Voice messages | Not supported | Requires speech-to-text (P2.1) |
| Voice transcript filler stripping | Not yet | P2.1 deliverable |
| Address extraction | Supported | CEP-based flow in checkout |
| Payment method selection | Supported | State machine flow |
| Inline payment/address in order message | Not handled | Noise absorbed into item names |
| Abbreviation expansion ("brahma" for "Cerveja Brahma 600ml") | Partial | LLM stochastic; F9 ambiguous-match clarification would harden it |
| Ambiguous product disambiguation ("me vê 1 picanha" with 3 picanha variants) | Not yet | F9 deferred — `propose_and_confirm_action` tool exists, never invoked for this case |
| Product name digit handling ("Casquinha 3 bolas") | **Broken** | Confirmed P0 extractor bug, deferred to P2 (architectural change) |
| Joke / sarcasm markers ("100 hambúrguer, brincadeira, 1 só") | Not supported | Stretch goal |
| Prompt injection resistance | Supported | LLM output sanitized; no role-play compliance |

---

## Sources

- Delivery Direto — "6 problemas gerados com pedidos pelo WhatsApp"
- AiPyra CRM — "Atendimento automático WhatsApp delivery: efeito 19h30"
- Saipos — "Pedidos por WhatsApp"
- WhatsMenu — "Como fazer pedidos por WhatsApp"
- Goomer — "Mensagem automática para delivery"
- ZaperMenu — "Pedidos via WhatsApp: 7 dúvidas respondidas"
- Xenioo — Food delivery chatbot tutorial
- Assistente Smart — "Chatbot para Hamburgueria"
