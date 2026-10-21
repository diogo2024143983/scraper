"""Scraper resiliente de preços para supermercados portugueses e Mercadona.

Os sites podem alterar HTML, bloquear pedidos ou depender de JavaScript. Por
isso cada loja tem várias URLs de pesquisa e o scraper tenta, por ordem:
pedidos HTTP com headers diferentes, parsing de JSON-LD e parsing de HTML.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
import unicodedata
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from html import unescape
from html.parser import HTMLParser
from itertools import permutations
from typing import Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, unquote, urljoin
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductPrice:
    retailer: str
    name: str
    price: float
    currency: str = "EUR"
    url: str = ""
    source: str = "html"


@dataclass(frozen=True)
class InvoiceProduct:
    """Produto obtido da fatura e usado para procurar equivalências."""

    name: str
    brand: str = ""
    quantity: float = 1.0


@dataclass(frozen=True)
class ProductMatch:
    requested: InvoiceProduct
    result: ProductPrice
    score: float
    match_type: str


@dataclass(frozen=True)
class RetailerConfig:
    name: str
    search_urls: tuple[str, ...]


RETAILERS: tuple[RetailerConfig, ...] = (
    RetailerConfig(
        "Continente",
        ("https://www.continente.pt/pesquisa/?q={query}",),
    ),
    RetailerConfig(
        "Pingo Doce",
        (
            "https://www.pingodoce.pt/on/demandware.store/Sites-pingo-doce-Site/default/Search-Show?q={query}",
            "https://www.pingodoce.pt/on/demandware.store/Sites-pingo-doce-Site/default/Search-UpdateGrid?q={query}&start=0&sz=24",
        ),
    ),
    RetailerConfig(
        "Lidl",
        ("https://www.lidl.pt/q/search?q={query}",),
    ),
    RetailerConfig(
        "Auchan",
        (
            "https://www.auchan.pt/pt/pesquisa?q={query}",
            "https://www.auchan.pt/on/demandware.store/Sites-AuchanPT-Site/pt_PT/Search-Show?q={query}",
        ),
    ),
    RetailerConfig(
        "Mercadona",
        ("https://tienda.mercadona.es/search?query={query}",),
    ),
)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
)
PRICE_RE = re.compile(r"(?<!\d)(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR)", re.I)


class _TextParser(HTMLParser):
    """Extrai texto e atributos relevantes sem depender de BeautifulSoup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.attributes: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._in_script = tag.lower() in {"script", "style"}
        for key, value in attrs:
            if value and key.lower() in {"class", "id", "data-price", "content"}:
                self.attributes.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"}:
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if not self._in_script:
            self.text.append(data)


