import asyncio
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class SportingbetScraper(BaseScraper):
    def __init__(self, target_url, headless=False):
        super().__init__(headless)
        self.target_url = target_url
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

    async def extract(self):
        await self.init_browser()
        
        await self.page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        
        self.page.on("response", self.intercept_odds)
        
        try:
            await self.page.goto(self.target_url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(15000)
        except:
            pass
            
        await self.close_browser()

    def transform_and_load(self):
        if not self.raw_data or not isinstance(self.raw_data, dict):
            print("Nenhum dado capturado.")
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
                    if not match_name:
                        continue
                        
                    home_team, away_team = match_name, "N/A"
                    for sep in [" - ", " v ", " x ", " vs "]:
                        if sep in match_name:
                            home_team, away_team = match_name.split(sep, 1)
                            break
                    
                    start_time_str = fixture.get("startDate")
                    if not start_time_str:
                        start_time = datetime.now(timezone.utc)
                    else:
                        try:
                            start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        except ValueError:
                            start_time = datetime.now(timezone.utc)

                    match = get_or_create_match(db, home_team.strip(), away_team.strip(), "Sportingbet Event", start_time)
                    
                    markets = fixture.get("optionMarkets", [])
                    for market in markets:
                        market_name = market.get("name", {}).get("value", "Mercado")
                        
                        options = market.get("options", [])
                        for option in options:
                            selection_name = option.get("name", {}).get("value", "Selecao")
                            price_data = option.get("price", {})
                            
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
                                    "market": market_name,
                                    "selection": selection_name,
                                    "odd_value": float(odd_value)
                                })
            
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso. Odds inseridas: {len(odds_to_insert)}")
            else:
                print("JSON capturado mas sem odds legiveis.")
                with open("odds_sportingbet.json", "w", encoding="utf-8") as f:
                    import json
                    json.dump(self.raw_data, f, indent=4, ensure_ascii=False)

        except Exception as e:
            print(f"Erro no parse: {e}")
        finally:
            db.close()

    async def run(self):
        await self.extract()
        self.transform_and_load()