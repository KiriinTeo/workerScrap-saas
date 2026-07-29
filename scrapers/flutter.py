import asyncio
import json
import re
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
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(3):
                await self.page.evaluate("window.scrollBy(0, 800)")
                await self.page.wait_for_timeout(2000)
            
            self.html_content = await self.page.content()
        except Exception:
            pass
        finally:
            self.page.remove_listener("response", self.intercept_odds)

    @staticmethod
    def _extract_balanced_json_objects(text, anchor):
        """
        Encontra objetos JSON balanceados (contagem de chaves) que contenham `anchor`.
        Substitui o antigo regex não-guloso, que corta no primeiro '}' e falha em
        qualquer objeto com dicts aninhados (ex: "event": {...}).
        """
        objects = []
        for m in re.finditer(re.escape(anchor), text):
            # anda pra trás até achar o '{' que abre o objeto que contém o anchor
            start = text.rfind('{', 0, m.start())
            if start == -1:
                continue
            depth = 0
            end = None
            in_string = False
            escape = False
            for i in range(start, len(text)):
                ch = text[i]
                if in_string:
                    if escape:
                        escape = False
                    elif ch == '\\':
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end:
                objects.append(text[start:end])
        return objects

    def _extract_catalogue_from_html(self):
        catalogue = {}
        try:
            for candidate in self._extract_balanced_json_objects(self.html_content, '"marketId"'):
                if '"event"' not in candidate:
                    continue
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                m_id = str(obj.get("marketId", ""))
                if m_id and "event" in obj and isinstance(obj["event"], dict) and "name" in obj["event"]:
                    catalogue[m_id] = obj
                    self.prices_data.append(obj)

            state_match = re.search(r'window\.INITIAL_STATE\s*=\s*(\{.*?\});\s*</script>', self.html_content, re.DOTALL)

            if state_match:
                try:
                    state_data = json.loads(state_match.group(1))
                    self._find_catalogue_recursive(state_data, catalogue)
                    self.prices_data.append(state_data)
                except json.JSONDecodeError:
                    pass

        except Exception as e:
            print(f"Erro ao extrair catálogo do HTML: {e}")

        # Fallback: o catálogo pode ter chegado via rede (fetch/XHR) e já estar
        # em self.prices_data (o interceptor captura qualquer resposta com
        # "selectionId", presente tanto em runnerDetails quanto em runners).
        # Antes isso era ignorado pois só se olhava para o HTML.
        if not catalogue:
            self._find_catalogue_recursive(self.prices_data, catalogue)

        return catalogue

    def _find_catalogue_recursive(self, data, catalogue):
        if isinstance(data, dict):
            m_id = str(data.get("marketId") or data.get("id", ""))
            if m_id and "event" in data and isinstance(data["event"], dict) and "name" in data["event"]:
                catalogue[m_id] = data
            for key, value in data.items():
                self._find_catalogue_recursive(value, catalogue)
        elif isinstance(data, list):
            for item in data:
                self._find_catalogue_recursive(item, catalogue)

    def _find_prices_recursive(self, data, prices):
        if isinstance(data, dict):
            m_id = str(data.get("marketId", ""))
            if m_id and "runnerDetails" in data and isinstance(data["runnerDetails"], list):
                if m_id not in prices:
                    prices[m_id] = []
                prices[m_id].extend(data["runnerDetails"])
            for key, value in data.items():
                self._find_prices_recursive(value, prices)
        elif isinstance(data, list):
            for item in data:
                self._find_prices_recursive(item, prices)

    def transform_and_load(self):
        catalogue = self._extract_catalogue_from_html()
        prices = {}
        self._find_prices_recursive(self.prices_data, prices)

        if not catalogue or not prices:
            print(f"Nenhum dado válido interceptado na {self.bookmaker_name} "
                  f"(catalogue={len(catalogue)} itens, prices={len(prices)} mercados). "
                  f"Salvando dump de debug.")
            try:
                with open(f"debug_{self.bookmaker_name.lower()}_html.html", "w", encoding="utf-8") as f:
                    f.write(self.html_content)
                with open(f"debug_{self.bookmaker_name.lower()}_prices.json", "w", encoding="utf-8") as f:
                    json.dump(self.prices_data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                print(f"Falha ao salvar debug dump: {e}")
            return

        db = SessionLocal()
        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)
            odds_to_insert = []

            for market_id, market_info in catalogue.items():
                event = market_info.get("event", {})
                event_name = event.get("name", "")
                
                if " v " not in event_name and " - " not in event_name and " vs " not in event_name.lower():
                    continue

                separator = " v "
                if " - " in event_name: separator = " - "
                elif " vs " in event_name.lower(): separator = " vs "
                
                home_team, away_team = event_name.split(separator, 1)
                
                start_time_str = event.get("openDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError:
                        pass

                match = get_or_create_match(db, home_team.strip(), away_team.strip(), "Futebol", start_time)

                m_name = str(market_info.get("marketName", "")).upper()
                m_type = str(market_info.get("marketType", "")).upper()
                market_str = f"{m_name} {m_type}"
                
                market_type = ""
                if any(m in market_str for m in ["MATCH_ODDS", "MATCH ODDS", "PROBABILIDADES", "RESULTADO FINAL", "1X2"]):
                    market_type = "1X2"
                elif any(m in market_str for m in ["AMBAS MARCAM", "BOTH TEAMS TO SCORE", "BTTS"]):
                    market_type = "BTTS"
                else:
                    continue

                runners_catalog = {}
                for r in market_info.get("runners", []):
                    r_id = str(r.get("selectionId", ""))
                    if r_id:
                        runners_catalog[r_id] = r.get("runnerName", "")

                market_prices = prices.get(market_id, [])

                for runner_price in market_prices:
                    selection_id = str(runner_price.get("selectionId", ""))
                    selection_name = runners_catalog.get(selection_id) or runner_price.get("runnerName", "")
                    selection_name = selection_name.strip()
                    
                    if not selection_name:
                        continue
                        
                    status = str(runner_price.get("runnerStatus", runner_price.get("status", ""))).upper()
                    if status and status not in ["ACTIVE", "OPEN"]:
                        continue

                    odd_value = 0.0
                    
                    runner_odds = runner_price.get("runnerOdds", {}) or runner_price.get("winRunnerOdds", {})
                    if runner_odds:
                        disp_odds = runner_odds.get("decimalDisplayOdds", {})
                        true_odds = runner_odds.get("trueOdds", {}).get("decimalOdds", {})
                        
                        if disp_odds and disp_odds.get("decimalOdds"):
                            odd_value = disp_odds.get("decimalOdds")
                        elif true_odds and true_odds.get("decimalOdds"):
                            odd_value = true_odds.get("decimalOdds")

                    if odd_value == 0.0 and "exchange" in runner_price:
                        back = runner_price["exchange"].get("availableToBack", [])
                        if back:
                            odd_value = back[0].get("price", 0.0)

                    if float(odd_value) > 1.0:
                        selection = selection_name
                        if market_type == "1X2":
                            if selection_name.upper() == home_team.strip().upper():
                                selection = "1"
                            elif selection_name.upper() == away_team.strip().upper():
                                selection = "2"
                            elif any(emp in selection_name.upper() for emp in ["EMPATE", "DRAW"]):
                                selection = "X"

                        odds_to_insert.append({
                            "match_id": match.id,
                            "bookmaker_id": bookmaker.id,
                            "market": market_type,
                            "selection": selection,
                            "odd_value": float(odd_value),
                            "is_super_odd": False
                        })

            if odds_to_insert:
                print(f"Sucesso: {len(odds_to_insert)} odds da {self.bookmaker_name} inseridas no banco.")
                bulk_insert_odds(db, odds_to_insert)
            else:
                print(f"Eventos encontrados, mas sem odds de 1X2 ou BTTS para o Duplo Green.")
                with open(f"{self.bookmaker_name.lower()}_dump.json", "w", encoding="utf-8") as f:
                    json.dump(self.prices_data, f, indent=4, ensure_ascii=False)

        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for url in self.target_urls:
            await self.extract_single(url)
            self.transform_and_load()
            
        await self.close_browser()