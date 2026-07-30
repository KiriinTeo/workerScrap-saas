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

    def _extract_match_urls(self):
        """Extrai as URLs das páginas de jogo individual a partir do
        EventMarketCard do catálogo da página de listagem (competição).
        É lá que fica o link 'eventViewLink.viewUrl' pra cada partida —
        a listagem só expõe o mercado promocional (boost), o 1X2/BTTS
        completos ficam na página do jogo."""
        urls = []
        try:
            blob = self._extract_named_state(self.html_content, "window.__TBD_PRELOADED_CATALOG__")
            if not blob:
                return urls
            state_data = json.loads(blob)
            cards = state_data.get("data", {}).get("EventMarketCard", [])
            base = self.bookmaker_base_url.rstrip("/")
            seen = set()
            for card in cards:
                view_url = (card.get("eventViewLink") or {}).get("viewUrl")
                if not view_url or view_url in seen:
                    continue
                seen.add(view_url)
                urls.append(f"{base}/apostas/{view_url.lstrip('/')}")
        except Exception as e:
            print(f"Erro ao extrair URLs de partidas: {e}")
        return urls

    @staticmethod
    def _extract_named_state(text, var_name):
        """Extrai o valor de `window.<var_name> = {...}` usando contagem de chaves
        balanceada (mais robusto que um regex .*? preguiçoso, que corta em qualquer
        '}' interno)."""
        idx = text.find(var_name)
        if idx == -1:
            return None
        eq = text.find('=', idx)
        if eq == -1:
            return None
        start = text.find('{', eq)
        if start == -1:
            return None
        depth = 0
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
                    return text[start:i + 1]
        return None

    @staticmethod
    def _urn_id(urn):
        """'ppb:event:35856785' ou 'ppb:fixture:35856785' -> '35856785'"""
        return str(urn).rsplit(':', 1)[-1] if urn else ""

    def _build_catalogue_from_graphql_cache(self, data, catalogue):
        """Monta o catálogo a partir do cache normalizado de GraphQL usado hoje
        pela Betfair/Flutter (window.__TBD_PRELOADED_CATALOG__), unindo
        SportsEvent + FootballFixture + SportsbookMarket pelo id numérico do evento."""
        events_by_urn = {e.get("urn"): e for e in data.get("SportsEvent", []) if e.get("urn")}
        fixtures_by_id = {
            self._urn_id(f.get("urn")): f for f in data.get("FootballFixture", []) if f.get("urn")
        }

        for market in data.get("SportsbookMarket", []):
            m_id = str(market.get("marketId", ""))
            event_urn = (market.get("hierarchy") or {}).get("sportevent")
            event = events_by_urn.get(event_urn)
            if not m_id or not event:
                continue

            fixture = fixtures_by_id.get(self._urn_id(event_urn))
            home_name = away_name = None
            if fixture:
                home_name = (fixture.get("home") or {}).get("name")
                away_name = (fixture.get("away") or {}).get("name")

            runners = [
                {
                    "selectionId": r.get("selectionId"),
                    "runnerName": r.get("name", ""),
                    "resultType": r.get("resultType", ""),
                }
                for r in market.get("runners", [])
            ]

            catalogue[m_id] = {
                "marketId": m_id,
                "marketName": market.get("name", ""),
                "marketType": market.get("marketType", ""),
                "runners": runners,
                "event": {
                    "name": event.get("name", ""),
                    "openDate": event.get("openDate"),
                    "home": home_name,
                    "away": away_name,
                },
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
                except json.JSONDecodeError:
                    pass

            # Schema antigo (mantido como fallback, caso alguma página ainda use)
            if not catalogue:
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

                state_blob = self._extract_named_state(self.html_content, "window.INITIAL_STATE")
                if state_blob:
                    try:
                        state_data = json.loads(state_blob)
                        self._find_catalogue_recursive(state_data, catalogue)
                        self.prices_data.append(state_data)
                    except json.JSONDecodeError:
                        pass

        except Exception as e:
            print(f"Erro ao extrair catálogo do HTML: {e}")

        # Fallback: o catálogo pode ter chegado via rede (fetch/XHR) e já estar
        # em self.prices_data (o interceptor captura qualquer resposta com
        # "selectionId", presente tanto em runnerDetails quanto em runners).
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
        """Acumula runnerDetails por marketId. Quando o site manda mais de um
        snapshot de rede pro mesmo mercado (ex: refresh durante o scroll), cada
        selectionId é deduplicado mantendo a atualização mais recente, em vez
        de simplesmente concatenar (o que gerava odds duplicadas no insert)."""
        if isinstance(data, dict):
            m_id = str(data.get("marketId", ""))
            if m_id and "runnerDetails" in data and isinstance(data["runnerDetails"], list):
                bucket = prices.setdefault(m_id, {})
                for runner in data["runnerDetails"]:
                    sel_id = str(runner.get("selectionId", ""))
                    if sel_id:
                        bucket[sel_id] = runner
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

                home_team = event.get("home")
                away_team = event.get("away")

                if not home_team or not away_team:
                    # Fallback: tenta separar o nome do evento manualmente.
                    # " x " é o separador padrão em competições brasileiras
                    # (ex: "Mirassol x Remo"); mantidos os antigos por segurança.
                    separators = [" x ", " v ", " - ", " vs "]
                    found_sep = None
                    for sep in separators:
                        if sep in event_name or sep in event_name.lower():
                            found_sep = sep
                            break
                    if not found_sep:
                        continue
                    parts = re.split(re.escape(found_sep), event_name, maxsplit=1, flags=re.IGNORECASE)
                    if len(parts) != 2:
                        continue
                    home_team, away_team = parts

                home_team = home_team.strip()
                away_team = away_team.strip()

                start_time_str = event.get("openDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError:
                        pass

                match = get_or_create_match(db, home_team, away_team, "Futebol", start_time)

                # Classificação por marketType EXATO na base (antes de qualquer
                # sufixo de variante, ex. "_-_2_UP"). Mercados "puros" (ex.
                # FULL_TIME_RESULT) e suas variantes turbinadas (ex.
                # FULL_TIME_RESULT_-_2_UP) usam a mesma base_type — a diferença
                # vira a flag is_super_odd, em vez de a variante ser descartada
                # ou confundida com o mercado normal.
                m_type_raw = str(market_info.get("marketType", "")).upper()
                base_type = m_type_raw.split("_-_")[0]
                is_super_odd = base_type != m_type_raw

                ONE_X_TWO_TYPES = {"MATCH_ODDS", "FULL_TIME_RESULT", "1X2"}
                BTTS_TYPES = {"BOTH_TEAMS_TO_SCORE", "BTTS"}

                if base_type in ONE_X_TWO_TYPES:
                    market_type = "1X2"
                elif base_type in BTTS_TYPES:
                    market_type = "BTTS"
                else:
                    continue

                runners_catalog = {}
                for r in market_info.get("runners", []):
                    r_id = str(r.get("selectionId", ""))
                    if r_id:
                        runners_catalog[r_id] = {
                            "name": r.get("runnerName", ""),
                            "resultType": str(r.get("resultType", "")).upper(),
                        }

                market_prices = prices.get(market_id, {})

                for runner_price in market_prices.values():
                    selection_id = str(runner_price.get("selectionId", ""))
                    runner_info = runners_catalog.get(selection_id, {})
                    selection_name = (runner_info.get("name") or runner_price.get("runnerName", "")).strip()
                    result_type = runner_info.get("resultType", "")
                    
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
                            RESULT_TYPE_MAP = {"HOME": "1", "AWAY": "2", "DRAW": "X"}
                            if result_type in RESULT_TYPE_MAP:
                                selection = RESULT_TYPE_MAP[result_type]
                            elif selection_name.upper() == home_team.upper():
                                selection = "1"
                            elif selection_name.upper() == away_team.upper():
                                selection = "2"
                            elif any(emp in selection_name.upper() for emp in ["EMPATE", "DRAW"]):
                                selection = "X"

                        odds_to_insert.append({
                            "match_id": match.id,
                            "bookmaker_id": bookmaker.id,
                            "market": market_type,
                            "selection": selection,
                            "odd_value": float(odd_value),
                            "is_super_odd": is_super_odd
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

            # A página de listagem da competição só expõe o mercado promocional
            # (boost) por padrão. O 1X2/BTTS completos (com empate) ficam nas
            # páginas de jogo individual — cujas URLs vêm do EventMarketCard
            # do próprio catálogo da listagem.
            match_urls = self._extract_match_urls()

            if match_urls:
                # Ainda processa a listagem (captura eventual super odd/boost),
                # depois visita cada partida pra pegar o mercado completo.
                self.transform_and_load()
                for match_url in match_urls:
                    await self.extract_single(match_url)
                    self.transform_and_load()
            else:
                # URL já era de uma página de jogo individual (ou o formato
                # da listagem mudou e não achamos EventMarketCard).
                self.transform_and_load()

        await self.close_browser()