def _to_price(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    normalized = value.replace("\xa0", " ").strip()
    match = PRICE_RE.search(normalized)
    if not match and re.fullmatch(r"\d{1,4}(?:[.,]\d{1,2})?", normalized):
        number_text = normalized
    elif match:
        number_text = match.group(1)
    else:
        return None
    if "," in number_text and "." in number_text:
        number = number_text.replace(".", "").replace(",", ".")
    elif "," in number_text:
        number = number_text.replace(",", ".")
    else:
        number = number_text
    try:
        return float(number)
    except ValueError:
        return None


def _json_ld_prices(html: str) -> list[tuple[str, float]]:
    prices: list[tuple[str, float]] = []
    for raw in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.I | re.S,
    ):
        try:
            data = json.loads(unescape(raw).strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            offers = item.get("offers", item)
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                continue
            price = _to_price(str(offers.get("price", "")))
            name = str(item.get("name", "")).strip()
            if price is not None and price > 0:
                prices.append((name, price))
    return prices


def _structured_product_prices(
    html: str, retailer: str, url: str, query: str
) -> list[ProductPrice]:
    """Extrai cartões conhecidos de Demandware e do catálogo atual do Lidl."""
    found: list[ProductPrice] = []

    # Continente, Auchan e Pingo Doce expõem este JSON nos cartões.
    for raw in re.findall(r'data-gtm-info="([^"]+)"', html, flags=re.I):
        try:
            data = json.loads(unescape(raw))
        except json.JSONDecodeError:
            continue
        items = data.get("items", []) if isinstance(data, dict) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            price = _to_price(str(data.get("value", item.get("price", ""))))
            name = str(item.get("item_name", "")).strip()
            brand = str(item.get("item_brand", "")).strip()
            if price and name:
                found.append(
                    ProductPrice(
                        retailer,
                        f"{brand} {name}".strip(),
                        price,
                        url=url,
                        source="data-gtm-info",
                    )
                )

    # Auchan: cartões Demandware com nome, URL e preço no mesmo bloco.
    auchan_starts = list(
        re.finditer(r'<div class="product-tile\b', html, flags=re.I)
    )
    for index, start in enumerate(auchan_starts):
        end = auchan_starts[index + 1].start() if index + 1 < len(auchan_starts) else len(html)
        block = html[start.start() : end]
        name_match = re.search(
            r'class="auc-product-tile__name".*?<a[^>]*>(.*?)</a>',
            block,
            flags=re.I | re.S,
        )
        price_match = re.search(r'class="sales".*?content="([0-9.,]+)"', block, flags=re.I | re.S)
        url_match = re.search(
            r'<a href="([^"]+)"[^>]*class="[^"]*auc-product-tile__image',
            block,
            flags=re.I,
        )
        if name_match and price_match:
            name = re.sub(r"<[^>]+>", " ", name_match.group(1))
            price = _to_price(price_match.group(1))
            if price:
                found.append(
                    ProductPrice(
                        retailer,
                        unescape(" ".join(name.split())),
                        price,
                        url=urljoin(url, unescape(url_match.group(1))) if url_match else url,
                        source="auchan-card",
                    )
                )

    # Continente: preço e nome são publicados no atributo de impressão.
    for raw in re.findall(
        r"data-product-tile-impression=(?:\"([^\"]+)\"|'([^']+)')",
        html,
        flags=re.I,
    ):
        raw = raw[0] or raw[1]
        try:
            data = json.loads(unescape(raw))
        except json.JSONDecodeError:
            continue
        price = _to_price(str(data.get("price", "")))
        name = str(data.get("name", "")).strip()
        if price and name:
            found.append(ProductPrice(retailer, name, price, url=url, source="continente-card"))

    # O Lidl usa cartões Vue: marca, título e preço ficam no mesmo bloco.
    # A impressão do cartão é JSON URL-encoded e é mais estável do que as
    # classes internas do componente Vue.
    lidl_blocks = re.findall(
        r'<li id="grid-item-[^"]+"[^>]*>(.*?)</li>',
        html,
        flags=re.I | re.S,
    )
    for block in lidl_blocks:
        impression_match = re.search(
            r'data-gridbox-impression="([^"]+)"',
            block,
            flags=re.I,
        )
        if not impression_match:
            continue
        try:
            data = json.loads(unquote(unescape(impression_match.group(1))))
        except json.JSONDecodeError:
            continue
        price = _to_price(str(data.get("price", "")))
        name = str(data.get("name", "")).strip()
        brand = str(data.get("brand", "")).strip()
        link_match = re.search(
            r'class="odsc-tile__link"[^>]+href="([^"]+)"',
            block,
            flags=re.I,
        )
        if price and name:
            found.append(
                ProductPrice(
                    retailer,
                    f"{brand} {name}".strip(),
                    price,
                    url=unescape(link_match.group(1)) if link_match else url,
                    source="lidl-card",
                )
            )

    # Compatibilidade com uma versão anterior do markup do Lidl.
    card_pattern = re.compile(
        r'<a[^>]+href="([^"]+/p/[^"]+)"[^>]*>.*?'
        r'class="product-grid-box__brand"[^>]*>(.*?)</div>.*?'
        r'class="product-grid-box__title"[^>]*>(.*?)</div>.*?'
        r'class="ods-price[^"]*"[^>]*>.*?'
        r'class="ods-price__value"[^>]*>'
        r'([^<]+)',
        flags=re.I | re.S,
    )
    for product_url, brand, name, price_text in card_pattern.findall(html):
        price = _to_price(price_text)
        clean_name = re.sub(r"<[^>]+>", " ", name)
        clean_brand = re.sub(r"<[^>]+>", " ", brand)
        if price and clean_name.strip():
            found.append(
                ProductPrice(
                    retailer,
                    f"{unescape(clean_brand).strip()} {unescape(clean_name).strip()}".strip(),
                    price,
                    url=unescape(product_url),
                    source="lidl-card",
                )
            )
    return found


def parse_prices(html: str, retailer: str, url: str, query: str) -> list[ProductPrice]:
    """Parseia preços visíveis e JSON-LD, removendo duplicados."""
    found: list[ProductPrice] = _structured_product_prices(html, retailer, url, query)
    for name, price in _json_ld_prices(html):
        found.append(ProductPrice(retailer, name or query, price, url=url, source="json-ld"))

    parser = _TextParser()
    parser.feed(html)
    text = " ".join(parser.text)
    if not found and retailer not in {"Auchan", "Continente", "Pingo Doce", "Lidl"}:
        for match in PRICE_RE.finditer(text):
            price = _to_price(match.group(0))
            if price is not None and price > 0:
                found.append(ProductPrice(retailer, query, price, url=url, source="html"))

    unique: dict[tuple[str, float], ProductPrice] = {}
    for item in found:
        unique.setdefault((item.name.casefold(), item.price), item)
    return list(unique.values())


def _normalise_terms(value: str) -> list[str]:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]+", value)


