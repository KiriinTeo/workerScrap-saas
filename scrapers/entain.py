import asyncio
import json
import re
import traceback
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class EntainScraper(BaseScraper):
    def __init__(self, target_urls, bookmaker_name, bookmaker_base_url, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.bookmaker_name = bookmaker_name
        self.bookmaker_base_url = bookmaker_base_url
        self.list_json_payloads = []
        self.match_json_payloads = []

    async def _intercept_list(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                self.list_json_payloads.append(data)
            except Exception:
                pass

    def _extract_main_league_urls(self, base_url):
        fixtures = []
        def _find(data):
            if isinstance(data, dict):
                if "fixtures" in data and isinstance(data["fixtures"], list):
                    for f in data["fixtures"]:
                        if "id" in f:
                            fixtures.append(f)
                for v in data.values():
                    _find(v)
            elif isinstance(data, list):
                for i in data:
                    _find(i)

        _find(self.list_json_payloads)
        
        if not fixtures:
            return set()

        league_keys = ["tournamentId", "competitionId", "leagueId", "categoryId", "sportId"]
        target_fixtures = fixtures

        for key in league_keys:
            counts = Counter([str(f[key]) for f in fixtures if key in f])
            if counts:
                main_league_id = counts.most_common(1)[0][0]
                target_fixtures = [f for f in fixtures if str(f.get(key)) == main_league_id]
                break

        urls = set()
        for f in target_fixtures:
            u = f.get("url") or f.get("eventUrl")
            if u and 'outright' not in str(u).lower():
                clean = str(u)
                if not clean.startswith('/'): clean = f"/{clean}"
                urls.add(f"{base_url}{clean}")
            else:
                urls.add(f"{base_url}/pt-br/sports/eventos/{f['id']}")
                
        return urls

    async def get_match_urls(self, url):
        self.list_json_payloads = []
        self.page.on("response", self._intercept_list)
        
        parsed_url = urlparse(url)
        base = f"{parsed_url.scheme}://{parsed_url.netloc}"

        try:
            print(f"Acessando listagem Entain ({self.bookmaker_name}): {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            for _ in range(5):
                await self.page.evaluate("window.scrollBy(0, 1500)")
                await self.page.wait_for_timeout(2000)

        except Exception:
            pass
        finally:
            self.page.remove_listener("response", self._intercept_list)

        final_urls = self._extract_main_league_urls(base)
        return list(final_urls)

    async def _intercept_match(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                if "optionMarkets" in str(data):
                    self.match_json_payloads.append(data)
            except Exception:
                pass

    async def extract_single(self, url):
        self.match_json_payloads = []
        self.page.on("response", self._intercept_match)

        try:
            print(f"Acessando partida interna: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(6000)
        except Exception:
            pass
        finally:
            self.page.remove_listener("response", self._intercept_match)

    def _generate_debug_dump(self, reason="unknown"):
        if self.match_json_payloads:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"debug_entain_{self.bookmaker_name.lower()}_{reason}_{timestamp}.json"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(self.match_json_payloads, f, indent=4, ensure_ascii=False)
                print(f"[!] Dump de debug gerado: {filename} (Motivo: {reason})")
            except Exception:
                pass

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

    def transform_and_load(self):
        if not self.match_json_payloads:
            return

        db = SessionLocal()
        unique_odds = {}
        fixtures = []
        seen_ids = set()
        
        self._find_fixtures_data(self.match_json_payloads, fixtures, seen_ids)

        if not fixtures:
            self._generate_debug_dump(reason="no_fixtures_found")
            return

        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)

            for fixture in fixtures:
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
                    
                start_time_str = fixture.get("startDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try: start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError: pass
                        
                match = get_or_create_match(db, home_team, away_team, "Futebol", start_time)
                
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
                        
                        sel_code = None
                        if raw_selection_name in ["1", home_team.upper()]: sel_code = "1"
                        elif raw_selection_name in ["X", "EMPATE", "DRAW"]: sel_code = "X"
                        elif raw_selection_name in ["2", away_team.upper()]: sel_code = "2"

                        if not sel_code: continue

                        is_vitoria = sel_code in ["1", "2"]
                        is_empate = sel_code == "X"

                        if is_vitoria and not has_early_payout: continue
                        if is_empate and not is_super_odd: continue

                        odd_key = f"{match.id}_{sel_code}"
                        
                        if odd_key not in unique_odds or has_early_payout or is_super_odd:
                            unique_odds[odd_key] = {
                                "match_id": match.id,
                                "bookmaker_id": bookmaker.id,
                                "market": "1X2",
                                "selection": sel_code,
                                "odd_value": float(odd_value),
                                "is_super_odd": bool(is_super_odd)
                            }
                        
            odds_to_insert = list(unique_odds.values())
                        
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds restritas salvas ({self.bookmaker_name}).")

        except Exception:
            traceback.print_exc()
            self._generate_debug_dump(reason="exception_raised")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for base_url in self.target_urls:
            match_urls = await self.get_match_urls(base_url)
            print(f"[-] {len(match_urls)} URLs da liga principal forjadas. Iniciando escaneamento profundo...")
            
            for match_url in match_urls:
                await self.extract_single(match_url)
                self.transform_and_load()
            
        await self.close_browser()