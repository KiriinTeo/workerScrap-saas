import json
from .base_scraper import BaseScraper

class AltenarScraper(BaseScraper):
    def __init__(self, bookmaker_name='altenar', headless=True):
        super().__init__(bookmaker_name, headless)
        self.raw_data = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                if isinstance(data, dict) and 'events' in data and 'markets' in data:
                    if len(data.get('events', [])) > 0:
                        self.raw_data = data
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.raw_data = None
        self.page.on("response", self.intercept_odds)
        
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(10000)
        except Exception:
            pass
            
        self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.raw_data:
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.raw_data, f, indent=4, ensure_ascii=False)

        await self._parse_altenar_data()

    async def _parse_altenar_data(self):
        if not self.raw_data:
            return

        try:
            events_raw = self.raw_data.get("events", [])
            markets_raw = {m.get("id"): m for m in self.raw_data.get("markets", [])}
            odds_raw = {o.get("id"): o for o in self.raw_data.get("odds", [])}
            
            for event in events_raw:
                match_id = event.get("id")
                name_str = event.get("name", "")
                if not name_str or " vs. " not in name_str:
                    continue
                    
                home_team, away_team = name_str.split(" vs. ", 1)
                home_team, away_team = home_team.strip(), away_team.strip()
                
                market_ids = event.get("marketIds", [])
                for m_id in market_ids:
                    market = markets_raw.get(m_id)
                    if not market: continue
                        
                    market_name = market.get("name", "").strip().upper()
                 
                    if not any(m in market_name for m in ["VENCEDOR DO ENCONTRO", "1X2", "MATCH RESULT", "RESULTADO DA PARTIDA"]):
                        continue 
                    
                    has_early_payout = any(term in market_name for term in ["PAGAMENTO ANTECIPADO", "EARLY PAYOUT", "VANTAGEM", "2 UP"])
                        
                    odd_ids = market.get("oddIds", [])
                    for o_id in odd_ids:
                        odd_data = odds_raw.get(o_id)
                        if not odd_data or odd_data.get("oddStatus") != 0: 
                            continue
                            
                        odd_value = odd_data.get("price", 0.0)
                        raw_selection_name = odd_data.get("name", "").strip()
                        is_super_odd = odd_data.get("isDBB", False)
                        
                        await self.process_and_store_odd(
                            match_id=match_id,
                            home_team=home_team,
                            away_team=away_team,
                            selection_name=raw_selection_name,
                            odd_value=odd_value,
                            has_early_payout=has_early_payout,
                            is_super_odd=is_super_odd
                        )
        except Exception:
            pass