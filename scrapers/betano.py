import asyncio
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class BetanoScraper(BaseScraper):
    def __init__(self, target_urls, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.raw_data = None

    async def extract_single(self, url):
        self.raw_data = None
        try:
            print(f"Acessando Betano: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(5000)
            
            ssr_data = await self.page.evaluate("() => window.initial_state || window.INITIAL_STATE")
            if ssr_data:
                self.raw_data = ssr_data
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")

    def _find_events(self, data):
        events = []
        if isinstance(data, dict):
            if "markets" in data and "name" in data and "startTime" in data:
                events.append(data)
            for key, value in data.items():
                events.extend(self._find_events(value))
        elif isinstance(data, list):
            for item in data:
                events.extend(self._find_events(item))
        return events

    def transform_and_load(self):
        if not self.raw_data:
            print("Falha: O cofre initial_state não foi capturado nesta URL.")
            return

        db = SessionLocal()
        try:
            bookmaker = get_or_create_bookmaker(db, "Betano", "https://br.betano.com")
            odds_to_insert = []
            
            events = self._find_events(self.raw_data)
            processed_events = set()

            for event in events:
                event_id = event.get("id")
                if event_id in processed_events: continue
                processed_events.add(event_id)

                name_str = event.get("name", "")
                if not name_str: continue
                    
                home_team, away_team = name_str, "Adversario"
                for sep in [" - ", " v ", " x ", " vs "]:
                    if sep in name_str:
                        home_team, away_team = name_str.split(sep, 1)
                        break
                        
                start_time_ts = event.get("startTime", 0)
                if start_time_ts == 0: continue
                start_time = datetime.fromtimestamp(start_time_ts / 1000, tz=timezone.utc)
                
                match = get_or_create_match(db, home_team.strip(), away_team.strip(), "Futebol", start_time)
                
                markets = event.get("markets", [])
                for market in markets:
                    market_name = market.get("name", "Mercado Desconhecido")
                    selections = market.get("selections", [])
                    for selection in selections:
                        selection_name = selection.get("fullName", "") or selection.get("name", "")
                        odd_value = selection.get("price", 0.0)
                        
                        if odd_value > 0:
                            odds_to_insert.append({
                                "match_id": match.id,
                                "bookmaker_id": bookmaker.id,
                                "market": market_name,
                                "selection": selection_name,
                                "odd_value": float(odd_value)
                            })
                            
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds da Betano inseridas no banco.")
            else:
                print("A varredura não achou odds válidas nesta URL.")
        except Exception as e:
            print(f"Erro na transformacao da Betano: {e}")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()
            
        await self.close_browser()