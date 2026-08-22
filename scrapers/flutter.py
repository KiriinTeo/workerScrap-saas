import json
import re
import traceback
import unicodedata
from .base_scraper import BaseScraper

def normalize_name(text):
    if not text: return ""
    text = unicodedata.normalize('NFD', str(text)).encode('ascii', 'ignore').decode('utf-8')
    return re.sub(r'[^a-z0-9]', '', text.lower())

class FlutterScraper(BaseScraper):
    def __init__(self, headless=True):
        super().__init__('betfair', headless)
        self.prices_data = []
        self.html_content = ""
        self.target_comp_id = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                str_data = str(data)
                if 'runnerDetails' in str_data or 'selectionId' in str_data:
                    self.prices_data.append(data)
            except Exception:
                pass

    async def scrape(self, url, save_dump=False):
        self.prices_data = []
        self.html_content = ""
        self.target_comp_id = None

        match = re.search(r'/c-(\d+)', url)
        if match:
            self.target_comp_id = str(match.group(1))

        self.page.on("response", self.intercept_odds)
        
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(4):
                await self.page.evaluate("window.scrollBy(0, 800)")
                await self.page.wait_for_timeout(2000)
            
            self.html_content = await self.page.content()
        except Exception:
            pass
        finally:
            self.page.remove_listener("response", self.intercept_odds)

        if save_dump and self.prices_data:
            import os
            os.makedirs("dumps", exist_ok=True)
            with open(f"dumps/{self.house_name}_raw_dump.json", "w", encoding="utf-8") as f:
                json.dump({"payloads": self.prices_data, "ssr": {}}, f, indent=2, ensure_ascii=False)

        await self._parse_flutter_data()

    @staticmethod
    def _extract_balanced_json_objects(text, anchor):
        objects = []
        for m in re.finditer(re.escape(anchor), text):
            start = text.rfind('{', 0, m.start())
            if start == -1: continue
            depth, in_string, escape, end = 0, False, False, None
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escape: escape = False
                    elif ch == '\\': escape = True
                    elif ch == '"': in_string = False
                    continue
                if ch == '"': in_string = True
                elif ch == '{': depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end: objects.append(text[start:end])
        return objects

    @staticmethod
    def _extract_named_state(text, var_name):
        idx = text.find(var_name)
        if idx == -1: return None
        eq = text.find('=', idx)
        if eq == -1: return None
        start = text.find('{', eq)
        if start == -1: return None
        depth, in_string, escape = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape: escape = False
                elif ch == '\\': escape = True
                elif ch == '"': in_string = False
                continue
            if ch == '"': in_string = True
            elif ch == '{': depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0: return text[start:i + 1]
        return None

    @staticmethod
    def _urn_id(urn):
        return str(urn).rsplit(':', 1)[-1] if urn else ""

    def _build_catalogue_from_graphql_cache(self, data, catalogue):
        events_by_urn = {e.get("urn"): e for e in data.get("SportsEvent", []) if e.get("urn")}
        fixtures_by_id = {self._urn_id(f.get("urn")): f for f in data.get("FootballFixture", []) if f.get("urn")}
        for market in data.get("SportsbookMarket", []):
            m_id = str(market.get("marketId", ""))
            event_urn = (market.get("hierarchy") or {}).get("sportevent")
            event = events_by_urn.get(event_urn)
            if not m_id or not event: continue
            fixture = fixtures_by_id.get(self._urn_id(event_urn))
            home_name = (fixture.get("home") or {}).get("name") if fixture else None
            away_name = (fixture.get("away") or {}).get("name") if fixture else None
            runners = [{"selectionId": r.get("selectionId"), "runnerName": r.get("name", ""), "resultType": r.get("resultType", "")} for r in market.get("runners", [])]
            catalogue[m_id] = {
                "marketId": m_id, "marketName": market.get("name", ""), "marketType": market.get("marketType", ""),
                "runners": runners, "event": {"name": event.get("name", ""), "home": home_name, "away": away_name}
            }

    def _extract_catalogue_from_html(self):
        catalogue = {}
        try:
            blob = self._extract_named_state(self.html_content, "window.__TBD_PRELOADED_CATALOG__")
            if blob:
                try:
                    state_data = json.loads(blob)
                    self.prices_data.append(state_data)
                    self._build_catalogue_from_graphql_cache(state_data.get("data", {}), catalogue)
                except json.JSONDecodeError: pass
            if not catalogue:
                for candidate in self._extract_balanced_json_objects(self.html_content, '"marketId"'):
                    if '"event"' not in candidate: continue
                    try: obj = json.loads(candidate)
                    except json.JSONDecodeError: continue
                    m_id = str(obj.get("marketId", ""))
                    if m_id and "event" in obj and isinstance(obj["event"], dict) and "name" in obj["event"]:
                        catalogue[m_id] = obj
                        self.prices_data.append(obj)
                state_blob = self._extract_named_state(self.html_content, "window.INITIAL_STATE")
                if state_blob:
                    try:
                        state_data = json.loads(state_blob)
                        self._find_catalogue_recursive(state_data, catalogue)
                        self.prices_data.append(state_data)
                    except json.JSONDecodeError: pass
        except Exception: pass
        if not catalogue:
            self._find_catalogue_recursive(self.prices_data, catalogue)
        return catalogue

    def _find_catalogue_recursive(self, data, catalogue):
        if isinstance(data, dict):
            m_id = str(data.get("marketId") or data.get("id", ""))
            if m_id and "event" in data and isinstance(data["event"], dict) and "name" in data["event"]:
                catalogue[m_id] = data
            for key, value in data.items(): self._find_catalogue_recursive(value, catalogue)
        elif isinstance(data, list):
            for item in data: self._find_catalogue_recursive(item, catalogue)

    def _find_prices_recursive(self, data, prices):
        if isinstance(data, dict):
            m_id = str(data.get("marketId", ""))
            if m_id and "runnerDetails" in data and isinstance(data["runnerDetails"], list):
                bucket = prices.setdefault(m_id, {})
                for runner in data["runnerDetails"]:
                    sel_id = str(runner.get("selectionId", ""))
                    if sel_id: bucket[sel_id] = runner
            for key, value in data.items(): self._find_prices_recursive(value, prices)
        elif isinstance(data, list):
            for item in data: self._find_prices_recursive(item, prices)

    async def _parse_flutter_data(self):
        catalogue = self._extract_catalogue_from_html()
        prices = {}
        self._find_prices_recursive(self.prices_data, prices)

        if not catalogue or not prices:
            print("🚨 [ERRO FLUTTER] Catálogo ou preços ausentes.")
            return

        try:
            for market_id, market_info in catalogue.items():
                event = market_info.get("event", {})
                event_name = event.get("name", "")
                
                match_id = event.get("id") or str(hash(event_name))

                home_team, away_team = event.get("home"), event.get("away")
                if not home_team or not away_team:
                    found_sep = next((sep for sep in [" x ", " v ", " - ", " vs "] if sep in event_name.lower()), None)
                    if not found_sep: continue
                    parts = re.split(re.escape(found_sep), event_name, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) != 2: continue
                    home_team, away_team = parts

                home_team, away_team = home_team.strip(), away_team.strip()
                home_norm = normalize_name(home_team)
                away_norm = normalize_name(away_team)
                
                m_type_raw = str(market_info.get("marketType", "")).upper()
                base_type = m_type_raw.split("_-_")[0]
                
                is_main_market = base_type in ["MATCH_ODDS", "FULL_TIME_RESULT", "1X2"]
                is_super_odd_market = "BOOST" in m_type_raw

                if not is_main_market and not is_super_odd_market:
                    continue

                has_early_payout = "2_UP" in m_type_raw
                if self.target_comp_id and is_main_market:
                    has_early_payout = True

                runners_catalog = {str(r.get("selectionId", "")): {"name": r.get("runnerName", "")} 
                                   for r in market_info.get("runners", []) if r.get("selectionId")}

                market_prices = prices.get(market_id, {})

                for runner_price in market_prices.values():
                    selection_id = str(runner_price.get("selectionId", ""))
                    runner_info = runners_catalog.get(selection_id, {})
                    selection_name = (runner_info.get("name") or runner_price.get("runnerName", "")).strip()
                    
                    if not selection_name: continue
                        
                    status = str(runner_price.get("runnerStatus", runner_price.get("status", ""))).upper()
                    if status and status not in ["ACTIVE", "OPEN"]: continue

                    odd_value, disp_odds_val, true_odds_val = 0.0, 0.0, 0.0
                    
                    runner_odds = runner_price.get("runnerOdds", {}) or runner_price.get("winRunnerOdds", {})
                    if runner_odds:
                        disp_odds = runner_odds.get("decimalDisplayOdds", {})
                        true_odds = runner_odds.get("trueOdds", {})
                        
                        def extract_odd_val(odd_obj):
                            if isinstance(odd_obj, dict):
                                dec_odds = odd_obj.get("decimalOdds")
                                if isinstance(dec_odds, dict):
                                    return float(dec_odds.get("decimalOdds", 0.0))
                                elif isinstance(dec_odds, (int, float, str)):
                                    try: return float(dec_odds)
                                    except ValueError: return 0.0
                            elif isinstance(odd_obj, (int, float, str)):
                                try: return float(odd_obj)
                                except ValueError: return 0.0
                            return 0.0

                        disp_odds_val = extract_odd_val(disp_odds)
                        true_odds_val = extract_odd_val(true_odds)
                        odd_value = disp_odds_val or true_odds_val

                    is_super_odd = is_super_odd_market or (disp_odds_val > 0.0 and true_odds_val > 0.0 and disp_odds_val > true_odds_val)

                    if odd_value <= 1.0:
                        continue

                    sel_norm = normalize_name(selection_name)
                    
                    is_vitoria = sel_norm in ['1', '2', 'home', 'away'] or sel_norm in home_norm or home_norm in sel_norm or sel_norm in away_norm or away_norm in sel_norm
                    is_empate = sel_norm in ['x', 'empate', 'draw', 'thedraw', 'empates']
                    
                    print(f"   [RAIO-X] Recebido: {home_team} x {away_team} | Sel: '{selection_name}' ({sel_norm}) @ {odd_value} | EP: {has_early_payout} | SO: {is_super_odd}")

                    if (is_vitoria and has_early_payout) or (is_empate and is_super_odd):
                        print(f"      APROVADO!")
                        await self.process_and_store_odd(
                            match_id=match_id,
                            home_team=home_team,
                            away_team=away_team,
                            selection_name=selection_name,
                            odd_value=odd_value,
                            has_early_payout=has_early_payout,
                            is_super_odd=is_super_odd
                        )
                    else:
                        print(f"      REPROVADO.")

        except Exception:
            traceback.print_exc()