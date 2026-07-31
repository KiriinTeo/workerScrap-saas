import asyncio
import json
import traceback
from datetime import datetime, timezone
from scrapers.base_scraper import BaseScraper
from db.crud import get_or_create_bookmaker, get_or_create_match, bulk_insert_odds
from config.database import SessionLocal

class AltenarScraper(BaseScraper):
    def __init__(self, target_urls, bookmaker_name, bookmaker_base_url, headless=False):
        super().__init__(headless)
        self.target_urls = target_urls if isinstance(target_urls, list) else [target_urls]
        self.bookmaker_name = bookmaker_name
        self.bookmaker_base_url = bookmaker_base_url
        self.raw_data = None

    async def intercept_odds(self, response):
        if response.status == 200 and 'json' in response.headers.get('content-type', '').lower():
            try:
                data = await response.json()
                if isinstance(data, dict) and 'events' in data and 'markets' in data:
                    if len(data.get('events', [])) > 0:
                        self.raw_data = data
                        print(f"[+] Payload rico interceptado! {len(data['events'])} eventos encontrados.")
            except Exception:
                pass

    async def extract_single(self, url):
        self.raw_data = None
        self.page.on("response", self.intercept_odds)
        
        try:
            print(f"Acessando {self.bookmaker_name}: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(10000)
        except Exception as e:
            print(f"Erro ao acessar {url}: {e}")
            
        self.page.remove_listener("response", self.intercept_odds)

    def _generate_debug_dump(self, reason="unknown"):
        if self.raw_data:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"debug_{self.bookmaker_name.lower()}_{reason}_{timestamp}.json"
            try:
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(self.raw_data, f, indent=4, ensure_ascii=False)
                print(f"[!] Dump de debug gerado: {filename} (Motivo: {reason})")
            except Exception as e:
                print(f"Falha ao gerar dump de debug: {e}")

    def transform_and_load(self):
        if not self.raw_data:
            print(f"Nenhum dado interceptado na {self.bookmaker_name}.")
            return

        db = SessionLocal()
        odds_to_insert = []
        
        try:
            bookmaker = get_or_create_bookmaker(db, self.bookmaker_name, self.bookmaker_base_url)
            
            events_raw = self.raw_data.get("events", [])
            markets_raw = {m.get("id"): m for m in self.raw_data.get("markets", [])}
            odds_raw = {o.get("id"): o for o in self.raw_data.get("odds", [])}
            
            if not events_raw:
                self._generate_debug_dump(reason="no_events")
                return

            for event in events_raw:
                name_str = event.get("name", "")
                if not name_str or " vs. " not in name_str:
                    continue
                    
                home_team, away_team = name_str.split(" vs. ", 1)
                home_team, away_team = home_team.strip(), away_team.strip()
                
                start_time_str = event.get("startDate")
                start_time = datetime.now(timezone.utc)
                if start_time_str:
                    try:
                        start_time = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
                    except ValueError:
                        pass
                        
                match = get_or_create_match(db, home_team, away_team, "Futebol", start_time)
                
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
                        
                        if odd_value and float(odd_value) > 1.0:
                            type_id = odd_data.get("typeId")
                            selection = None
                            
                            if type_id == 1 or raw_selection_name == home_team:
                                selection = "1"
                            elif type_id == 2 or "EMPATE" in raw_selection_name.upper() or "DRAW" in raw_selection_name.upper():
                                selection = "X"
                            elif type_id == 3 or raw_selection_name == away_team:
                                selection = "2"
                                
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
                                "is_super_odd": bool(is_super_odd)
                            })
                            
            if odds_to_insert:
                bulk_insert_odds(db, odds_to_insert)
                print(f"Sucesso: {len(odds_to_insert)} odds filtradas da {self.bookmaker_name} inseridas no banco.")
                self._generate_debug_dump(reason="success")
            else:
                print(f"Eventos processados, mas nenhuma odd passou no filtro restrito (Vitória+EP ou Empate+SuperOdd).")
                self._generate_debug_dump(reason="no_valid_odds")

        except Exception as e:
            print(f"Erro no processamento da {self.bookmaker_name}: {e}")
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