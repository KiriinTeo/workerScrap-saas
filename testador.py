import argparse
import asyncio
import sys
import os

from scrapers.kaizen import KaizenScraper
from scrapers.alternar import AltenarScraper
from scrapers.kambi import KambiScraper
from scrapers.flutter import FlutterScraper
from scrapers.entain import EntainScraper

SCRAPER_REGISTRY = {
    "kaizen": KaizenScraper,
    "altenar": AltenarScraper,
    "kambi": KambiScraper,
    "flutter": FlutterScraper,
    "entain": EntainScraper
}

DEFAULT_URLS = {
    "kaizen": "https://www.betano.bet.br/sport/futebol/brasil/brasileirao-serie-a-betano/10016r/?bt=matchresult",
    "altenar": "https://www.vaidebet.bet.br/sports#/sport/66/category/593/championship/11318", 
    "kambi": "https://www.kto.bet.br/esportes/futebol/brasil/brasileirao-serie-a",
    "flutter": "https://www.betfair.bet.br/apostas/futebol/brasileir%C3%A3o-s%C3%A9rie-a/c-13",
    "entain": "https://www.sportingbet.bet.br/pt-br/sports/futebol-4/aposta/brasil-33/brasileiro-serie-a-102838" 
}

async def run_test(scraper_name, custom_url=None, headless=False):
    scraper_name = scraper_name.lower()
    
    if scraper_name not in SCRAPER_REGISTRY:
        print(f"Erro: Scraper '{scraper_name}' nao encontrado.")
        print(f"Opcoes disponiveis: {', '.join(SCRAPER_REGISTRY.keys())}")
        sys.exit(1)

    url_alvo = custom_url if custom_url else DEFAULT_URLS.get(scraper_name)
    
    if not url_alvo:
        print(f"Erro: Nenhuma URL padrao definida para {scraper_name} e nenhuma foi fornecida.")
        sys.exit(1)

    os.makedirs("dumps", exist_ok=True)

    print(f"Iniciando ambiente de testes para: {scraper_name.upper()}")
    print(f"URL: {url_alvo}")
    print(f"Modo Headless: {headless}")
    
    ScraperClass = SCRAPER_REGISTRY[scraper_name]
    scraper = ScraperClass(headless=headless) 
    
    try:
        await scraper.init_browser()
        await scraper.scrape(url_alvo, save_dump=True)
        print("Scraping finalizado com sucesso. Verifique a pasta dumps/.")
    except Exception as e:
        print(f"Erro durante o teste: {e}")
    finally:
        await scraper.close()
        print("Teste encerrado.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Testador Dinamico de Scrapers - Bet SaaS")
    
    parser.add_argument(
        "scraper", 
        type=str, 
        help=f"Nome do scraper para testar. Opcoes: {', '.join(SCRAPER_REGISTRY.keys())}"
    )
    
    parser.add_argument(
        "-u", "--url", 
        type=str, 
        help="URL customizada para o teste (ignora a URL padrao)", 
        default=None
    )
    
    parser.add_argument(
        "--headless", 
        action="store_true", 
        help="Roda o navegador em modo invisivel (sem abrir a janela)"
    )

    args = parser.parse_args()

    asyncio.run(run_test(
        scraper_name=args.scraper, 
        custom_url=args.url, 
        headless=args.headless
    ))