def _query_variants(product: InvoiceProduct) -> tuple[str, ...]:
    """Cria tentativas do produto exato e de marca branca."""
    exact = " ".join(part for part in (product.brand, product.name) if part).strip()
    generic = product.name.strip()
    variants = [exact, generic]
    terms = _normalise_terms(generic)
    if len(terms) > 2:
        variants.append(" ".join(terms))
    if 1 < len(terms) <= 5:
        # Alguns motores de pesquisa dão relevância diferente à ordem dos
        # termos. Tenta todas as ordens para garantir que os termos ficam
        # juntos, por exemplo "abelharuco branco" e "branco abelharuco".
        variants.extend(" ".join(order) for order in permutations(terms))
    elif len(terms) > 5:
        # Evita uma explosão factorial em descrições longas de faturas.
        variants.extend((" ".join(reversed(terms)), " ".join(sorted(terms))))
    return tuple(dict.fromkeys(item for item in variants if item))


def _match_result(product: InvoiceProduct, result: ProductPrice) -> tuple[float, str]:
    requested_terms = set(_normalise_terms(f"{product.brand} {product.name}"))
    result_terms = set(_normalise_terms(result.name))
    if not requested_terms or not result_terms:
        return 0.0, "similar"
    overlap = len(requested_terms & result_terms) / len(requested_terms)
    similarity = SequenceMatcher(
        None,
        " ".join(sorted(requested_terms)),
        " ".join(sorted(result_terms)),
    ).ratio()
    score = min(1.0, 0.65 * overlap + 0.35 * similarity)
    brand_terms = set(_normalise_terms(product.brand))
    if brand_terms and brand_terms <= result_terms:
        score = min(1.0, score + 0.15)
    return score, "exact" if score >= 0.85 else "similar"


def _has_relevant_lidl_product(html: str, query: str) -> bool:
    """Evita tratar recomendações de uma pesquisa vazia como resultados."""
    terms = set(_normalise_terms(query))
    if not terms:
        return False
    names = re.findall(
        r'class="product-grid-box__title"[^>]*>(.*?)</div>',
        html,
        flags=re.I | re.S,
    )
    for raw_name in names:
        name = set(_normalise_terms(re.sub(r"<[^>]+>", " ", raw_name)))
        if terms & name:
            return True
    return False


def _lidl_has_unrelated_cards(html: str, query: str) -> bool:
    """Deteta recomendações carregadas para uma pesquisa sem correspondência."""
    names = re.findall(
        r'class="product-grid-box__title"[^>]*>(.*?)</div>',
        html,
        flags=re.I | re.S,
    )
    return bool(names) and not _has_relevant_lidl_product(html, query)


