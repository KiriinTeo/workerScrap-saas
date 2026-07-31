import asyncio
import json
import re
import traceback
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class FlutterScraper(BaseScraper):
    def __init__(self, target_urls, bookmaker_name, bookmaker_base_url, headless=False):
        super().__init__(headless=headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.bookmaker_name = bookmaker_name
        self.bookmaker_base_url = bookmaker_base_url
        self.prices_data = []
        self.html_content = ""

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                str_data = str(data)
                if 'runnerDetails' in str_data or 'selectionId' in str_data:
                    self.prices_data.append(data)
            except Exception:
                pass

    async def extract_single(self, url):
        self.prices_data = []
        self.html_content = ""
        self.page.on("response", self.intercept_odds)
        
        try:
            print(f"Acessando {self.bookmaker_name}: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(3):
                await self.page.evaluate("window.scrollBy(0, 800)")
                await self.page.wait_for_timeout(2000)
            
            self.html_content = await self.page.content()
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")
        finally:
            self.page.remove_listener("response", self.intercept_odds)

    def _generate_debug_dump(self, reason="unknown"):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            if self.html_content:
                html_file = f"debug_{self.bookmaker_name.lower()}_{reason}_{timestamp}.html"
                with open(html_file, "w", encoding="utf-8") as f:
                    f.write(self.html_content)
            if self.prices_data:
                json_file = f"debug_{self.bookmaker_name.lower()}_{reason}_{timestamp}.json"
                with open(json_file, "w", encoding="utf-8") as f:
                    json.dump(self.prices_data, f, indent=2, ensure_ascii=False)
            print(f"[!] Dumps de debug gerados (Motivo: {reason})")
        except Exception as e:
            print(f"Falha ao salvar dump de debug: {e}")

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
                "runners": runners, "event": {"name": event.get("name", ""), "openDate": event.get("openDate"), "home": home_name, "away": away_name}
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

    def transform_and_load(self):
        catalogue = self._extract_catalogue_from_html()
        prices = {}
        self._find_prices_recursive(self.prices_data, prices)

        if not catalogue or not prices:
            print(f"Nenhum dado de catálogo/preço interceptado na {self.bookmaker_name}.")
            self._generate_debug_dump(reason="missing_core_data")
            return

        db = SessionLocal()
        odds_to_insert = []

        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)

            for market_id, market_info in catalogue.items():
                event = market_info.get("event", {})
                event_name = event.get("name", "")

                home_team, away_team = event.get("home"), event.get("away")
                if not home_team or not away_team:
                    found_sep = next((sep for sep in [" x ", " v ", " - ", " vs "] if sep in event_name.lower()), None)
                    if not found_sep: continue
                    parts = re.split(re.escape(found_sep), event_name, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) != 2: continue
                    home_team, away_team = parts

                home_team, away_team = home_team.strip(), away_team.strip()
                start_time_str = event.get("openDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try: start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError: pass

                match = get_or_create_match(db, home_team, away_team, "Futebol", start_time)

                m_type_raw = str(market_info.get("marketType", "")).upper()
                base_type = m_type_raw.split("_-_")[0]
                
                if base_type not in ["MATCH_ODDS", "FULL_TIME_RESULT", "1X2"] and "BOOST" not in m_type_raw:
                    continue

                has_early_payout = "2_UP" in m_type_raw

                runners_catalog = {str(r.get("selectionId", "")): {"name": r.get("runnerName", ""), "resultType": str(r.get("resultType", "")).upper()} 
                                   for r in market_info.get("runners", []) if r.get("selectionId")}

                market_prices = prices.get(market_id, {})

                for runner_price in market_prices.values():
                    selection_id = str(runner_price.get("selectionId", ""))
                    runner_info = runners_catalog.get(selection_id, {})
                    selection_name = (runner_info.get("name") or runner_price.get("runnerName", "")).strip()
                    result_type = runner_info.get("resultType", "")
                    
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

                    is_super_odd = ("BOOST" in m_type_raw) or (disp_odds_val > 0.0 and true_odds_val > 0.0 and disp_odds_val > true_odds_val)

                    if float(odd_value) > 1.0:
                        selection = None
                        RESULT_TYPE_MAP = {"HOME": "1", "AWAY": "2", "DRAW": "X"}
                        if result_type in RESULT_TYPE_MAP:
                            selection = RESULT_TYPE_MAP[result_type]
                        elif selection_name.upper() == home_team.upper():
                            selection = "1"
                        elif selection_name.upper() == away_team.upper():
                            selection = "2"
                        elif any(emp in selection_name.upper() for emp in ["EMPATE", "DRAW"]):
                            selection = "X"

                        if not selection:
                            continue

                        is_vitoria = selection in ["1", "2"]
                        is_empate = selection == "X"

                        if is_vitoria and not has_early_payout:
                            continue
                        
                        if is_empate and not is_super_odd:
                            continue

                        odds_to_insert.append({
                            "match_id": match.id,
                            "bookmaker_id": bookmaker.id,
                            "market": "1X2",
                            "selection": selection,
                            "odd_value": float(odd_value),
                            "is_super_odd": is_super_odd
                        })

            if odds_to_insert:
                print(f"Sucesso: {len(odds_to_insert)} odds filtradas da {self.bookmaker_name} inseridas no banco.")
                bulk_insert_odds(db, odds_to_insert)
                self._generate_debug_dump(reason="success")
            else:
                print(f"Eventos encontrados, mas nenhuma odd passou no filtro restrito (Vitória+EP ou Empate+SuperOdd).")
                self._generate_debug_dump(reason="no_valid_odds")

        except Exception as e:
            print(f"Erro na {self.bookmaker_name}: {e}")
            traceback.print_exc()
            self._generate_debug_dump(reason="exception_raised")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()

        await self.close_browser()