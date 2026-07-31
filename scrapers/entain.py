import asyncio
import json
import re
import traceback
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
        self.raw_data = []
        self.html_content = ""

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

    async def get_match_urls(self, url):
        """Usa Força Bruta (Regex) no HTML bruto para caçar qualquer vestígio de URL de partida."""
        urls_encontradas = set()

        try:
            print(f"Acessando listagem Entain ({self.bookmaker_name}): {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Scroll para garantir que scripts de lazy-load injetem as variáveis no HTML
            for _ in range(4):
                await self.page.evaluate("window.scrollBy(0, 1500)")
                await self.page.wait_for_timeout(2000)

            # Extração de Força Bruta no HTML inteiro
            html_content = await self.page.content()
            
            # Limpa escapes de JSON caso existam (ex: \/sports\/events -> /sports/events)
            clean_html = html_content.replace('\\/', '/')
            
            # Regex matadora: procura qualquer coisa parecida com /pt-br/sports/events/partida-xxxx
            regex_matches = re.findall(r'(/[a-zA-Z0-9\-]+/sports/events/:]+)', clean_html)
            
            for match in regex_matches:
                if 'outright' not in match.lower():
                    urls_encontradas.add(match)

            # DOM Tradicional Fallback
            dom_hrefs = await self.page.evaluate('''() => {
                let urls = [];
                document.querySelectorAll('a').forEach(a => {
                    let href = a.getAttribute('href');
                    if (href && href.includes('/events/') && !href.includes('outright')) {
                        urls.push(href);
                    }
                });
                return urls;
            }''')
            
            for dh in dom_hrefs:
                urls_encontradas.add(dh)

        except Exception as e:
            print(f"Erro ao extrair URLs da listagem: {e}")

        parsed_url = urlparse(url)
        base = f"{parsed_url.scheme}://{parsed_url.netloc}"
        
        final_urls = []
        for path in urls_encontradas:
            clean_path = path if path.startswith('/') else f"/{path}"
            final_urls.append(f"{base}{clean_path}")

        return list(set(final_urls))

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                if isinstance(data, dict):
                    str_data = str(data)
                    if "optionMarkets" in str_data or "fixtures" in str_data:
                        self.raw_data.append(data)
            except Exception:
                pass

    async def extract_single(self, url):
        self.raw_data = []
        self.html_content = ""
        self.page.on("response", self.intercept_odds)

        try:
            print(f"Acessando partida interna: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(6000)
            
            # HTML para SSR Fallback da página interna (onde mora o "VP")
            self.html_content = await self.page.content()
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")
        finally:
            self.page.remove_listener("response", self.intercept_odds)

    def _generate_debug_dump(self, reason="unknown"):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            if self.raw_data:
                filename = f"debug_entain_{self.bookmaker_name.lower()}_network_{reason}_{timestamp}.json"
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(self.raw_data, f, indent=4, ensure_ascii=False)
            
            if self.html_content and reason in ["no_fixtures_in_match_page", "no_valid_odds_vp"]:
                html_filename = f"debug_entain_{self.bookmaker_name.lower()}_ssr_{reason}_{timestamp}.html"
                with open(html_filename, "w", encoding="utf-8") as f:
                    f.write(self.html_content)
                
            print(f"[!] Dump de debug gerado (Motivo: {reason})")
        except Exception:
            pass

    def _find_fixtures(self, data, fixtures_list, seen_ids):
        if isinstance(data, dict):
            if "optionMarkets" in data and "homeName" in data and "awayName" in data:
                f_id = data.get("id")
                if f_id and f_id not in seen_ids:
                    seen_ids.add(f_id)
                    fixtures_list.append(data)
            for key, value in data.items():
                self._find_fixtures(value, fixtures_list, seen_ids)
        elif isinstance(data, list):
            for item in data:
                self._find_fixtures(item, fixtures_list, seen_ids)

    def transform_and_load(self):
        db = SessionLocal()
        odds_to_insert = []
        fixtures = []
        seen_ids = set()
        
        self._find_fixtures(self.raw_data, fixtures, seen_ids)
        
        if self.html_content:
            for candidate in self._extract_balanced_json_objects(self.html_content, '"optionMarkets"'):
                try:
                    obj = json.loads(candidate)
                    self._find_fixtures(obj, fixtures, seen_ids)
                except Exception:
                    continue

        if not fixtures:
            print(f"Falha total: Nenhum dado de jogo localizado.")
            self._generate_debug_dump(reason="no_fixtures_in_match_page")
            return

        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)

            for fixture in fixtures:
                home_team = fixture.get("homeName", "").strip()
                away_team = fixture.get("awayName", "").strip()
                
                if not home_team or not away_team: continue
                    
                start_time_str = fixture.get("startDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try: start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError: pass
                        
                match = get_or_create_match(db, home_team, away_team, "Futebol", start_time)
                
                option_markets = fixture.get("optionMarkets", [])
                for market in option_markets:
                    market_name = market.get("name", {"value": ""}).get("value", "").upper()
                    
                    if not any(m in market_name for m in ["RESULTADO DA PARTIDA", "1X2", "MATCH RESULT", "VENCEDOR"]):
                        continue
                    
                    has_early_payout = any(term in market_name for term in ["PAGAMENTO ANTECIPADO", "VANTAGEM", "EARLY PAYOUT", "(VP)", " VP"])
                    
                    options = market.get("options", [])
                    for option in options:
                        odd_value = 0.0
                        price_data = option.get("price", {})
                        if price_data:
                            num = price_data.get("numerator")
                            den = price_data.get("denominator")
                            if num is not None and den is not None and den != 0:
                                odd_value = (float(num) / float(den)) + 1.0

                        if odd_value <= 1.0: continue

                        raw_selection_name = option.get("name", {"value": ""}).get("value", "").strip().upper()
                        is_super_odd = option.get("isBoosted", False) or any(b in market_name for b in ["BOOST", "AUMENTADA", "TURBINADA", "MELHORADA"])
                        
                        sel_code = None
                        if raw_selection_name in ["1", home_team.upper()]: sel_code = "1"
                        elif raw_selection_name in ["X", "EMPATE", "DRAW"]: sel_code = "X"
                        elif raw_selection_name in ["2", away_team.upper()]: sel_code = "2"

                        if not sel_code: continue

                        is_vitoria = sel_code in ["1", "2"]
                        is_empate = sel_code == "X"

                        if is_vitoria and not has_early_payout: continue
                        if is_empate and not is_super_odd: continue

                        odds_to_insert.append({
                            "match_id": match.id,
                            "bookmaker_id": bookmaker.id,
                            "market": "1X2",
                            "selection": sel_code,
                            "odd_value": float(odd_value),
                            "is_super_odd": bool(is_super_odd)
                        })
                        
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds restritas da {self.bookmaker_name} salvas.")
                self._generate_debug_dump(reason="valid_odds_saved")
            else:
                print(f"Eventos processados. Nenhuma seleção passou nos filtros rigorosos de VP/SuperOdd.")
                self._generate_debug_dump(reason="no_valid_odds_vp")

        except Exception as e:
            print(f"Erro no processamento da {self.bookmaker_name}: {e}")
            traceback.print_exc()
            self._generate_debug_dump(reason="exception_raised")
        finally:
            db.close()

    async def run(self):
        await self.init_browser()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        for base_url in self.target_urls:
            match_urls = await self.get_match_urls(base_url)
            print(f"[-] {len(match_urls)} URLs extraídas brutalmente do HTML. Iniciando escaneamento profundo...")
            
            for match_url in match_urls:
                await self.extract_single(match_url)
                self.transform_and_load()
            
        await self.close_browser()