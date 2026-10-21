# Scraper de preços

Scraper em Python para pesquisar produtos no Continente, Pingo Doce, Lidl,
Auchan e Mercadona.

## Utilização

```powershell
python scraper.py "leite meio gordo" --json
```

Opções úteis:

- `--retries 4`: número de tentativas por URL;
- `--timeout 20`: timeout de cada pedido HTTP;
- `--json`: saída adequada para guardar/processar noutra aplicação.

O scraper usa retries com backoff exponencial, alterna User-Agents e tenta
extrair preços de JSON-LD e do texto HTML. Alguns sites são renderizados
apenas por JavaScript, requerem localização/loja ou podem bloquear automação;
nesses casos é normal uma loja não devolver resultados. Os URLs estão

No Auchan, os resultados são lidos por cartão de produto, incluindo nome,
preço e URL. Assim, uma pesquisa como `natas parmalat carbonara` devolve
apenas o produto real correspondente, em vez de todos os preços soltos da
página. Os URLs estão
concentrados em `RETAILERS` no início de [scraper.py](./scraper.py), para
serem atualizados sem alterar a lógica.

Quando disponíveis, são usados os cartões estruturados de cada loja: o
Pingo Doce, Continente e Auchan fornecem dados Demandware/JSON embutidos e o
Lidl fornece cartões Vue com marca, nome, preço e URL. Isto evita associar
todos os preços da página ao mesmo produto.

O Lidl entrega os cartões de pesquisa apenas depois de executar JavaScript.
Por isso a interface usa automaticamente um browser headless quando a
resposta HTTP não contém produtos. Instala o suporte uma vez:

```powershell
py -3 -m pip install playwright
py -3 -m playwright install chromium
```

Depois executa `py -3 interface.py`; o Lidl será pesquisado no Chromium
headless e os cartões renderizados serão extraídos. A CLI continua a usar
apenas HTTP por defeito; para a ativar por código usa
`PriceScraper(use_browser=True)`. Sem Playwright, é registado um aviso
explicando a instalação necessária. O Auchan usa
`/pt/pesquisa?q=...` (com fallback para `Search-Show`) e já é processado
pelos cartões reais da página.

Quando o Lidl não encontra produtos, pode apresentar recomendações que não
correspondem à pesquisa. Essas recomendações são ignoradas: se não houver um
card cujo nome partilhe termos com a pesquisa, a consulta termina com zero
resultados e não fica à espera de mais carregamentos.

## Interface gráfica

Para experimentar visualmente com pedidos reais:

```powershell
py -3 interface.py
```

Escreve um produto, por exemplo `leite meio gordo`, e carrega em
**Pesquisar**. A interface mostra a loja, produto encontrado, preço,
confiança, tipo de correspondência e URL. As pesquisas ocorrem numa thread
separada para a janela não bloquear enquanto os sites respondem.

Também podes usar a CLI diretamente; ela faz pedidos HTTP reais:

```powershell
py -3 scraper.py "leite meio gordo" --json
```

## Utilização com produtos de uma fatura

Depois de extrair os itens da fatura (por OCR, PDF ou outra API), use
`search_product` ou `search_products`:

```python
from scraper import InvoiceProduct, PriceScraper

scraper = PriceScraper()
matches = scraper.search_product(
    InvoiceProduct(name="café torrado moído", brand="Delta", quantity=2)
)

for match in matches[:5]:
    print(match.match_type, match.score, match.result.retailer, match.result.price)
```

Também aceita o formato habitual de dados extraídos:

```python
products = [
    {"name": "leite meio gordo", "brand": "Mimosa", "quantity": 1},
    {"name": "arroz agulha", "quantity": 2},
]
matches_by_product = scraper.search_products(products)
```

Para cada produto são tentadas várias pesquisas: nome com marca, nome sem
marca e termos normalizados. Cada resultado inclui `score` (0 a 1) e
`match_type` (`exact` ou `similar`). Assim, uma marca que não exista numa
loja pode ser comparada com uma alternativa de marca branca. O campo
`quantity` fica associado ao item para o cálculo posterior do total da
fatura; os preços devolvidos são preços unitários.
