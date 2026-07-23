import asyncio
import json
from scrapers.base_scraper import BaseScraper

class BetanoScraper(BaseScraper):
    def __init__(self, target_url, headless=False):
        super().__init__(headless)
        self.target_url = target_url
        self.raw_data = None

    async def extract(self):
        await self.init_browser()
        
        await self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        
        try:
            print("Acessando a Betano...")
            await self.page.goto(self.target_url, wait_until="domcontentloaded", timeout=60000)
            
            print("Pagina carregada. Aguardando 5 segundos para a hidratacao do SSR...")
            await self.page.wait_for_timeout(5000)
            
            print("Injetando script para ler a memoria do navegador...")
            
            # Aqui pedimos para o navegador nos entregar a variavel global certa
            ssr_data = await self.page.evaluate("""() => {
                if (window.__NUXT__) return window.__NUXT__;
                if (window.__NEXT_DATA__) return window.__NEXT_DATA__;
                if (window.INITIAL_STATE) return window.INITIAL_STATE;
                if (window.state) return window.state;
                return null;
            }""")
            
            if ssr_data:
                print("\n[BINGO] Cofre de dados SSR encontrado e extraido da memoria!")
                self.raw_data = ssr_data
                
                with open("betano_ssr_dump.json", "w", encoding="utf-8") as f:
                    json.dump(ssr_data, f, indent=4, ensure_ascii=False)
                    
                print("Arquivo 'betano_ssr_dump.json' salvo com sucesso na raiz do projeto!")
            else:
                print("\nFalha: Nenhuma variavel global mapeada continha os dados.")

        except Exception as e:
            print(f"Erro de navegacao na Betano: {e}")
            
        await self.close_browser()

    def transform_and_load(self):
        # Desativamos a logica de banco de dados temporariamente 
        # so para garantirmos a captura visual do JSON e avaliarmos a estrutura
        pass

    async def run(self):
        await self.extract()
        self.transform_and_load()