class PriceScraper:
    def __init__(
        self,
        retailers: Sequence[RetailerConfig] = RETAILERS,
        timeout: float = 15,
        retries: int = 3,
        backoff: float = 0.75,
        opener=urlopen,
        use_browser: bool = False,
    ) -> None:
        if retries < 1:
            raise ValueError("retries deve ser pelo menos 1")
        self.retailers = tuple(retailers)
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._opener = opener
        self.use_browser = use_browser

    def _fetch(self, url: str, attempt: int) -> str:
        request = Request(
            url,
            headers={
                "User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)],
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
            },
        )
        with self._opener(request, timeout=self.timeout) as response:
            body = response.read()
            return body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")

    def _fetch_lidl_browser(self, url: str) -> str:
        """Obtém o HTML renderizado do Lidl através de Playwright opcional."""
        try:
            from playwright.sync_api import (
                TimeoutError as PlaywrightTimeoutError,
                sync_playwright,
            )
        except ImportError as exc:
            raise RuntimeError(
                "Para obter produtos do Lidl, instala Playwright: "
                "py -3 -m pip install playwright; py -3 -m playwright install chromium"
            ) from exc
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=USER_AGENTS[0],
                locale="pt-PT",
            )
            try:
                # Analytics and consent requests can remain open indefinitely;
                # waiting for networkidle would therefore cause false timeouts.
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=min(5000, int(self.timeout * 1000)),
                )
                page.locator("main").first.wait_for(
                    state="attached",
                    timeout=min(2500, int(self.timeout * 1000)),
                )
                try:
                    page.locator(".product-grid-box__title").first.wait_for(
                        state="attached",
                        timeout=min(2500, int(self.timeout * 1000)),
                    )
                except PlaywrightTimeoutError:
                    return ""
                page.wait_for_timeout(1000)
                return page.content()
            except PlaywrightTimeoutError:
                LOGGER.warning("Lidl excedeu o tempo limite para %s", url)
                return ""
            finally:
                browser.close()

    def search(self, query: str, *, allow_lidl_browser: bool | None = None) -> list[ProductPrice]:
        if not query.strip():
            raise ValueError("query não pode estar vazia")
        results: list[ProductPrice] = []
        lidl_browser_attempted = False
        if allow_lidl_browser is None:
            allow_lidl_browser = self.use_browser
        for retailer in self.retailers:
            retailer_results: list[ProductPrice] = []
            for template in retailer.search_urls:
                url = template.format(query=quote_plus(query.strip()))
                for attempt in range(self.retries):
                    try:
                        html = self._fetch(url, attempt)
                        retailer_results = parse_prices(html, retailer.name, url, query)
                        if (
                            not retailer_results
                            and retailer.name == "Lidl"
                            and allow_lidl_browser
                            and not lidl_browser_attempted
                        ):
                            if _lidl_has_unrelated_cards(html, query):
                                break
                            lidl_browser_attempted = True
                            html = self._fetch_lidl_browser(url)
                            retailer_results = (
                                parse_prices(html, retailer.name, url, query)
                                if html
                                else []
                            )
                        if retailer_results:
                            break
                    except (HTTPError, URLError, TimeoutError, OSError) as exc:
                        LOGGER.warning(
                            "%s: tentativa %d/%d falhou: %s",
                            retailer.name,
                            attempt + 1,
                            self.retries,
                            exc,
                        )
                    if attempt + 1 < self.retries:
                        time.sleep(self.backoff * (2**attempt))
                if retailer_results:
                    break
            if not retailer_results and retailer.name == "Lidl":
                LOGGER.warning(
                    "Lidl não devolveu produtos no HTML HTTP; a pesquisa é "
                    "renderizada por JavaScript e requer um endpoint/API ou browser headless."
                )
            results.extend(retailer_results)
        return sorted(results, key=lambda item: (item.retailer, item.price))

    def search_product(
        self,
        product: InvoiceProduct | Mapping[str, object] | str,
        *,
        minimum_score: float = 0.25,
    ) -> list[ProductMatch]:
        """Procura um item da fatura, incluindo alternativas sem marca.

        ``product`` pode ser um ``InvoiceProduct``, um dicionário extraído de
        OCR/JSON (com ``name`` e opcionalmente ``brand``) ou apenas texto.
        Os resultados são deduplicados por loja/nome/preço e ordenados por
        confiança, mantendo o preço mais baixo como desempate.
        """
        if isinstance(product, str):
            requested = InvoiceProduct(product)
        elif isinstance(product, InvoiceProduct):
            requested = product
        else:
            raw_name = product.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                raise ValueError("o produto da fatura precisa de um campo 'name'")
            raw_brand = product.get("brand", "")
            requested = InvoiceProduct(raw_name, raw_brand if isinstance(raw_brand, str) else "")

        candidates: dict[tuple[str, str, float], ProductMatch] = {}
        for index, query in enumerate(_query_variants(requested)):
            for result in self.search(
                query,
                allow_lidl_browser=self.use_browser and index == 0,
            ):
                score, match_type = _match_result(requested, result)
                if score >= minimum_score:
                    key = (result.retailer, result.name.casefold(), result.price)
                    candidates.setdefault(
                        key,
                        ProductMatch(requested, result, score, match_type),
                    )
            # The first query already found candidates; avoid repeating the
            # same network work in another word order. Alternate ordering is
            # kept as a fallback for search engines returning no products.
            if candidates:
                break
        return sorted(
            candidates.values(),
            key=lambda item: (-item.score, item.result.price, item.result.retailer),
        )

    def search_products(
        self,
        products: Sequence[InvoiceProduct | Mapping[str, object] | str],
        *,
        minimum_score: float = 0.25,
    ) -> dict[str, list[ProductMatch]]:
        """Processa todos os produtos extraídos da fatura."""
        return {
            product.name if isinstance(product, InvoiceProduct) else (
                product if isinstance(product, str) else str(product.get("name", ""))
            ): self.search_product(product, minimum_score=minimum_score)
            for product in products
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Pesquisa preços em supermercados.")
    parser.add_argument("query", help="produto a pesquisar, por exemplo 'leite meio gordo'")
    parser.add_argument("--retries", type=int, default=3, help="tentativas por URL (predefinição: 3)")
    parser.add_argument("--timeout", type=float, default=15, help="timeout HTTP em segundos")
    parser.add_argument("--json", action="store_true", help="imprime resultados em JSON")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    results = PriceScraper(timeout=args.timeout, retries=args.retries).search(args.query)
    if args.json:
        print(json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2))
    else:
        for item in results:
            print(f"{item.retailer}: {item.price:.2f} {item.currency} - {item.name} ({item.url})")
    if not results:
        LOGGER.warning("Nenhum preço encontrado; os sites podem exigir JavaScript ou ter bloqueado o pedido.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
