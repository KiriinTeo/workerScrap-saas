import asyncio
import json
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class AltenarScraper(BaseScraper):
    def __init__(self, target_urls, bookmaker_name, bookmaker_base_url, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.bookmaker_name = bookmaker_name
        self.bookmaker_base_url = bookmaker_base_url
        self.raw_data = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                str_data = str(data).lower()
                
                if 'topsports' not in str_data and 'markets' in str_data and ('events' in str_data or 'runners' in str_data):
                    self.raw_data = data
            except Exception:
                pass

    async def extract_single(self, url):
        self.raw_data = None
        self.page.on("response", self.intercept_odds)
        
        try:
            print(f"Acessando {self.bookmaker_name}: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(10000)
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")
            
        self.page.remove_listener("response", self.intercept_odds)

    def _find_events(self, data):
        events = []
        if isinstance(data, dict):
            if "markets" in data and ("name" in data or "eventName" in data):
                events.append(data)
            for key, value in data.items():
                events.extend(self._find_events(value))
        elif isinstance(data, list):
            for item in data:
                events.extend(self._find_events(item))
        return events

    def transform_and_load(self):
        if not self.raw_data:
            print(f"Nenhum dado com odds reais interceptado na {self.bookmaker_name}.")
            return

        db = SessionLocal()
        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)
            odds_to_insert = []
            
            events = self._find_events(self.raw_data)
            processed_events = set()
            
            for event in events:
                event_id = event.get("id")
                if event_id:
                    if event_id in processed_events: continue
                    processed_events.add(event_id)

                name_str = event.get("name", "")
                if not name_str or " vs " not in name_str:
                    continue
                    
                home_team, away_team = name_str.split(" vs ", 1)
                
                start_time_str = event.get("startDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError:
                        pass
                        
                match = get_or_create_match(db, home_team.strip(), away_team.strip(), "Futebol", start_time)
                
                markets = event.get("markets", [])
                for market in markets:
                    market_name = market.get("name", "").strip()
                    
                    market_type = ""
                    if any(m in market_name for m in ["1x2", "1X2", "Vencedor", "Match Result", "Resultado"]):
                        market_type = "1X2"
                    elif any(m in market_name for m in ["Ambas", "BTTS", "marcam", "Equipas Marcam"]):
                        market_type = "BTTS"
                    elif any(m in market_name for m in ["Total", "Mais/Menos", "Over/Under", "Gols"]):
                        market_type = "Over/Under"
                    else:
                        continue
                        
                    is_super_odd = market.get("isSuperOdd", False)
                    
                    selections = market.get("runners", [])
                    for selection in selections:
                        selection_name = selection.get("name", "").strip()
                        odd_value = selection.get("price", 0.0)
                        
                        if odd_value and float(odd_value) > 0:
                            odds_to_insert.append({
                                "match_id": match.id,
                                "bookmaker_id": bookmaker.id,
                                "market": market_type,
                                "selection": selection_name,
                                "odd_value": float(odd_value),
                                "is_super_odd": bool(is_super_odd)
                            })
                            
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds da {self.bookmaker_name} inseridas.")
            else:
                print(f"Eventos encontrados, mas sem odds para o Duplo Green. Gerando Dump...")
                with open(f"{self.bookmaker_name.lower()}_dump.json", "w", encoding="utf-8") as f:
                    json.dump(self.raw_data, f, indent=4, ensure_ascii=False)

        except Exception as e:
            print(f"Erro na {self.bookmaker_name}: {e}")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()
            
        await self.close_browser()