import json
import re
from .base_scraper import BaseScraper

class EntainScraper(BaseScraper):
    def __init__(self, bookmaker_name='entain', headless=True):
        super().__init__(bookmaker_name, headless)
        self.match_json_payloads = []

    async def _intercept_match(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                if 'cds-api' in response.url or 'fixture' in response.url:
                    data = await response.json()
                    if "optionMarkets" in str(data) or "markets" in str(data):
                        self.match_json_payloads.append(data)
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.match_json_payloads = []
        self.page.on("response", self._intercept_match)

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            for _ in range(4):
                await self.page.evaluate("window.scrollBy(0, 1000)")
                await self.page.wait_for_timeout(1500)
                
        except Exception:
            pass
        finally:
            self.page.remove_listener("response", self._intercept_match)

        if save_dump and self.match_json_payloads:
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump(self.match_json_payloads, f, indent=4, ensure_ascii=False)
            print(f"Dump da {self.house_name.upper()} gerado com sucesso! ({len(self.match_json_payloads)} payloads)")
        elif save_dump:
            print(f"Falha: Nenhum dado capturado no interceptor da {self.house_name.upper()}.")

        await self._parse_entain_data()

    def _find_fixtures_data(self, data, fixtures_list, seen_ids):
        if isinstance(data, dict):
            if "optionMarkets" in data and "id" in data:
                f_id = str(data.get("id"))
                if f_id not in seen_ids:
                    seen_ids.add(f_id)
                    fixtures_list.append(data)
            for value in data.values():
                self._find_fixtures_data(value, fixtures_list, seen_ids)
        elif isinstance(data, list):
            for item in data:
                self._find_fixtures_data(item, fixtures_list, seen_ids)

    async def _parse_entain_data(self):
        if not self.match_json_payloads:
            return

        fixtures = []
        seen_ids = set()
        
        self._find_fixtures_data(self.match_json_payloads, fixtures, seen_ids)

        try:
            for fixture in fixtures:
                match_id = fixture.get("id")
                home_team = fixture.get("homeName", "").strip()
                away_team = fixture.get("awayName", "").strip()
                
                if not home_team or not away_team:
                    f_name = fixture.get("name", {}).get("value", "")
                    if " - " in f_name:
                        parts = f_name.split(" - ", 1)
                        home_team, away_team = parts[0].strip(), parts[1].strip()
                    elif " v " in f_name.lower() or " vs " in f_name.lower():
                        parts = re.split(r'\s+vs?\s+', f_name, maxsplit=1, flags=re.IGNORECASE)
                        if len(parts) == 2:
                            home_team, away_team = parts[0].strip(), parts[1].strip()

                if not home_team or not away_team: 
                    continue
                    
                option_markets = fixture.get("optionMarkets", [])
                for market in option_markets:
                    market_name = market.get("name", {"value": ""}).get("value", "").upper()
                    
                    valid_markets = ["RESULTADO DA PARTIDA", "1X2", "MATCH RESULT", "VENCEDOR", "TEMPO REGULAMENTAR"]
                    if not any(m in market_name for m in valid_markets):
                        continue
                    
                    has_early_payout = any(term in market_name for term in ["VP+2", " VP", "(VP)", "VANTAGEM", "PAGAMENTO ANTECIPADO"])
                    
                    options = market.get("options", [])
                    for option in options:
                        price_data = option.get("price", {})
                        if not price_data: continue
                            
                        num = price_data.get("numerator")
                        den = price_data.get("denominator")
                        if num is None or den is None or den == 0: continue
                            
                        odd_value = (float(num) / float(den)) + 1.0
                        if odd_value <= 1.0: continue

                        raw_selection_name = option.get("name", {"value": ""}).get("value", "").strip().upper()
                        is_super_odd = option.get("isBoosted", False) or any(b in market_name for b in ["BOOST", "SUPER ODD", "AUMENTADA"])
                        
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