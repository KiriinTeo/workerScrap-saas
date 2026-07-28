import asyncio
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class SportingbetScraper(BaseScraper):
    def __init__(self, target_urls, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.raw_data = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'api/widget' in response.url:
            try:
                data = await response.json()
                for widget in data.get("widgets", []):
                    if "fixtures" in widget.get("payload", {}):
                        self.raw_data = data
                        break
            except Exception:
                pass

    async def extract_single(self, url):
        self.raw_data = None 
        self.page.on("response", self.intercept_odds)
        
        try:
            print(f"Acessando Sportingbet: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(15000) 
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")
            
        self.page.remove_listener("response", self.intercept_odds)

    def transform_and_load(self):
        if not self.raw_data or not isinstance(self.raw_data, dict):
            print("Nenhum dado válido interceptado nesta URL da Sportingbet.")
            return

        db = SessionLocal()
        try:
            bookmaker = get_or_create_bookmaker(db, "Sportingbet", "https://sports.sportingbet.com")
            odds_to_insert = []
            
            widgets = self.raw_data.get("widgets", [])
            for widget in widgets:
                payload = widget.get("payload", {})
                fixtures = payload.get("fixtures", [])
                
                for fixture in fixtures:
                    match_name = fixture.get("name", {}).get("value", "")
                    if not match_name: continue
                        
                    home_team, away_team = match_name, "N/A"
                    for sep in [" - ", " v ", " x ", " vs "]:
                        if sep in match_name:
                            home_team, away_team = match_name.split(sep, 1)
                            break
                    
                    start_time_str = fixture.get("startDate")
                    start_time = datetime.now(timezone.utc)
                    if start_time_str:
                        try:
                            start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        except ValueError:
                            pass

                    match = get_or_create_match(db, home_team.strip(), away_team.strip(), "Futebol", start_time)
                    
                    markets = fixture.get("optionMarkets", [])
                    for market in markets:
                        market_name = market.get("name", {}).get("value", "").strip()
                        
                        market_type = ""
                        if any(m in market_name for m in ["Resultado da partida", "Tempo Regulamentar", "1X2"]):
                            market_type = "1X2"
                        elif any(m in market_name for m in ["Ambas as equipes marcam", "Ambas Marcam"]):
                            market_type = "BTTS"
                        elif any(m in market_name for m in ["Total de gols", "Mais/Menos"]):
                            market_type = "Over/Under"
                        else:
                            continue
                            
                        is_super_odd = False 
                        
                        options = market.get("options", [])
                        for option in options:
                            selection_name = option.get("name", {}).get("value", "").strip()
                            price_data = option.get("price", {})

                            if selection_name in ["X", "x", "v", "vs", "VS", "V"]:
                                selection_name = "Empate"
                            
                            odd_value = price_data.get("odds") or price_data.get("oddsValue")
                            if not odd_value:
                                num = price_data.get("numerator")
                                den = price_data.get("denominator")
                                if num is not None and den is not None and den != 0:
                                    odd_value = (float(num) / float(den)) + 1.0

                            if odd_value:
                                odds_to_insert.append({
                                    "match_id": match.id,
                                    "bookmaker_id": bookmaker.id,
                                    "market": market_type,
                                    "selection": selection_name,
                                    "odd_value": float(odd_value),
                                    "is_super_odd": is_super_odd
                                })
            
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso. {len(odds_to_insert)} odds inseridas da Sportingbet.")
        except Exception as e:
            print(f"Erro no parse da Sportingbet: {e}")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        # Loop Mestre
        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()
            
        await self.close_browser()